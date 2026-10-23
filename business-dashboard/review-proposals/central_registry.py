"""Proposed Subscription API domain component, not a new service or registry.

Not wired into shared backend services without the required owner approval.
Authenticated routes must supply actor IDs from validated Entra delegated tokens.
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from access_roles import BOOTSTRAP_ADMIN_ID, AccessGrant, InvalidAccess, Role, parse_grant

ACCESS_PARTITION='__platform_access__'


class RegistryDenied(Exception):pass
class RegistryConflict(Exception):pass
class RegistryUnavailable(Exception):pass


class CentralRegistry:
    def __init__(self,store,bootstrap_policy,identity_proofs=None):
        self.store=store
        self.identity_proofs=identity_proofs
        self.directory=bootstrap_policy['directory_id']
        approved=bootstrap_policy['users']
        if set(approved)!={BOOTSTRAP_ADMIN_ID}:
            raise ValueError('Only the existing approved bootstrap Admin may be seeded')
        self.tenants=frozenset(approved[BOOTSTRAP_ADMIN_ID]['tenants'])
        self.root=AccessGrant(self.directory,BOOTSTRAP_ADMIN_ID,Role.PLATFORM_ADMIN,tuple(sorted(self.tenants)),'active',1)

    async def resolve(self,object_id,directory_id):
        if directory_id!=self.directory:raise RegistryDenied('Directory denied')
        try:
            UUID(object_id)
            record=await self.store.get(object_id)
        except (ValueError,TypeError):raise RegistryDenied('Invalid subject') from None
        except Exception:raise RegistryUnavailable('Access registry unavailable') from None
        # Bootstrap is an already approved identity, not auto-registration of a
        # new team member. Even this branch waits for the registry read to succeed.
        if object_id==BOOTSTRAP_ADMIN_ID and record is None:return self.root
        if record is None:raise RegistryDenied('Unregistered identity')
        if object_id!=BOOTSTRAP_ADMIN_ID and (record.get('approved_by')!=BOOTSTRAP_ADMIN_ID or not record.get('approval_reference')):
            raise RegistryDenied('Explicit Platform Admin approval is required')
        try:return parse_grant(record,self.directory,self.tenants,expected_user=object_id)
        except InvalidAccess:raise RegistryDenied('Access denied') from None

    async def authorize(self,object_id,directory_id,permission,tenant=None):
        grant=await self.resolve(object_id,directory_id)
        if permission not in grant.permissions or (tenant is not None and tenant not in grant.tenants):
            raise RegistryDenied('Permission or tenant denied')
        return grant

    async def _admin(self,actor,directory):
        return await self.authorize(actor,directory,'Access.Manage')

    async def users(self,actor,directory,cursor=None,limit=50):
        admin=await self._admin(actor,directory)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Invalid page size')
        if cursor is not None:UUID(cursor)
        try:records=await self.store.list()
        except Exception:raise RegistryUnavailable('Access registry unavailable') from None
        records=[record for record in records if record['object_id']!=BOOTSTRAP_ADMIN_ID]+[self.root.public()]
        visible=[]
        for record in records:
            try:grant=parse_grant(record,self.directory,self.tenants,active=False)
            except InvalidAccess:raise RegistryUnavailable('Invalid access registry state') from None
            if set(grant.tenants).issubset(admin.tenants) and (cursor is None or grant.object_id>cursor):visible.append(grant)
        visible.sort(key=lambda grant:grant.object_id)
        page=visible[:limit]
        return dict(contract='central-access-v1',items=[grant.public() for grant in page],
                    next_cursor=page[-1].object_id if len(visible)>limit else None)

    async def register(self,actor,directory,object_id,role,tenants,approval_reference):
        admin=await self._admin(actor,directory)
        if actor!=BOOTSTRAP_ADMIN_ID:raise RegistryDenied('Original Platform Admin approval required')
        if object_id==BOOTSTRAP_ADMIN_ID:raise RegistryDenied('Approved Platform Admin is immutable')
        # UUID syntax is not proof that an Entra user exists. The API records
        # a short-lived non-grant receipt only after validating that user's
        # delegated Entra token. No Graph/Azure role is required for team users.
        if self.identity_proofs is None or not await self.identity_proofs.verified(object_id,self.directory):
            raise RegistryDenied('A verified Entra user identity receipt is required')
        if not isinstance(approval_reference,str) or not approval_reference.strip() or len(approval_reference)>500:
            raise ValueError('Explicit approval reference required')
        after=dict(directory_id=self.directory,object_id=object_id,role=Role(role).value,
                   tenants=list(tenants),status='active',version=1,
                   approved_by=actor,approval_reference=approval_reference.strip())
        parse_grant(after,self.directory,self.tenants)
        if not set(tenants).issubset(admin.tenants):raise RegistryDenied('Cannot grant another tenant')
        if await self.store.get(object_id) is not None:raise RegistryConflict('Already registered')
        await self._commit(actor,None,after,'register',approval_reference)
        return parse_grant(after,self.directory,self.tenants)

    async def change(self,actor,directory,object_id,expected_version,changes,reason):
        admin=await self._admin(actor,directory)
        if object_id==BOOTSTRAP_ADMIN_ID:raise RegistryDenied('Approved Platform Admin is immutable')
        if not changes or set(changes)-{'role','tenants','status'}:raise ValueError('Unsupported access change')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500:raise ValueError('Reason required')
        before=await self.store.get(object_id)
        if before is None:raise RegistryDenied('Unknown user')
        if type(expected_version) is not int or before['version']!=expected_version:raise RegistryConflict('Refresh access version')
        old=parse_grant(before,self.directory,self.tenants,active=False)
        if not set(old.tenants).issubset(admin.tenants):raise RegistryDenied('Cannot manage another tenant')
        after={**before,**changes,'version':expected_version+1}
        new=parse_grant(after,self.directory,self.tenants,active=False)
        if not set(new.tenants).issubset(admin.tenants):raise RegistryDenied('Cannot grant another tenant')
        if old.status=='revoked' and new.status!='revoked':raise RegistryDenied('Revocation cannot be silently reversed')
        expansion=(not new.permissions.issubset(old.permissions) or not set(new.tenants).issubset(old.tenants))
        if expansion and actor!=BOOTSTRAP_ADMIN_ID:raise RegistryDenied('Original Platform Admin approval required')
        await self._commit(actor,before,after,'change',reason)
        return new

    async def _commit(self,actor,before,after,action,reason):
        now=datetime.now(timezone.utc).isoformat()
        after.update(id='access:'+after['object_id'],tenant_id=ACCESS_PARTITION,record_type='platform_access',updated_at=now)
        audit=dict(id='access-audit:'+str(uuid4()),tenant_id=ACCESS_PARTITION,record_type='access_audit',
                   actor_object_id=actor,target_object_id=after['object_id'],action=action,reason=reason,
                   before=None if before is None else parse_grant(before,self.directory,self.tenants,active=False).public(),
                   after=parse_grant(after,self.directory,self.tenants,active=False).public(),timestamp=now)
        try:await self.store.commit(before,after,audit)
        except RegistryConflict:raise
        except Exception:raise RegistryUnavailable('Access mutation was not confirmed; refresh before retrying') from None


class CosmosAccessStore:
    """Uses an existing Subscription container; creates no Azure resources."""
    def __init__(self,container):self.container=container

    async def get(self,object_id):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:return await self.container.read_item(item='access:'+object_id,partition_key=ACCESS_PARTITION)
        except CosmosResourceNotFoundError:
            # Parent-container 404 must never become a bootstrap allow.
            metadata=await self.container.read()
            if metadata.get('partitionKey',{}).get('paths')!=['/tenant_id']:
                raise RegistryUnavailable('Invalid access registry partition')
            return None

    async def list(self):
        return [row async for row in self.container.query_items(
            query="SELECT * FROM c WHERE c.record_type = 'platform_access' ORDER BY c.id",partition_key=ACCESS_PARTITION)]

    async def commit(self,before,after,audit):
        from azure.cosmos.exceptions import CosmosBatchOperationError
        first=('create',(after,)) if before is None else ('replace',(after['id'],after),{'if_match_etag':before['_etag']})
        try:
            await self.container.execute_item_batch(batch_operations=[first,('create',(audit,))],partition_key=ACCESS_PARTITION)
        except CosmosBatchOperationError as error:
            statuses=[result.get('statusCode') for result in error.operation_responses]
            if 409 in statuses or 412 in statuses:raise RegistryConflict('Access record changed; refresh') from None
            raise
