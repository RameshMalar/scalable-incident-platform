"""Read subscriptions directly from Cosmos; no HTTP or notification publishing."""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from pydantic import ValidationError

from app.notification_models import InvalidSubscription, SubscriptionRecord
from app.owner_eligibility import ACCESS_PARTITION


logger = logging.getLogger("app.subscription_store")

MATCH_QUERY = (
    "SELECT c.id, c.tenant_id, c.event_type, c.channel, "
    "c.telegram_chat_id, c.enabled, c.owner_object_id FROM c "
    "WHERE c.tenant_id = @tenant AND c.event_type = @event_type "
    "AND c.enabled = true AND c.channel = 'telegram'"
)


def validate_subscription(document: Any) -> SubscriptionRecord:
    try:
        return SubscriptionRecord.model_validate(document)
    except ValidationError:
        # Do not expose raw stored configuration in error messages/logs.
        raise InvalidSubscription("invalid subscription routing fields") from None


def matches(record: SubscriptionRecord, tenant_id: str, event_type: str) -> bool:
    return (
        record.tenant_id == tenant_id
        and record.event_type == event_type
        and record.enabled is True
        and record.channel == "telegram"
    )


class SubscriptionReader(ABC):
    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @abstractmethod
    async def find_matches(
        self, tenant_id: str, event_type: str
    ) -> list[SubscriptionRecord]:
        """Return matches or raise; outages must never become an empty list."""

    async def read_platform_record(self, document_id: str) -> dict | None:
        """Read an access or Telegram ownership record; None only if absent."""
        raise NotImplementedError("this reader cannot check account-owned subscriptions")


class CosmosSubscriptionReader(SubscriptionReader):
    def __init__(
        self,
        endpoint: str | None = None,
        database_name: str | None = None,
        container_name: str | None = None,
        credential=None,
        client_factory=None,
    ):
        self.endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT")
        self.database_name = database_name or os.environ.get(
            "COSMOS_DATABASE", "incidents"
        )
        self.container_name = container_name or os.environ.get(
            "COSMOS_SUBSCRIPTIONS_CONTAINER", "subscriptions"
        )
        if not self.endpoint:
            raise ValueError("COSMOS_ENDPOINT is required for subscription reading")
        self._credential = credential
        self._owns_credential = credential is None
        self._client_factory = client_factory or CosmosClient
        self._client = None
        self._container = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._container is not None:
                return
            if self._credential is None:
                self._credential = DefaultAzureCredential()
            self._client = self._client_factory(
                self.endpoint, credential=self._credential
            )
            self._container = self._client.get_database_client(
                self.database_name
            ).get_container_client(self.container_name)

    async def find_matches(
        self, tenant_id: str, event_type: str
    ) -> list[SubscriptionRecord]:
        await self.start()
        rows = self._container.query_items(
            query=MATCH_QUERY,
            partition_key=tenant_id,
            parameters=[
                {"name": "@tenant", "value": tenant_id},
                {"name": "@event_type", "value": event_type},
            ],
        )
        records = []
        # AsyncItemPaged iteration follows continuation tokens through every
        # page. There is no API offset/limit and no first-page shortcut here.
        async for document in rows:
            try:
                record = validate_subscription(document)
            except InvalidSubscription:
                logger.warning("skipping malformed subscription routing record")
                continue
            if record.tenant_id != tenant_id:
                logger.warning("ignoring subscription from another tenant partition")
                continue
            if matches(record, tenant_id, event_type):
                records.append(record)
        return records

    async def read_platform_record(self, document_id: str) -> dict | None:
        await self.start()
        try:
            return await self._container.read_item(
                item=document_id, partition_key=ACCESS_PARTITION
            )
        except CosmosResourceNotFoundError:
            # A missing container raises the same exception as a missing
            # item; only a confirmed container makes "absent" trustworthy.
            metadata = await self._container.read()
            if metadata.get("partitionKey", {}).get("paths") != ["/tenant_id"]:
                raise
            return None

    async def close(self) -> None:
        try:
            if self._client is not None:
                await self._client.close()
        finally:
            self._client = self._container = None
            if self._owns_credential and self._credential is not None:
                try:
                    await self._credential.close()
                finally:
                    self._credential = None


class InMemorySubscriptionReader(SubscriptionReader):
    """Injected local/test configuration; not selected by the current worker."""

    def __init__(
        self,
        documents: Iterable[Mapping[str, Any]] = (),
        platform_records: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self._records = [validate_subscription(document) for document in documents]
        self.platform_records = dict(platform_records or {})

    async def read_platform_record(self, document_id: str) -> dict | None:
        record = self.platform_records.get(document_id)
        return dict(record) if record is not None else None

    async def find_matches(
        self, tenant_id: str, event_type: str
    ) -> list[SubscriptionRecord]:
        return [
            record for record in self._records
            if matches(record, tenant_id, event_type)
        ]
