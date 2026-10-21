"""Pure local tests. Every identity except the approved root is a synthetic fixture."""
import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HERE=Path(__file__).parent
sys.path[:0]=[str(HERE),str(HERE/'review-proposals')]
from access_roles import BOOTSTRAP_ADMIN_ID as ROOT, Role, InvalidAccess, parse_grant
from access_api import current_grant, AccessAPIUnavailable, request_access
from admin_access import require_admin
from dashboard_security import AccessDenied, OperatorContext
from central_registry import CentralRegistry, RegistryConflict, RegistryDenied, RegistryUnavailable

POLICY=json.loads((HERE/'access-policy.json').read_text())
DIR=POLICY['directory_id']
USER='00000000-0000-4000-8000-000000000001'
OTHER='00000000-0000-4000-8000-000000000002'


def run(coro):return asyncio.run(coro)


class Store:
    def __init__(self):self.rows={};self.audits=[];self.fail=False
    async def get(self,user):
        if self.fail:raise OSError('private connection details')
        return copy.deepcopy(self.rows.get(user))
    async def list(self):return copy.deepcopy(list(self.rows.values()))
    async def commit(self,before,after,audit):
        current=self.rows.get(after['object_id'])
        if current!=before:raise RegistryConflict('CAS')
        self.rows[after['object_id']]=copy.deepcopy(after)
        self.audits.append(copy.deepcopy(audit))


class Proofs:
    async def verified(self,user,directory):return user in (USER,OTHER) and directory==DIR


@pytest.fixture
def registry():return CentralRegistry(Store(),POLICY,Proofs())


def register(registry,role=Role.SOC_VIEWER,user=USER,tenants=None):
    return run(registry.register(ROOT,DIR,user,role,tenants or ['TENANT-01'],'offline test fixture approval'))


def test_bootstrap_exact_mapping_and_no_automatic_registration(registry):
    grant=run(registry.resolve(ROOT,DIR))
    assert grant.role==Role.PLATFORM_ADMIN and set(grant.tenants)=={'TENANT-01','TENANT-02','TENANT-03'}
    assert not registry.store.rows and not registry.store.audits


@pytest.mark.parametrize('role,simulate,manage',[(Role.PLATFORM_ADMIN,True,True),(Role.SOC_OPERATOR,True,False),(Role.SOC_VIEWER,False,False)])
def test_three_roles_and_simulator_authorization(registry,role,simulate,manage):
    grant=register(registry,role)
    assert ('Incidents.Simulate' in grant.permissions)==simulate
    assert ('Access.Manage' in grant.permissions)==manage
    assert 'Dashboard.Read' in grant.permissions and 'Subscriptions.SelfManage' in grant.permissions
    if simulate:run(registry.authorize(USER,DIR,'Incidents.Simulate','TENANT-01'))
    else:
        with pytest.raises(RegistryDenied):run(registry.authorize(USER,DIR,'Incidents.Simulate','TENANT-01'))


@pytest.mark.parametrize('permission',['Dashboard.Read','Incidents.Simulate','Subscriptions.SelfManage'])
def test_cross_tenant_denial(registry,permission):
    register(registry,Role.SOC_OPERATOR)
    with pytest.raises(RegistryDenied):run(registry.authorize(USER,DIR,permission,'TENANT-02'))


def test_unknown_user_wrong_directory_and_unapproved_tenant_denied(registry):
    with pytest.raises(RegistryDenied):run(registry.resolve(USER,DIR))
    with pytest.raises(RegistryDenied):run(registry.resolve(ROOT,USER))
    with pytest.raises(InvalidAccess):register(registry,tenants=['TENANT-DEV'])
    assert not registry.store.rows


def test_uuid_without_verified_identity_receipt_cannot_be_registered(registry):
    registry.identity_proofs=None
    with pytest.raises(RegistryDenied):register(registry)
    assert not registry.store.rows


@pytest.mark.parametrize('status',['disabled','revoked'])
def test_revocation_blocks_next_request_and_simulation(registry,status):
    register(registry,Role.SOC_OPERATOR)
    run(registry.authorize(USER,DIR,'Dashboard.Read','TENANT-01'))
    run(registry.change(ROOT,DIR,USER,1,{'status':status},'explicit test revocation'))
    for permission in ('Dashboard.Read','Incidents.Simulate','Subscriptions.SelfManage'):
        with pytest.raises(RegistryDenied):run(registry.authorize(USER,DIR,permission,'TENANT-01'))
    assert len(registry.store.audits)==2


def test_tenant_removal_takes_effect_without_token_reissue(registry):
    register(registry,tenants=['TENANT-01','TENANT-02'])
    run(registry.change(ROOT,DIR,USER,1,{'tenants':['TENANT-02']},'remove tenant'))
    with pytest.raises(RegistryDenied):run(registry.authorize(USER,DIR,'Dashboard.Read','TENANT-01'))


