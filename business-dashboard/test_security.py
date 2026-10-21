"""Offline signed-token, authorization and tenant isolation regressions."""
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pandas as pd
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0,str(Path(__file__).parent))
from dashboard_security import AccessDenied, OperatorVerifier, id_token
import test_dashboard
dashboard = test_dashboard.dashboard

DIR='00000000-0000-0000-0000-000000000001'
CLIENT='00000000-0000-0000-0000-000000000002'
USER='00000000-0000-0000-0000-000000000003'


@pytest.fixture
def identity():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    keys=Mock(get_signing_key_from_jwt=Mock(return_value=SimpleNamespace(key=key.public_key())))
    verifier=OperatorVerifier(DIR,CLIENT,{USER:['TENANT-01']},keys,user_permissions={USER:['Dashboard.Read','Incidents.Simulate']})
    def token(**changes):
        claims=dict(iss=f'https://login.microsoftonline.com/{DIR}/v2.0',aud=CLIENT,exp=int(time.time())+300,
                    tid=DIR,oid=USER,roles=['Dashboard.Read','Incidents.Simulate'])
        claims.update(changes)
        return jwt.encode(claims,key,algorithm='RS256')
    return verifier,token


def test_signed_operator_has_exact_authorized_scope(identity):
    verifier,token=identity
    context=verifier.verify(token())
    assert context.tenants==('TENANT-01',) and context.can_simulate
    assert verifier.verify(token(roles=['Dashboard.Read'])).can_simulate is False


@pytest.mark.parametrize('changes',[
    {'oid':'unknown'}, {'tid':'foreign'}, {'aud':'wrong'}, {'iss':'https://evil.example'},
    {'exp':1}, {'roles':['Incidents.Write']}, {'roles':[]}, {'roles':'Dashboard.Read'},
])
def test_unknown_expired_or_wrong_identity_denied(identity,changes):
    verifier,token=identity
    with pytest.raises(AccessDenied):verifier.verify(token(**changes))


def test_unsigned_principal_header_cannot_authenticate(identity):
    verifier,_=identity
    assert id_token({'X-MS-CLIENT-PRINCIPAL': 'fake'})==''
    with pytest.raises(AccessDenied):verifier.verify('')


def test_approved_policy_has_no_other_users_or_tenants():
    policy=json.loads(Path(__file__).with_name('access-policy.json').read_text())
    assert policy['directory_id']=='5ba5ef5e-3109-4e77-85bd-cfeb0d347e82'
    assert list(policy['users'])==['ded95a62-f8b6-4589-b49a-2041a35d10d7']
    assert policy['users']['ded95a62-f8b6-4589-b49a-2041a35d10d7']['tenants']==['TENANT-01','TENANT-02','TENANT-03']


def test_missing_login_client_fails_closed(monkeypatch):
    monkeypatch.delenv('DASHBOARD_AUTH_CLIENT_ID',raising=False)
    with pytest.raises(ValueError):OperatorVerifier.from_env()


def test_queries_use_only_authorized_partitions_and_keep_all_records(monkeypatch):
    rows=[dict(tenant_id='TENANT-01',event_id=str(i),timestamp='2026-10-19T10:00:00Z') for i in range(250)]
    container=Mock(query_items=Mock(return_value=iter(rows)))
    monkeypatch.setattr(dashboard,'get_container',lambda:container)
    dashboard._load_incidents.clear()
    frame=dashboard._load_incidents(('TENANT-01',))
    assert len(frame)==250
    call=container.query_items.call_args.kwargs
    assert call['partition_key']=='TENANT-01'
    assert call['parameters']==[{'name':'@tenant','value':'TENANT-01'}]
    assert 'TOP ' not in call['query'] and 'LIMIT ' not in call['query']
    dashboard._load_incidents.clear()


def test_foreign_tenant_data_is_rejected_even_if_sdk_returns_it(monkeypatch):
    container=Mock(query_items=Mock(return_value=[dict(tenant_id='TENANT-02',event_id='foreign',timestamp='2026-10-19T10:00:00Z')]))
    monkeypatch.setattr(dashboard,'get_container',lambda:container)
    dashboard._load_incidents.clear()
    with pytest.raises(AccessDenied):dashboard._load_incidents(('TENANT-01',))
    dashboard._load_incidents.clear()


def test_evidence_not_in_authorized_dataset_is_never_read(monkeypatch):
    monkeypatch.setattr(dashboard,'get_evidence_store',lambda:object())
    monkeypatch.setattr(dashboard,'require_operator',lambda:SimpleNamespace(tenants=('TENANT-01',)))
    monkeypatch.setattr(dashboard,'_load_incidents',lambda tenants:pd.DataFrame({'evidence_uri':['https://valid/own.jpg']}))
    fetch=Mock()
    monkeypatch.setattr(dashboard,'_load_preview',fetch)
    assert dashboard.load_evidence('https://valid/foreign.jpg') is None
    fetch.assert_not_called()


def test_authentication_precedes_cached_incident_read(monkeypatch):
    def deny():raise AccessDenied('denied')
    monkeypatch.setattr(dashboard,'require_operator',deny)
    fetch=Mock()
    monkeypatch.setattr(dashboard,'_load_incidents',fetch)
    with pytest.raises(AccessDenied):dashboard.load_incidents()
    fetch.assert_not_called()


def test_tied_timestamps_have_deterministic_event_order():
    frame=pd.DataFrame({'tenant_id':['TENANT-01']*3,'event_id':['C','A','B'],'timestamp':pd.to_datetime(['2026-10-19T10:00:00Z']*3)})
    assert dashboard.ordered_incidents(frame)['event_id'].tolist()==['A','B','C']


def test_native_oidc_signed_identity_is_used_instead_of_session_claims(monkeypatch,identity):
    verifier,token=identity
    monkeypatch.setattr(dashboard.st,'user',SimpleNamespace(is_logged_in=True,tokens={'id':token(),'access':'delegated'}))
    monkeypatch.setattr(dashboard,'get_operator_verifier',lambda:verifier)
    monkeypatch.setenv('DASHBOARD_AUTH_MODE','oidc')
    assert dashboard.require_operator().user_id==USER
    assert dashboard.delegated_api_token()=='delegated'


def test_original_blob_transfer_is_bounded():
    client=Mock()
    client.__enter__=Mock(return_value=client)
    client.__exit__=Mock(return_value=False)
    client.get_blob_properties.return_value=SimpleNamespace(size=2,content_settings=SimpleNamespace(content_type='image/jpeg'))
    client.download_blob.return_value.readall.return_value=b'abc'
    store=dashboard.EvidenceStore('https://storage.blob.core.windows.net','evidence',max_bytes=2,credential=object(),blob_client_factory=Mock(return_value=client))
    with pytest.raises(dashboard.EvidenceUnavailable):store.fetch('https://storage.blob.core.windows.net/evidence/test.jpg')
    # The SDK rejects a length without an offset; see test_evidence_blob_sdk.py.
    client.download_blob.assert_called_once_with(offset=0,length=3)


def test_read_only_operator_never_sees_simulation_controls(monkeypatch):
    monkeypatch.setattr(dashboard,'require_operator',lambda:SimpleNamespace(tenants=('TENANT-01',),can_simulate=False))
    post=Mock()
    monkeypatch.setattr(dashboard.simulator,'publish',post)
    at=test_dashboard.run_ui('app.render_simulator()')
    assert not at.exception and at.error and not at.button
    post.assert_not_called()
