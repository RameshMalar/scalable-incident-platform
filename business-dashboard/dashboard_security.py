"""Entra authentication and server-controlled platform-tenant authorization.

Streamlit OIDC supplies an ID token to the server (Easy Auth is an explicit
alternative). Verify its signature ourselves; neither an unsigned principal
header nor Streamlit session state grants access.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import UUID

import jwt
from access_roles import BOOTSTRAP_ADMIN_ID, Role


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class OperatorContext:
    user_id: str
    directory_id: str
    tenants: tuple[str, ...]
    permissions: frozenset[str]
    expires_at: int
    role: str = ''

    @property
    def can_simulate(self) -> bool:
        return 'Incidents.Simulate' in self.permissions

    @property
    def can_manage_access(self) -> bool:
        return self.role == Role.PLATFORM_ADMIN and 'Access.Manage' in self.permissions

    @property
    def is_admin(self) -> bool:
        return 'Dashboard.Admin' in self.permissions or self.role == Role.PLATFORM_ADMIN

    @property
    def can_export(self) -> bool:
        return 'Dashboard.Export' in self.permissions or self.is_admin


# Local sign-in roles. Admin-only capabilities are listed here, server-side,
# and checked again inside every page that uses them.
LOCAL_ROLE_PERMISSIONS = {
    'admin': frozenset({'Dashboard.Read', 'Dashboard.Export', 'Dashboard.Admin', 'Incidents.Simulate'}),
    'viewer': frozenset({'Dashboard.Read'}),
}
LOCAL_ROLE_NAMES = {'admin': 'Admin', 'viewer': 'Viewer'}


@dataclass(frozen=True)
class VerifiedIdentity:
    user_id: str
    directory_id: str
    expires_at: int
    signed_roles: tuple[str, ...]


class OperatorVerifier:
    def __init__(self, directory_id, client_id, user_tenants, keys=None, user_permissions=None):
        UUID(directory_id)
        UUID(client_id)
        if not isinstance(user_tenants, dict) or not user_tenants:
            raise ValueError('An explicit operator-to-tenant mapping is required')
        for user, tenants in user_tenants.items():
            UUID(user)
            if (not isinstance(tenants, list) or not tenants
                    or any(not isinstance(t, str) or not t.strip() or t != t.strip() for t in tenants)):
                raise ValueError('Invalid operator tenant mapping')
        self.directory_id = directory_id
        self.client_id = client_id
        self.user_tenants = user_tenants
        self.user_permissions = user_permissions or {user: ['Dashboard.Read'] for user in user_tenants}
        self.issuers = [f'https://login.microsoftonline.com/{directory_id}/v2.0', f'https://sts.windows.net/{directory_id}/']
        self.keys = keys or jwt.PyJWKClient(f'https://login.microsoftonline.com/{directory_id}/discovery/v2.0/keys', cache_keys=True)

    @classmethod
    def from_env(cls):
        # The reviewed policy is packaged with the app, not submitted by a user.
        policy = json.loads(Path(__file__).with_name('access-policy.json').read_text(encoding='utf-8'))
        return cls(policy['directory_id'], os.environ.get('DASHBOARD_AUTH_CLIENT_ID', ''),
                   {user: entry['tenants'] for user, entry in policy['users'].items()},
                   user_permissions={user: entry['permissions'] for user, entry in policy['users'].items()})

    @property
    def approved_tenants(self):
        return frozenset(tenant for tenants in self.user_tenants.values() for tenant in tenants)

    def verify_identity(self, token: str) -> VerifiedIdentity:
        if not isinstance(token, str) or not token or len(token) > 65536:
            raise AccessDenied('A signed operator identity is required')
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'],
                                audience=self.client_id, issuer=self.issuers,
                                options={'require': ['exp', 'iss', 'aud', 'tid', 'oid']})
            user = claims['oid']
            roles = claims.get('roles', [])
            if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
                raise AccessDenied('Operator access denied')
            UUID(user)
            if claims['tid'] != self.directory_id or claims.get('idtyp') == 'app':
                raise AccessDenied('Operator access denied')
            return VerifiedIdentity(user,claims['tid'],int(claims['exp']),tuple(roles))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, RecursionError):
            raise AccessDenied('Invalid or expired operator identity') from None

    def verify(self, token: str) -> OperatorContext:
        """Bootstrap-only mode; it never registers team members."""
        identity=self.verify_identity(token)
        permissions=frozenset(identity.signed_roles) & frozenset(self.user_permissions.get(identity.user_id,[]))
        if identity.user_id not in self.user_tenants or 'Dashboard.Read' not in permissions:
            raise AccessDenied('Operator access denied')
        if identity.user_id == BOOTSTRAP_ADMIN_ID:
            role=Role.PLATFORM_ADMIN
            permissions=permissions | {'Access.Manage'}
        else:
            role=Role.SOC_OPERATOR if 'Incidents.Simulate' in permissions else Role.SOC_VIEWER
        return OperatorContext(identity.user_id,identity.directory_id,tuple(sorted(set(self.user_tenants[identity.user_id]))),permissions,identity.expires_at,role.value)


def id_token(headers, tokens=None) -> str:
    if tokens is not None:
        return tokens.get('id', '')
    # Header names are case insensitive in HTTP; no tokens are returned to UI.
    values = {str(key).lower(): value for key, value in headers.items()}
    return values.get('x-ms-token-aad-id-token', '')


def access_token(headers, tokens=None) -> str:
    values = {str(key).lower(): value for key, value in headers.items()}
    token = tokens.get('access', '') if tokens is not None else values.get('x-ms-token-aad-access-token', '')
    if not isinstance(token, str) or not token or len(token) > 65536 or '\r' in token or '\n' in token:
        raise AccessDenied('A delegated API token is required')
    return token
