"""Authenticated account-only UI for the proposed self-service API contract.

No bot token or private chat ID enters the browser. The live API must implement
and verify the contract before the feature is enabled.
"""
import json
import os
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError

import streamlit as st
from dashboard_security import AccessDenied, access_token


class AccountSetupUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_preferences(method, path, token, tenant_id, payload=None, opener=None):
    origin = os.environ.get('SUBSCRIPTION_API_ORIGIN', '')
    parsed = urlparse(origin)
    allowed = ('/api/v1/me/telegram', '/api/v1/me/telegram/challenges', '/api/v1/me/telegram/preferences')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/')
            or parsed.username or parsed.password or parsed.query or parsed.fragment or path not in allowed):
        raise AccountSetupUnavailable('Account setup is not configured')
    if not token or '\r' in token or '\n' in token:
        raise AccountSetupUnavailable('A delegated identity token is required')
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(origin.rstrip('/') + path + '?' + urlencode({'tenant_id': tenant_id}), data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
            value = json.loads(response.read(65536))
            if response.status not in (200, 201) or not isinstance(value, dict):
                raise AccountSetupUnavailable('Account setup is unavailable')
            return value
    except (ValueError, URLError, OSError):
        raise AccountSetupUnavailable('Account setup is unavailable') from None


def render_account_setup(require_operator, get_access_token=None):
    context = require_operator()
    st.subheader('Authenticated Account Setup')
    st.caption('Telegram preferences apply only to your verified account and approved tenant context.')
    if os.environ.get('ENABLE_TELEGRAM_ONBOARDING', '').lower() != 'true':
        st.info('Telegram onboarding is disabled until the ownership-verification and self-service API contracts are approved and verified.')
        return
    try:
        tenant_id = st.selectbox('Account tenant', context.tenants)
        if tenant_id not in context.tenants:
            raise AccountSetupUnavailable('Platform tenant not authorized')
        token = get_access_token() if get_access_token else access_token(st.context.headers)
        # The self-service API must derive the user from this token; never from
        # a submitted user ID, client-wide tenant binding or arbitrary chat ID.
        status = request_preferences('GET', '/api/v1/me/telegram', token, tenant_id)
        if (status.get('contract') != 'self-service-v1' or status.get('tenant_id') != tenant_id or type(status.get('verified')) is not bool
                or type(status.get('enabled')) is not bool
                or not isinstance(status.get('supported_event_types'), list)
                or not all(isinstance(item, str) for item in status['supported_event_types'])):
            raise AccountSetupUnavailable('Self-service authorization has not been verified')
        if not status['verified']:
            st.info('Verify ownership in your private Telegram conversation before opting in.')
            if st.button('Link my Telegram account'):
                challenge = request_preferences('POST', '/api/v1/me/telegram/challenges', token, tenant_id, {})
                link = challenge.get('deep_link', '')
                parsed = urlparse(link)
                if (parsed.scheme != 'https' or parsed.netloc != 't.me' or parsed.fragment
                        or not parsed.path.strip('/') or not parsed.query.startswith('start=')):
                    raise AccountSetupUnavailable('Invalid ownership verification response')
                st.link_button('Verify ownership in Telegram', link)
                st.caption('The one-time link expires. Return here and refresh verification after starting the bot.')
            st.button('Refresh verification')
        with st.form('telegram_account_preferences'):
            enabled = st.checkbox('Opt in to Telegram incident alerts', value=status['enabled'])
            events = st.multiselect('Eligible event types', status['supported_event_types'],
                default=[value for value in status.get('event_types', []) if value in status['supported_event_types']])
            save = st.form_submit_button('Save account preferences', disabled=not status['verified'] and not status['enabled'])
        if save:
            require_operator()  # Recheck identity expiry immediately before writes.
            if enabled and not status['verified']:
                raise AccountSetupUnavailable('Ownership verification is required before enabling alerts')
            result = request_preferences('PUT', '/api/v1/me/telegram/preferences', token, tenant_id,
                {'enabled': enabled, 'event_types': events})
            if (result.get('contract') != 'self-service-v1' or result.get('enabled') is not enabled
                    or result.get('tenant_id') != tenant_id):
                raise AccountSetupUnavailable('Preference update could not be confirmed')
            st.success('Account preferences saved.' if enabled else 'Telegram alerts disabled for your account.')
    except (AccountSetupUnavailable, AccessDenied, ValueError, TypeError, KeyError):
        st.error('Account setup is unavailable. No subscription change has been confirmed.')
