"""Canonical application roles; these are not Azure resource-management roles."""
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

BOOTSTRAP_ADMIN_ID = 'ded95a62-f8b6-4589-b49a-2041a35d10d7'


class Role(StrEnum):
    PLATFORM_ADMIN = 'Platform Admin'
    SOC_OPERATOR = 'SOC Operator'
    SOC_VIEWER = 'SOC Viewer'


ROLE_PERMISSIONS = {
    Role.PLATFORM_ADMIN: frozenset({'Dashboard.Read', 'Incidents.Simulate', 'Access.Manage', 'Subscriptions.SelfManage'}),
    Role.SOC_OPERATOR: frozenset({'Dashboard.Read', 'Incidents.Simulate', 'Subscriptions.SelfManage'}),
    Role.SOC_VIEWER: frozenset({'Dashboard.Read', 'Subscriptions.SelfManage'}),
}


class InvalidAccess(Exception):
    pass


@dataclass(frozen=True)
class AccessGrant:
    directory_id: str
    object_id: str
    role: Role
    tenants: tuple[str, ...]
    status: str
    version: int

    @property
    def permissions(self):
        return ROLE_PERMISSIONS[self.role] if self.status == 'active' else frozenset()

    def public(self):
        return dict(directory_id=self.directory_id, object_id=self.object_id, role=self.role.value,
                    tenants=list(self.tenants), status=self.status, version=self.version)


def parse_grant(value, directory_id, approved_tenants, *, expected_user=None, active=True):
    """Validate a trusted registry response; never grant from UI session state."""
    try:
        if not isinstance(value, dict) or value.get('directory_id') != directory_id:
            raise InvalidAccess('Invalid directory')
        user = value['object_id']
        UUID(user)
        if expected_user is not None and user != expected_user:
            raise InvalidAccess('Wrong subject')
        tenants = value['tenants']
        if (not isinstance(tenants, list) or not tenants or any(not isinstance(t,str) for t in tenants)
                or len(set(tenants)) != len(tenants) or not set(tenants).issubset(approved_tenants)):
            raise InvalidAccess('Unapproved platform tenant')
        status = value['status']
        if status not in ('active','disabled','revoked') or (active and status != 'active'):
            raise InvalidAccess('Access is inactive')
        version = value['version']
        if type(version) is not int or version < 1:
            raise InvalidAccess('Invalid access version')
        role = Role(value['role'])
        if user == BOOTSTRAP_ADMIN_ID and (role != Role.PLATFORM_ADMIN or status != 'active' or set(tenants) != set(approved_tenants)):
            raise InvalidAccess('Approved Platform Admin mapping must remain unchanged')
        return AccessGrant(directory_id,user,role,tuple(sorted(tenants)),status,version)
    except (KeyError,TypeError,ValueError):
        raise InvalidAccess('Malformed access record') from None
