"""Offline lifecycle contract tests; no Telegram/Azure/network calls."""
import copy
from urllib.parse import parse_qs,urlparse

import pytest
from test_central_access import ROOT,DIR,USER,Store,Proofs,POLICY,run,register
from central_registry import CentralRegistry,RegistryDenied,RegistryUnavailable
from telegram_lifecycle import TelegramLifecycle


class TelegramStore:
    def __init__(self):self.challenges={};self.rows={}
    async def issue(self,digest,value):self.challenges[digest]=copy.deepcopy(value)
    async def challenge(self,digest):return copy.deepcopy(self.challenges.get(digest))
    async def consume_and_bind(self,digest,expected,chat,now):
        current=self.challenges.get(digest)
        if current!=expected or current['expires']<=now:raise RegistryDenied('Challenge consumed')
        key=(current['user'],current['tenant'])
        self.rows[key]=dict(verified=True,destination_approved=True,enabled=False,event_types=[],private_chat_id=chat)
        del self.challenges[digest]
    async def preferences(self,user,tenant):return copy.deepcopy(self.rows.get((user,tenant)))
    async def save_preferences(self,user,tenant,before,enabled,event_types):
        key=(user,tenant)
        if (self.rows.get(key) or {})!=before:raise RegistryDenied('Concurrent preference change')
        self.rows[key]={**before,'enabled':enabled,'event_types':event_types}


@pytest.fixture
def lifecycle():
    registry=CentralRegistry(Store(),POLICY,Proofs())
    register(registry)
    return TelegramLifecycle(registry,TelegramStore(),'offline_test_bot','x'*32,['intrusion_detected'],clock=lambda:1000)


def link(service):
    challenge=run(service.challenge(USER,DIR,'TENANT-01'))
    token=parse_qs(urlparse(challenge['deep_link']).query)['start'][0]
    update=dict(message=dict(text='/start '+token,chat={'id':123,'type':'private'},**{'from':{'id':123,'is_bot':False}}))
    run(service.verify_webhook('x'*32,update))
    return update


def test_ownership_is_not_implicit_opt_in_and_no_private_data_in_status(lifecycle):
    link(lifecycle)
    status=run(lifecycle.status(USER,DIR,'TENANT-01'))
    assert status['verified'] and not status['enabled']
    assert 'private_chat_id' not in status
    assert run(lifecycle.registry.authorize(USER,DIR,'Dashboard.Read','TENANT-01'))


def test_complete_opt_in_preferences_and_unsubscribe(lifecycle):
    link(lifecycle)
    run(lifecycle.preferences(USER,DIR,'TENANT-01',True,['intrusion_detected']))
    assert run(lifecycle.eligible(USER,DIR,'TENANT-01','intrusion_detected','production'))
    run(lifecycle.preferences(USER,DIR,'TENANT-01',False,[]))
    assert not run(lifecycle.eligible(USER,DIR,'TENANT-01','intrusion_detected','production'))
    run(lifecycle.preferences(USER,DIR,'TENANT-01',False,[])) # idempotent


def test_no_subscription_required_for_dashboard_and_unverified_cannot_opt_in(lifecycle):
    assert run(lifecycle.registry.authorize(USER,DIR,'Dashboard.Read','TENANT-01'))
    with pytest.raises(RegistryDenied):run(lifecycle.preferences(USER,DIR,'TENANT-01',True,['intrusion_detected']))


def test_expired_replayed_and_forged_webhook_denied(lifecycle):
    update=link(lifecycle)
    with pytest.raises(RegistryDenied):run(lifecycle.verify_webhook('x'*32,update))
    with pytest.raises(RegistryDenied):run(lifecycle.verify_webhook('wrong',update))
    challenge=run(lifecycle.challenge(USER,DIR,'TENANT-01'))
    token=parse_qs(urlparse(challenge['deep_link']).query)['start'][0]
    update['message']['text']='/start '+token
    lifecycle.clock=lambda:1301
    with pytest.raises(RegistryDenied):run(lifecycle.verify_webhook('x'*32,update))


def test_group_chat_cannot_verify_private_ownership(lifecycle):
    update=link(lifecycle)
    update['message']['chat']['type']='group'
    with pytest.raises(RegistryDenied):run(lifecycle.verify_webhook('x'*32,update))


@pytest.mark.parametrize('status',['disabled','revoked'])
def test_access_removal_blocks_queued_notification_eligibility(lifecycle,status):
    link(lifecycle)
    run(lifecycle.preferences(USER,DIR,'TENANT-01',True,['intrusion_detected']))
    run(lifecycle.registry.change(ROOT,DIR,USER,1,{'status':status},'synthetic fixture'))
    with pytest.raises(RegistryDenied):run(lifecycle.eligible(USER,DIR,'TENANT-01','intrusion_detected','production'))


def test_wrong_tenant_test_purpose_and_unselected_events_do_not_deliver(lifecycle):
    link(lifecycle)
    run(lifecycle.preferences(USER,DIR,'TENANT-01',True,['intrusion_detected']))
    with pytest.raises(RegistryDenied):run(lifecycle.eligible(USER,DIR,'TENANT-02','intrusion_detected','production'))
    assert not run(lifecycle.eligible(USER,DIR,'TENANT-01','intrusion_detected','load_test'))
    assert not run(lifecycle.eligible(USER,DIR,'TENANT-01','smoke_detected','production'))


def test_destination_approval_and_registry_availability_are_mandatory(lifecycle):
    link(lifecycle)
    lifecycle.store.rows[(USER,'TENANT-01')]['destination_approved']=False
    with pytest.raises(RegistryDenied):run(lifecycle.preferences(USER,DIR,'TENANT-01',True,[]))
    lifecycle.registry.store.fail=True
    with pytest.raises(RegistryUnavailable):run(lifecycle.eligible(USER,DIR,'TENANT-01','intrusion_detected','production'))


def test_opt_out_remains_possible_after_ownership_invalidated(lifecycle):
    link(lifecycle)
    run(lifecycle.preferences(USER,DIR,'TENANT-01',True,['intrusion_detected']))
    lifecycle.store.rows[(USER,'TENANT-01')]['verified']=False
    assert not run(lifecycle.preferences(USER,DIR,'TENANT-01',False,[]))['enabled']
