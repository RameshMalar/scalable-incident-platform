"""Tests of the frontend against the PROPOSED self-service API contract."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0,str(Path(__file__).parent))
import account_setup as account


@pytest.fixture
def account_ui(monkeypatch):
    monkeypatch.setattr(account,'test_operator',lambda:SimpleNamespace(tenants=('TENANT-01',)),raising=False)
    monkeypatch.setattr(account,'access_token',lambda headers:'delegated-token')
    monkeypatch.delenv('ENABLE_TELEGRAM_ONBOARDING',raising=False)
    status={'contract':'self-service-v1','tenant_id':'TENANT-01','verified':True,'enabled':False,'event_types':[], 'supported_event_types':['intrusion_detected']}
    api=Mock(return_value=status)
    monkeypatch.setattr(account,'request_preferences',api)
    def run():return AppTest.from_string('import account_setup as app\napp.render_account_setup(app.test_operator)').run(timeout=10)
    return run,api,status


def test_onboarding_disabled_by_default_makes_no_api_calls(account_ui):
    run,api,_=account_ui
    at=run()
    assert not at.exception and not at.checkbox
    api.assert_not_called()


def test_unverified_owner_cannot_enable_alerts(monkeypatch,account_ui):
    run,api,status=account_ui
    monkeypatch.setenv('ENABLE_TELEGRAM_ONBOARDING','true')
    status['verified']=False
    at=run()
    assert not at.exception
    save=next(button for button in at.button if button.label=='Save account preferences')
    assert save.disabled
    assert not at.text_input  # No chat-ID or bot-token entry.


@pytest.mark.parametrize('enable',[True,False])
def test_subscribe_and_unsubscribe_use_self_service_put(monkeypatch,account_ui,enable):
    run,api,status=account_ui
    monkeypatch.setenv('ENABLE_TELEGRAM_ONBOARDING','true')
    status['enabled']=not enable
    at=run()
    at.checkbox[0].set_value(enable)
    at.multiselect[0].select('intrusion_detected')
    response=dict(status,enabled=enable)
    api.side_effect=lambda method,*args: response if method=='PUT' else status
    next(button for button in at.button if button.label=='Save account preferences').click().run()
    assert not at.exception and at.success
    writes=[call for call in api.call_args_list if call.args[0]=='PUT']
    assert len(writes)==1
    assert writes[0].args[4]=={'enabled':enable,'event_types':['intrusion_detected']}
    assert 'telegram_chat_id' not in writes[0].args[4]


def test_foreign_tenant_api_response_is_rejected(monkeypatch,account_ui):
    run,api,status=account_ui
    monkeypatch.setenv('ENABLE_TELEGRAM_ONBOARDING','true')
    status['tenant_id']='TENANT-02'
    at=run()
    assert not at.exception and at.error and not at.checkbox


def test_unapproved_api_contract_has_no_subscription_controls(monkeypatch,account_ui):
    run,api,status=account_ui
    monkeypatch.setenv('ENABLE_TELEGRAM_ONBOARDING','true')
    status['contract']='legacy-admin-crud'
    at=run()
    assert not at.exception and at.error and not at.checkbox


def test_revoked_verification_still_allows_owner_to_opt_out(monkeypatch,account_ui):
    run,api,status=account_ui
    monkeypatch.setenv('ENABLE_TELEGRAM_ONBOARDING','true')
    status.update(verified=False,enabled=True)
    at=run()
    at.checkbox[0].uncheck()
    api.side_effect=lambda method,*args: dict(status,enabled=False) if method=='PUT' else status
    next(button for button in at.button if button.label=='Save account preferences').click().run()
    assert not at.exception and at.success
    assert any(call.args[0]=='PUT' and call.args[4]['enabled'] is False for call in api.call_args_list)
