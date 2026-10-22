"""Startup sign-in preflight and Admin destination review. No network."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import admin_access
import auth_preflight
from access_api import AccessAPIUnavailable, PENDING_DESTINATIONS, request_access

CLIENT = '00000000-0000-4000-8000-0000000000c1'
DIRECTORY = json.load(open(auth_preflight.POLICY, encoding='utf-8'))['directory_id']
REDIRECT = 'https://dash---signin-test.example.azurecontainerapps.io/oauth2callback'
SCOPE = 'api://00000000-0000-4000-8000-0000000000a1/user_impersonation'


def secrets(**changes):
    auth = {'redirect_uri': REDIRECT, 'cookie_secret': 'c' * 40, 'expose_tokens': ['id', 'access']}
    provider = {'client_id': CLIENT, 'client_secret': 'placeholder-not-a-secret',
                'server_metadata_url': f'https://login.microsoftonline.com/{DIRECTORY}/v2.0/.well-known/openid-configuration',
                'client_kwargs': {'scope': 'openid profile email ' + SCOPE}}
    auth.update(changes.pop('auth', {}))
    provider.update(changes.pop('provider', {}))
    lines = ['[auth]'] + [f'{k} = {json.dumps(v)}' for k, v in auth.items()]
    lines += ['[auth.microsoft]'] + [f'{k} = {json.dumps(v)}' if k != 'client_kwargs'
                                     else f'client_kwargs = {{ scope = {json.dumps(v["scope"])} }}' for k, v in provider.items()]
    return '\n'.join(lines)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'secrets.toml'
    path.write_text(secrets(), encoding='utf-8')
    return dict(DASHBOARD_AUTH_CLIENT_ID=CLIENT, DASHBOARD_EXPECTED_REDIRECT_URI=REDIRECT,
                STREAMLIT_SECRETS_FILES=str(path), ENABLE_CENTRAL_ACCESS='true', DASHBOARD_API_SCOPE=SCOPE), path


def test_complete_configuration_passes(env):
    assert auth_preflight.check(env[0]) == []


@pytest.mark.parametrize('changes,failure', [
    ({'auth': {'redirect_uri': 'https://incident-dashboard.example/oauth2callback'}}, 'redirect_uri_matches_revision'),
    ({'auth': {'cookie_secret': 'short'}}, 'cookie_secret_strength'),
    ({'auth': {'expose_tokens': ['id']}}, 'expose_tokens'),
    ({'provider': {'client_id': '00000000-0000-4000-8000-0000000000ff'}}, 'client_id_matches_setting'),
    ({'provider': {'client_secret': ' '}}, 'client_secret_present'),
    ({'provider': {'server_metadata_url': 'https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration'}}, 'approved_directory_metadata'),
    ({'provider': {'client_kwargs': {'scope': 'openid profile'}}}, 'api_scope_for_central_access'),
    ({'provider': {'client_kwargs': {'scope': SCOPE}}}, 'openid_scope'),
])
def test_each_inconsistency_is_named(env, changes, failure):
    settings, path = env
    path.write_text(secrets(**changes), encoding='utf-8')
    assert failure in auth_preflight.check(settings)


def test_missing_settings_or_file_fail_closed(env, capsys, monkeypatch):
    settings, path = env
    assert 'client_id_setting' in auth_preflight.check({**settings, 'DASHBOARD_AUTH_CLIENT_ID': ''})
    assert 'expected_redirect_setting' in auth_preflight.check({**settings, 'DASHBOARD_EXPECTED_REDIRECT_URI': 'http://x/cb'})
    assert auth_preflight.check({**settings, 'STREAMLIT_SECRETS_FILES': ''}) == ['secrets_file_setting']
    assert auth_preflight.check({**settings, 'STREAMLIT_SECRETS_FILES': str(path) + '.missing'}) == ['secrets_file_readable']
    assert auth_preflight.check({**settings, 'DASHBOARD_AUTH_MODE': 'none'}) == ['auth_mode']
    # Today's production settings (no auth configured) must never start the new image.
    assert auth_preflight.check({}) != []
    monkeypatch.setattr(auth_preflight.os, 'environ', {})
    assert auth_preflight.main() == 1
    output = capsys.readouterr()
    assert 'FAILED' in output.err and 'placeholder' not in output.err + output.out


def test_output_never_contains_secret_values(env, capsys, monkeypatch):
    settings, path = env
    path.write_text(secrets(auth={'cookie_secret': 'tiny-cookie'}, provider={'client_id': 'wrong-client-value'}), encoding='utf-8')
    monkeypatch.setattr(auth_preflight.os, 'environ', settings)
    assert auth_preflight.main() == 1
    printed = ''.join(capsys.readouterr())
    for value in ('tiny-cookie', 'wrong-client-value', 'placeholder-not-a-secret', CLIENT):
        assert value not in printed


# --- Admin destination review -----------------------------------------------------

def test_review_request_is_get_only_for_one_tenant(monkeypatch):
    monkeypatch.setenv('SUBSCRIPTION_API_ORIGIN', 'https://subscription.example')
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = SimpleNamespace(status=200, read=lambda n: b'{"contract":"x"}')
    request_access('GET', PENDING_DESTINATIONS, 'token', opener=opener, tenant_id='TENANT-01')
    sent = opener.open.call_args.args[0]
    assert sent.get_method() == 'GET' and sent.full_url.endswith(PENDING_DESTINATIONS + '?tenant_id=TENANT-01')
    for call in (dict(method='POST'), dict(tenant_id=None), dict(tenant_id='T&x=1'), dict(payload={'approve': True})):
        args = dict(method='GET', tenant_id='TENANT-01', payload=None) | call
        with pytest.raises(AccessAPIUnavailable):
            request_access(args['method'], PENDING_DESTINATIONS, 'token', args['payload'], opener=opener, tenant_id=args['tenant_id'])
    with pytest.raises(AccessAPIUnavailable):
        request_access('GET', '/api/v1/access/me', 'token', opener=opener, tenant_id='TENANT-01')


def review(**record_changes):
    record = {'id': 'telegram:77', 'tenant_id': 'TENANT-01', 'channel': 'telegram', 'telegram_chat_id': '77', 'enabled': True}
    record.update(record_changes)
    return {'contract': 'destination-review-v1', 'tenant_id': 'TENANT-01',
            'items': [{'object_id': '00000000-0000-4000-8000-000000000101', 'role': 'SOC Viewer',
                       'verified_at': 'x', 'chat_hint': '…77', 'approval_record': record}]}


def test_review_response_is_validated():
    assert len(admin_access.validated_review(review(), 'TENANT-01')) == 1
    for bad in (review(tenant_id='TENANT-02'), review(enabled=False), review(id='telegram:78'),
                review(extra='x'), review(telegram_chat_id='abc', id='telegram:abc'), {**review(), 'contract': 'other'}):
        with pytest.raises((AccessAPIUnavailable, KeyError, ValueError)):
            admin_access.validated_review(bad, 'TENANT-01')
    with pytest.raises(AccessAPIUnavailable):
        admin_access.validated_review(review(), 'TENANT-02')


def test_review_section_is_disabled_without_onboarding_and_makes_no_request(monkeypatch):
    monkeypatch.delenv('ENABLE_TELEGRAM_ONBOARDING', raising=False)
    calls = []
    monkeypatch.setattr(admin_access, 'request_access', lambda *a, **k: calls.append(a))
    fake = MagicMock()
    monkeypatch.setattr(admin_access, 'st', fake)
    admin_access.render_destination_review(SimpleNamespace(tenants=('TENANT-01',)), lambda: None, lambda: 'token')
    assert calls == [] and fake.info.called and not fake.button.called
