"""Central application access registry (contract central-access-v1).

The one source of application roles and tenant assignments for the dashboard,
the simulator and Telegram self-service. Roles come from app.access_roles,
which is byte-identical to the dashboard's copy (tests enforce it). These are
application permissions only; no Azure or Entra role is created for anyone.

Reviewed from business-dashboard/review-proposals/central_registry.py.
"""

import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.access_roles import BOOTSTRAP_ADMIN_ID, AccessGrant, InvalidAccess, Role, parse_grant
from app.platform_documents import ACCESS_PARTITION, DocumentConflict, public

CONTRACT = "central-access-v1"
IDENTITY_RECEIPT_SECONDS = 900
MAX_REASON = 500


class RegistryDenied(Exception):
    pass


class RegistryConflict(Exception):
    pass


class RegistryUnavailable(Exception):
    pass


def access_id(object_id: str) -> str:
    return "access:" + object_id


def receipt_id(object_id: str) -> str:
    return "identity-receipt:" + object_id


def grant_response(grant: AccessGrant) -> dict:
    # permissions are derived here, server side, so consumers such as the
    # ingestion simulator never need a role map of their own.
    return {**grant.public(), "permissions": sorted(grant.permissions), "contract": CONTRACT}


class AccessRegistry:
    def __init__(self, documents, directory_id: str, tenants, clock=time.time):
        UUID(directory_id)
        tenants = frozenset(tenants)
        if not tenants or any(not tenant or tenant.startswith("__") for tenant in tenants):
            raise ValueError("ACCESS_PLATFORM_TENANTS must list the approved platform tenants")
        self.documents = documents
        self.directory = directory_id
        self.tenants = tenants
        self.clock = clock
        self.root = AccessGrant(directory_id, BOOTSTRAP_ADMIN_ID, Role.PLATFORM_ADMIN, tuple(sorted(tenants)), "active", 1)

    def _parse(self, record, **kwargs):
        return parse_grant(record, self.directory, self.tenants, **kwargs)

    async def _read(self, document_id):
        try:
            return await self.documents.read(document_id)
        except Exception:
            raise RegistryUnavailable("access registry unavailable") from None

    async def _seed_root(self):
        # The already approved Admin is stored the first time it is resolved,
        # so Worker and Notification eligibility read one record for everyone.
        # It is the only identity ever written without an explicit approval.
        record = {**self.root.public(), "id": access_id(BOOTSTRAP_ADMIN_ID), "tenant_id": ACCESS_PARTITION,
                  "record_type": "platform_access", "updated_at": datetime.now(timezone.utc).isoformat()}
        try:
            await self.documents.batch([("create", record)])
        except DocumentConflict:
            pass
        except Exception:
            raise RegistryUnavailable("access registry unavailable") from None
        return await self._read(access_id(BOOTSTRAP_ADMIN_ID))

    async def resolve(self, object_id: str) -> AccessGrant:
        try:
            UUID(object_id)
        except (ValueError, TypeError, AttributeError):
            raise RegistryDenied("invalid subject") from None
        record = await self._read(access_id(object_id))
        if record is None and object_id == BOOTSTRAP_ADMIN_ID:
            record = await self._seed_root()
        if record is None:
            raise RegistryDenied("unregistered identity")
        if object_id != BOOTSTRAP_ADMIN_ID and (record.get("approved_by") != BOOTSTRAP_ADMIN_ID or not record.get("approval_reference")):
            raise RegistryDenied("explicit Platform Admin approval is required")
        try:
            return self._parse(record, expected_user=object_id)
        except InvalidAccess:
            raise RegistryDenied("access denied") from None

    async def authorize(self, object_id: str, permission: str, tenant: str | None = None) -> AccessGrant:
        grant = await self.resolve(object_id)
        if permission not in grant.permissions or (tenant is not None and tenant not in grant.tenants):
            raise RegistryDenied("permission or tenant denied")
        return grant

    async def record_identity_receipt(self, object_id: str) -> dict:
        """Proof that this person signed in; it grants nothing by itself."""
        UUID(object_id)
        expires_at = int(self.clock()) + IDENTITY_RECEIPT_SECONDS
        receipt = {"id": receipt_id(object_id), "tenant_id": ACCESS_PARTITION, "record_type": "identity_receipt",
                   "object_id": object_id, "directory_id": self.directory, "expires_at": expires_at}
        before = await self._read(receipt_id(object_id))
        operation = ("create", receipt) if before is None else ("replace", receipt, before["_etag"])
        try:
            await self.documents.batch([operation])
        except DocumentConflict:
            raise RegistryConflict("identity receipt changed; retry") from None
        except Exception:
            raise RegistryUnavailable("access registry unavailable") from None
        return {"object_id": object_id, "directory_id": self.directory, "expires_at": expires_at}

    async def users(self, actor: str, cursor: str | None = None, limit: int = 50) -> dict:
        admin = await self.authorize(actor, "Access.Manage")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid page size")
        if cursor is not None:
            UUID(cursor)
        try:
            records = await self.documents.query("platform_access")
        except Exception:
            raise RegistryUnavailable("access registry unavailable") from None
        visible = []
        for record in records:
            try:
                grant = self._parse(record, active=False)
            except InvalidAccess:
                raise RegistryUnavailable("invalid access registry state") from None
            if set(grant.tenants).issubset(admin.tenants) and (cursor is None or grant.object_id > cursor):
                visible.append(grant)
        visible.sort(key=lambda grant: grant.object_id)
        page = visible[:limit]
        return {"contract": CONTRACT, "items": [grant.public() for grant in page],
                "next_cursor": page[-1].object_id if len(visible) > limit else None}

    async def register(self, actor, object_id, role, tenants, approval_reference) -> AccessGrant:
        admin = await self.authorize(actor, "Access.Manage")
        if actor != BOOTSTRAP_ADMIN_ID:
            raise RegistryDenied("original Platform Admin approval required")
        UUID(object_id)
        if object_id == BOOTSTRAP_ADMIN_ID:
            raise RegistryDenied("approved Platform Admin is immutable")
        if not isinstance(approval_reference, str) or not approval_reference.strip() or len(approval_reference) > MAX_REASON:
            raise ValueError("explicit approval reference required")
        if not set(tenants).issubset(admin.tenants):
            raise RegistryDenied("cannot grant another tenant")
        # A UUID is not proof that a person exists. Registration consumes the
        # receipt that person's own delegated sign-in recorded.
        receipt = await self._read(receipt_id(object_id))
        if (receipt is None or receipt.get("directory_id") != self.directory
                or type(receipt.get("expires_at")) is not int or receipt["expires_at"] <= self.clock()):
            raise RegistryDenied("a fresh verified Entra identity receipt is required")
        after = {"directory_id": self.directory, "object_id": object_id, "role": Role(role).value,
                 "tenants": list(tenants), "status": "active", "version": 1,
                 "approved_by": actor, "approval_reference": approval_reference.strip()}
        self._parse(after)
        if await self._read(access_id(object_id)) is not None:
            raise RegistryConflict("already registered")
        await self._commit(actor, None, after, "register", approval_reference.strip(),
                           extra=[("delete", receipt["id"], receipt["_etag"])])
        return self._parse(after)

    async def change(self, actor, object_id, expected_version, changes, reason) -> AccessGrant:
        admin = await self.authorize(actor, "Access.Manage")
        UUID(object_id)
        if object_id == BOOTSTRAP_ADMIN_ID:
            raise RegistryDenied("approved Platform Admin is immutable")
        if not changes or set(changes) - {"role", "tenants", "status"}:
            raise ValueError("unsupported access change")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            raise ValueError("reason required")
        before = await self._read(access_id(object_id))
        if before is None:
            raise RegistryDenied("unknown user")
        if type(expected_version) is not int or before.get("version") != expected_version:
            raise RegistryConflict("refresh access version")
        try:
            old = self._parse(before, active=False)
        except InvalidAccess:
            raise RegistryUnavailable("invalid access registry state") from None
        if not set(old.tenants).issubset(admin.tenants):
            raise RegistryDenied("cannot manage another tenant")
        after = {**public(before), **changes, "version": expected_version + 1}
        try:
            new = self._parse(after, active=False)
        except InvalidAccess:
            raise ValueError("invalid access change") from None
        if not set(new.tenants).issubset(admin.tenants):
            raise RegistryDenied("cannot grant another tenant")
        if old.status == "revoked" and new.status != "revoked":
            raise RegistryDenied("revocation cannot be reversed")
        expansion = not new.permissions.issubset(old.permissions) or not set(new.tenants).issubset(old.tenants)
        if expansion and actor != BOOTSTRAP_ADMIN_ID:
            raise RegistryDenied("original Platform Admin approval required")
        await self._commit(actor, before, after, "change", reason.strip())
        return new

    async def _commit(self, actor, before, after, action, reason, extra=()):
        now = datetime.now(timezone.utc).isoformat()
        after.update(id=access_id(after["object_id"]), tenant_id=ACCESS_PARTITION, record_type="platform_access", updated_at=now)
        audit = {"id": "access-audit:" + str(uuid4()), "tenant_id": ACCESS_PARTITION, "record_type": "access_audit",
                 "actor_object_id": actor, "target_object_id": after["object_id"], "action": action, "reason": reason,
                 "before": None if before is None else self._parse(before, active=False).public(),
                 "after": self._parse(after, active=False).public(), "timestamp": now}
        first = ("create", after) if before is None else ("replace", after, before["_etag"])
        try:
            await self.documents.batch([first, ("create", audit), *extra])
        except DocumentConflict:
            raise RegistryConflict("access record changed; refresh") from None
        except Exception:
            raise RegistryUnavailable("access mutation was not confirmed; refresh before retrying") from None
