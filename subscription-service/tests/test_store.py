from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from app.models import Subscription
from app.store import CosmosSubscriptionStore, InMemorySubscriptionStore, build_store


@pytest.fixture
def item():
    now = datetime.now(timezone.utc)
    return Subscription(id="SUB-1", tenant_id="TENANT-A", event_type="intrusion_detected", telegram_chat_id="123", created_at=now, updated_at=now)


@pytest.fixture
async def cosmos():
    container = Mock()
    container.read = AsyncMock()
    container.create_item = AsyncMock()
    container.read_item = AsyncMock()
    container.replace_item = AsyncMock()
    container.delete_item = AsyncMock()
    client = Mock()
    client.close = AsyncMock()
    client.get_database_client.return_value.get_container_client.return_value = container
    credential = AsyncMock()
    factory = Mock(return_value=client)
    store = CosmosSubscriptionStore("https://example.documents.azure.com", credential=credential, client_factory=factory)
    await store.start()
    yield store, container, client, credential, factory
    await store.close()


async def rows(items):
    for item in items:
        yield item


async def test_cosmos_create_and_partitioned_read(cosmos, item):
    store, container, _, _, _ = cosmos
    await store.create(item)
    document = container.create_item.call_args.args[0]
    assert document["id"] == item.id
    assert document["tenant_id"] == "TENANT-A"
    assert isinstance(document["created_at"], str)
    container.read_item.return_value = {**document, "_etag": "etag"}
    assert await store.get("TENANT-A", "SUB-1") == item
    container.read_item.assert_awaited_once_with(item="SUB-1", partition_key="TENANT-A")


async def test_cosmos_list_is_parameterized_and_partitioned(cosmos, item):
    store, container, _, _, _ = cosmos
    container.query_items.return_value = rows([item.model_dump(mode="json")])
    assert await store.list("TENANT-A", 51, 0) == [item]
    kwargs = container.query_items.call_args.kwargs
    assert kwargs["partition_key"] == "TENANT-A"
    assert {"name": "@tenant", "value": "TENANT-A"} in kwargs["parameters"]
    assert "TENANT-A" not in kwargs["query"]


async def test_cosmos_replace_uses_existing_partition_body(cosmos, item):
    store, container, _, _, _ = cosmos
    assert await store.replace(item) == item
    container.replace_item.assert_awaited_once_with(item="SUB-1", body=item.model_dump(mode="json"))


async def test_cosmos_delete_has_explicit_partition(cosmos):
    store, container, _, _, _ = cosmos
    assert await store.delete("TENANT-A", "SUB-1") is True
    container.delete_item.assert_awaited_once_with(item="SUB-1", partition_key="TENANT-A")


async def test_cosmos_missing_operations(cosmos, item):
    store, container, _, _, _ = cosmos
    for method in [container.read_item, container.replace_item, container.delete_item]:
        method.side_effect = CosmosResourceNotFoundError(status_code=404)
    assert await store.get("TENANT-B", item.id) is None
    assert await store.replace(item) is None
    assert await store.delete("TENANT-B", item.id) is False


async def test_cosmos_ready_checks_remote_access(cosmos):
    store, container, _, _, _ = cosmos
    container.query_items.return_value = rows([])
    assert await store.ready()
    container.read.assert_awaited_once()
    assert container.query_items.call_args.kwargs["partition_key"] == "__readiness__"


async def test_cosmos_lifecycle_reuses_client_and_keeps_external_credential(cosmos):
    store, _, client, credential, factory = cosmos
    await store.start()
    factory.assert_called_once()
    await store.close()
    client.close.assert_awaited_once()
    credential.close.assert_not_awaited()


async def test_memory_same_id_in_different_partitions(item):
    store = InMemorySubscriptionStore()
    await store.create(item)
    other = item.model_copy(update={"tenant_id": "TENANT-B"})
    await store.create(other)
    assert await store.get("TENANT-A", item.id) == item
    assert await store.delete("TENANT-B", item.id)
    assert await store.get("TENANT-A", item.id) == item
    assert await store.replace(other) is None


def test_memory_requires_explicit_development(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_STORE", "memory")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    with pytest.raises(ValueError):
        build_store()
    monkeypatch.setenv("AUTH_MODE", "development")
    assert isinstance(build_store(), InMemorySubscriptionStore)


def test_cosmos_requires_endpoint():
    with pytest.raises(ValueError):
        CosmosSubscriptionStore("")
