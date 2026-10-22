"""Records kept in the reserved platform partition of the subscriptions container.

Access grants, their audit trail, identity receipts and Telegram ownership all
live here, beside -- never inside -- tenant subscription partitions. No new
container or Azure resource is created: the Subscription API identity already
holds data-plane contributor on this container. Tenant routes cannot reach the
partition because app.auth refuses to bind a client to a reserved tenant.

Every multi-record change is one transactional batch inside the partition, with
ETag preconditions, so a concurrent writer is rejected rather than overwritten.
"""

import itertools
from abc import ABC, abstractmethod

from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

ACCESS_PARTITION = "__platform_access__"


class DocumentConflict(Exception):
    """A precondition failed: the record changed, exists, or is gone."""


class DocumentsUnavailable(Exception):
    """The partition could not be read or written; never treat as absent."""


def public(document: dict) -> dict:
    return {key: value for key, value in document.items() if not key.startswith("_")}


class PlatformDocuments(ABC):
    async def start(self) -> None:
        pass

    @abstractmethod
    async def read(self, document_id: str) -> dict | None:
        """Return the record with its _etag, or None when it does not exist."""

    @abstractmethod
    async def query(self, record_type: str, field: str | None = None, value=None) -> list[dict]:
        pass

    @abstractmethod
    async def batch(self, operations: list[tuple]) -> None:
        """Apply ("create", doc), ("replace", doc, etag), ("delete", id, etag) atomically."""


class InMemoryPlatformDocuments(PlatformDocuments):
    def __init__(self):
        self.items: dict[str, dict] = {}
        self._etags = itertools.count(1)
        self.fail = False

    def _check(self):
        if self.fail:
            raise DocumentsUnavailable("platform records unavailable")

    async def read(self, document_id):
        self._check()
        item = self.items.get(document_id)
        return dict(item) if item is not None else None

    async def query(self, record_type, field=None, value=None):
        self._check()
        return [dict(item) for _, item in sorted(self.items.items())
                if item.get("record_type") == record_type and (field is None or item.get(field) == value)]

    async def batch(self, operations):
        self._check()
        staged = dict(self.items)
        for operation in operations:
            kind = operation[0]
            if kind == "create":
                document = operation[1]
                if document["id"] in staged:
                    raise DocumentConflict("record already exists")
                staged[document["id"]] = {**public(document), "_etag": str(next(self._etags))}
            elif kind == "replace":
                document, etag = operation[1], operation[2]
                current = staged.get(document["id"])
                if current is None or current["_etag"] != etag:
                    raise DocumentConflict("record changed")
                staged[document["id"]] = {**public(document), "_etag": str(next(self._etags))}
            elif kind == "delete":
                current = staged.get(operation[1])
                if current is None or current["_etag"] != operation[2]:
                    raise DocumentConflict("record changed")
                del staged[operation[1]]
            else:
                raise ValueError("unsupported batch operation")
        self.items = staged


class CosmosPlatformDocuments(PlatformDocuments):
    """Shares the Subscription store's container client; opens nothing new."""

    def __init__(self, subscription_store):
        self.subscription_store = subscription_store

    @property
    def container(self):
        container = self.subscription_store.container
        if container is None:
            raise DocumentsUnavailable("platform records unavailable")
        return container

    async def read(self, document_id):
        container = self.container
        try:
            return await container.read_item(item=document_id, partition_key=ACCESS_PARTITION)
        except CosmosResourceNotFoundError:
            # An item 404 and a missing container share one SDK exception. A
            # missing or mis-partitioned container must never read as "no
            # record" -- for the bootstrap Admin that would look like a grant.
            metadata = await container.read()
            if metadata.get("partitionKey", {}).get("paths") != ["/tenant_id"]:
                raise DocumentsUnavailable("invalid platform partition") from None
            return None

    async def query(self, record_type, field=None, value=None):
        text = "SELECT * FROM c WHERE c.record_type = @type"
        parameters = [{"name": "@type", "value": record_type}]
        if field is not None:
            if not field.isidentifier():
                raise ValueError("invalid field")
            text += f" AND c.{field} = @value"
            parameters.append({"name": "@value", "value": value})
        rows = self.container.query_items(query=text + " ORDER BY c.id", parameters=parameters, partition_key=ACCESS_PARTITION)
        return [row async for row in rows]

    async def batch(self, operations):
        translated = []
        for operation in operations:
            if operation[0] == "create":
                translated.append(("create", (public(operation[1]),)))
            elif operation[0] == "replace":
                translated.append(("replace", (operation[1]["id"], public(operation[1])), {"if_match_etag": operation[2]}))
            elif operation[0] == "delete":
                translated.append(("delete", (operation[1],), {"if_match_etag": operation[2]}))
            else:
                raise ValueError("unsupported batch operation")
        try:
            await self.container.execute_item_batch(batch_operations=translated, partition_key=ACCESS_PARTITION)
        except CosmosBatchOperationError as error:
            statuses = {result.get("statusCode") for result in error.operation_responses or []}
            if statuses & {404, 409, 412}:
                raise DocumentConflict("record changed") from None
            raise
