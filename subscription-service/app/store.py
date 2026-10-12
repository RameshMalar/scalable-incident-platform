"""Storage boundary. Every operation requires an explicit tenant partition."""

import asyncio
import os
from abc import ABC, abstractmethod

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

from app.models import Subscription


class SubscriptionStore(ABC):
    mode = "abstract"

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @abstractmethod
    async def ready(self) -> bool:
        pass

    @abstractmethod
    async def create(self, subscription: Subscription) -> Subscription:
        pass

    @abstractmethod
    async def list(self, tenant_id: str, limit: int, offset: int) -> list[Subscription]:
        pass

    @abstractmethod
    async def get(self, tenant_id: str, subscription_id: str) -> Subscription | None:
        pass

    @abstractmethod
    async def replace(self, subscription: Subscription) -> Subscription | None:
        pass

    @abstractmethod
    async def delete(self, tenant_id: str, subscription_id: str) -> bool:
        pass


class InMemorySubscriptionStore(SubscriptionStore):
    mode = "memory"

    def __init__(self):
        self.items: dict[tuple[str, str], Subscription] = {}

    async def ready(self) -> bool:
        return True

    async def create(self, subscription: Subscription) -> Subscription:
        key = (subscription.tenant_id, subscription.id)
        if key in self.items:
            raise ValueError("subscription already exists")
        self.items[key] = subscription.model_copy(deep=True)
        return subscription

    async def list(self, tenant_id: str, limit: int, offset: int) -> list[Subscription]:
        items = sorted((item for (tenant, _), item in self.items.items() if tenant == tenant_id), key=lambda item: item.id)
        return [item.model_copy(deep=True) for item in items[offset:offset + limit]]

    async def get(self, tenant_id: str, subscription_id: str) -> Subscription | None:
        item = self.items.get((tenant_id, subscription_id))
        return item.model_copy(deep=True) if item else None

    async def replace(self, subscription: Subscription) -> Subscription | None:
        key = (subscription.tenant_id, subscription.id)
        if key not in self.items:
            return None
        self.items[key] = subscription.model_copy(deep=True)
        return subscription

    async def delete(self, tenant_id: str, subscription_id: str) -> bool:
        return self.items.pop((tenant_id, subscription_id), None) is not None


class CosmosSubscriptionStore(SubscriptionStore):
    mode = "cosmos"

    def __init__(self, endpoint: str, database: str = "incidents", container: str = "subscriptions", credential=None, client_factory=CosmosClient):
        if not endpoint:
            raise ValueError("COSMOS_ENDPOINT is required")
        self.endpoint, self.database, self.container_name = endpoint, database, container
        self.credential = credential
        self.owns_credential = credential is None
        self.client_factory = client_factory
        self.client = None
        self.container = None

    async def start(self) -> None:
        if self.client is None:
            self.credential = self.credential or DefaultAzureCredential()
            self.client = self.client_factory(self.endpoint, credential=self.credential)
            self.container = self.client.get_database_client(self.database).get_container_client(self.container_name)

    async def ready(self) -> bool:
        async def check():
            await self.container.read()
            async for _ in self.container.query_items("SELECT TOP 1 c.id FROM c", partition_key="__readiness__"):
                break
        await asyncio.wait_for(check(), timeout=5)
        return True

    async def create(self, subscription: Subscription) -> Subscription:
        await self.container.create_item(subscription.model_dump(mode="json"))
        return subscription

    async def list(self, tenant_id: str, limit: int, offset: int) -> list[Subscription]:
        query = "SELECT * FROM c WHERE c.tenant_id = @tenant ORDER BY c.id OFFSET @offset LIMIT @limit"
        rows = self.container.query_items(
            query=query, partition_key=tenant_id,
            parameters=[{"name": "@tenant", "value": tenant_id}, {"name": "@offset", "value": offset}, {"name": "@limit", "value": limit}],
        )
        return [Subscription.model_validate({k: v for k, v in row.items() if not k.startswith("_")}) async for row in rows]

    async def get(self, tenant_id: str, subscription_id: str) -> Subscription | None:
        try:
            row = await self.container.read_item(item=subscription_id, partition_key=tenant_id)
        except CosmosResourceNotFoundError:
            return None
        return Subscription.model_validate({k: v for k, v in row.items() if not k.startswith("_")})

    async def replace(self, subscription: Subscription) -> Subscription | None:
        try:
            await self.container.replace_item(item=subscription.id, body=subscription.model_dump(mode="json"))
        except CosmosResourceNotFoundError:
            return None
        return subscription

    async def delete(self, tenant_id: str, subscription_id: str) -> bool:
        try:
            await self.container.delete_item(item=subscription_id, partition_key=tenant_id)
        except CosmosResourceNotFoundError:
            return False
        return True

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = self.container = None
        if self.owns_credential and self.credential is not None:
            await self.credential.close()
            self.credential = None


def build_store() -> SubscriptionStore:
    mode = os.environ.get("SUBSCRIPTION_STORE", "cosmos")
    if mode == "memory":
        if os.environ.get("AUTH_MODE") != "development":
            raise ValueError("memory store requires explicit AUTH_MODE=development")
        return InMemorySubscriptionStore()
    if mode != "cosmos":
        raise ValueError("SUBSCRIPTION_STORE must be cosmos or memory")
    return CosmosSubscriptionStore(os.environ.get("COSMOS_ENDPOINT", ""), os.environ.get("COSMOS_DATABASE", "incidents"), os.environ.get("COSMOS_SUBSCRIPTIONS_CONTAINER", "subscriptions"))