def test_changes_require_admin_and_original_approval_for_new_members(registry):
    register(registry,Role.SOC_VIEWER)
    with pytest.raises(RegistryDenied):run(registry.change(USER,DIR,USER,1,{'role':Role.PLATFORM_ADMIN},'self elevate'))
    run(registry.change(ROOT,DIR,USER,1,{'role':Role.PLATFORM_ADMIN},'synthetic admin fixture'))
    with pytest.raises(RegistryDenied):run(registry.register(USER,DIR,OTHER,Role.SOC_VIEWER,['TENANT-01'],'not root'))
    assert OTHER not in registry.store.rows


def test_immutable_admin_and_compare_and_swap(registry):
    with pytest.raises(RegistryDenied):run(registry.change(ROOT,DIR,ROOT,1,{'status':'disabled'},'deny'))
    register(registry)
    with pytest.raises(RegistryConflict):run(registry.change(ROOT,DIR,USER,2,{'status':'disabled'},'stale'))
    assert registry.store.rows[USER]['status']=='active' and len(registry.store.audits)==1


def test_registry_outage_never_falls_back_even_for_root(registry):
    registry.store.fail=True
    with pytest.raises(RegistryUnavailable,match='^Access registry unavailable$'):run(registry.resolve(ROOT,DIR))


def test_revoked_registration_cannot_be_reenabled(registry):
    register(registry)
    run(registry.change(ROOT,DIR,USER,1,{'status':'revoked'},'revoke'))
    with pytest.raises(RegistryDenied):run(registry.change(ROOT,DIR,USER,2,{'status':'active'},'re-enable'))


def test_registry_list_is_paginated_and_requires_admin(registry):
    register(registry)
    first=run(registry.users(ROOT,DIR,limit=1))
    second=run(registry.users(ROOT,DIR,cursor=first['next_cursor'],limit=1))
    assert first['items'][0]['object_id']!=second['items'][0]['object_id']
    with pytest.raises(RegistryDenied):run(registry.users(USER,DIR))


def test_current_grant_rechecks_every_time_and_validates_subject(monkeypatch,registry):
    import access_api
    grant=register(registry).public()
    api=Mock(return_value=dict(grant,contract='central-access-v1'))
    monkeypatch.setattr(access_api,'request_access',api)
    identity=SimpleNamespace(user_id=USER,directory_id=DIR)
    current_grant(identity,'token',registry.tenants)
    api.return_value=dict(grant,contract='central-access-v1',status='revoked')
    with pytest.raises(InvalidAccess):current_grant(identity,'token',registry.tenants)
    assert api.call_count==2
    api.return_value=dict(grant,contract='central-access-v1',object_id=OTHER)
    with pytest.raises(InvalidAccess):current_grant(identity,'token',registry.tenants)


@pytest.mark.parametrize('origin',['http://example.test','https://name:secret@example.test','https://example.test/path'])
def test_access_transport_rejects_insecure_origin_without_call(monkeypatch,origin):
    monkeypatch.setenv('SUBSCRIPTION_API_ORIGIN',origin)
    opener=Mock()
    with pytest.raises(AccessAPIUnavailable):request_access('GET','/api/v1/access/me','token',opener=opener)
    opener.open.assert_not_called()


def test_admin_interface_checks_role_on_server():
    viewer=OperatorContext(USER,DIR,('TENANT-01',),frozenset({'Dashboard.Read'}),9999999999,Role.SOC_VIEWER)
    with pytest.raises(AccessDenied):require_admin(lambda:viewer)


def test_admin_page_disabled_by_default_never_calls_backend(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import admin_access
    admin=OperatorContext(ROOT,DIR,('TENANT-01','TENANT-02','TENANT-03'),frozenset({'Dashboard.Read','Access.Manage'}),9999999999,Role.PLATFORM_ADMIN)
    monkeypatch.setattr(admin_access,'fixture_operator',lambda:admin,raising=False)
    api=Mock();monkeypatch.setattr(admin_access,'request_access',api)
    monkeypatch.delenv('ENABLE_CENTRAL_ACCESS',raising=False)
    at=AppTest.from_string('import admin_access as a\na.render_admin_access(a.fixture_operator,lambda:"offline-token")').run()
    assert not at.exception and at.info and not at.text_input
    api.assert_not_called()


def test_admin_page_direct_entry_by_viewer_is_denied(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import admin_access
    viewer=OperatorContext(USER,DIR,('TENANT-01',),frozenset({'Dashboard.Read'}),9999999999,Role.SOC_VIEWER)
    monkeypatch.setattr(admin_access,'fixture_operator',lambda:viewer,raising=False)
    api=Mock();monkeypatch.setattr(admin_access,'request_access',api)
    monkeypatch.setenv('ENABLE_CENTRAL_ACCESS','true')
    at=AppTest.from_string('import admin_access as a\na.render_admin_access(a.fixture_operator,lambda:"offline-token")').run()
    assert not at.exception and at.error
    api.assert_not_called()


def test_unknown_role_is_rejected(registry):
    value=register(registry).public();value['role']='owner'
    with pytest.raises(InvalidAccess):parse_grant(value,DIR,registry.tenants)
