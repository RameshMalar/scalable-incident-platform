from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.exceptions import ServiceRequestError

from app.notification_models import InvalidSubscription
from app.subscription_store import CosmosSubscriptionReader, InMemorySubscriptionReader
from tests.test_notification_models import subscription_document


class PagedRows:
    """Async iteration traverses supplied pages, like Cosmos AsyncItemPaged."""

    def __init__(self, pages, failure=None):
        self.pages = pages
        self.failure = failure
        self.pages_consumed = 0

    async def iterate(self):
        for page in self.pages:
            self.pages_consumed += 1
            for document in page:
                yield document
        if self.failure is not None:
            raise self.failure

    def __aiter__(self):
        return self.iterate()


@pytest.fixture
async def cosmos():
    container = Mock()
    container.query_items.return_value = PagedRows([])
    client = Mock()
    client.close = AsyncMock()
    client.get_database_client.return_value.get_container_client.return_value = container
    factory = Mock(return_value=client)
    credential = AsyncMock()
    reader = CosmosSubscriptionReader(
        endpoint="https://example.documents.azure.com", credential=credential,
        client_factory=factory,
    )
    yield reader, container, client, factory, credential
    await reader.close()


async def test_query_is_parameterized_and_tenant_partitioned(cosmos):
    reader, container, client, _, _ = cosmos
    await reader.find_matches("TENANT-A' OR true", "intrusion_detected")
    kwargs = container.query_items.call_args.kwargs
    assert kwargs["partition_key"] == "TENANT-A' OR true"
    assert kwargs["parameters"] == [
        {"name": "@tenant", "value": "TENANT-A' OR true"},
        {"name": "@event_type", "value": "intrusion_detected"},
    ]
    assert "TENANT-A" not in kwargs["query"]
    assert "c.enabled = true" in kwargs["query"]
    assert "c.channel = 'telegram'" in kwargs["query"]
    assert "OFFSET" not in kwargs["query"]
    assert "LIMIT" not in kwargs["query"]
    client.get_database_client.assert_called_once_with("incidents")
    client.get_database_client.return_value.get_container_client.assert_called_once_with("subscriptions")


async def test_all_cosmos_pages_and_phase_one_documents(cosmos):
    reader, container, _, _, _ = cosmos
    pages = PagedRows([
        [subscription_document(id="SUB-1")], [],
        [subscription_document(id="SUB-2"), subscription_document(id="SUB-3")],
    ])
    container.query_items.return_value = pages
    records = await reader.find_matches("TENANT-A", "intrusion_detected")
    assert [record.id for record in records] == ["SUB-1", "SUB-2", "SUB-3"]
    assert pages.pages_consumed == 3


@pytest.mark.parametrize("changes", [
    {"tenant_id": "TENANT-B"}, {"event_type": "helmet_missing"},
    {"enabled": False}, {"event_type": "*"},
])
async def test_unexpected_nonmatching_cosmos_rows_are_excluded(cosmos, changes):
    reader, container, _, _, _ = cosmos
    container.query_items.return_value = PagedRows([[subscription_document(**changes)]])
    assert await reader.find_matches("TENANT-A", "intrusion_detected") == []


async def test_query_failure_is_not_empty_matches(cosmos):
    reader, container, _, _, _ = cosmos
    container.query_items.side_effect = ServiceRequestError("offline")
    with pytest.raises(ServiceRequestError):
        await reader.find_matches("TENANT-A", "intrusion_detected")


async def test_later_page_failure_does_not_return_partial_matches(cosmos):
    reader, container, _, _, _ = cosmos
    container.query_items.return_value = PagedRows(
        [[subscription_document()]], failure=ServiceRequestError("later page offline")
    )
    with pytest.raises(ServiceRequestError):
        await reader.find_matches("TENANT-A", "intrusion_detected")


async def test_malformed_cosmos_row_is_skipped(cosmos):
    reader, container, _, _, _ = cosmos
    container.query_items.return_value = PagedRows([[subscription_document(enabled="true")]])
    assert await reader.find_matches("TENANT-A", "intrusion_detected") == []


async def test_client_reuse_and_external_credential_ownership(cosmos):
    reader, _, client, factory, credential = cosmos
    await reader.start()
    await reader.start()
    factory.assert_called_once_with(reader.endpoint, credential=credential)
    await reader.close()
    client.close.assert_awaited_once()
    credential.close.assert_not_awaited()


async def test_managed_identity_credential_closed_when_owned(monkeypatch, cosmos):
    _, container, client, factory, _ = cosmos
    credential = AsyncMock()
    credential_factory = Mock(return_value=credential)
    monkeypatch.setattr("app.subscription_store.DefaultAzureCredential", credential_factory)
    reader = CosmosSubscriptionReader("https://example.documents.azure.com", client_factory=factory)
    await reader.start()
    credential_factory.assert_called_once_with()
    await reader.close()
    credential.close.assert_awaited_once()


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://configured.documents.azure.com")
    monkeypatch.setenv("COSMOS_DATABASE", "configured-db")
    monkeypatch.setenv("COSMOS_SUBSCRIPTIONS_CONTAINER", "configured-subscriptions")
    reader = CosmosSubscriptionReader()
    assert reader.endpoint == "https://configured.documents.azure.com"
    assert reader.database_name == "configured-db"
    assert reader.container_name == "configured-subscriptions"


def test_endpoint_required(monkeypatch):
    monkeypatch.delenv("COSMOS_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="COSMOS_ENDPOINT"):
        CosmosSubscriptionReader()


async def test_memory_exact_matching_and_tenant_isolation():
    reader = InMemorySubscriptionReader([
        subscription_document(), subscription_document(id="SUB-B", tenant_id="TENANT-B"),
        subscription_document(id="SUB-OFF", enabled=False),
        subscription_document(id="SUB-OTHER", event_type="helmet_missing"),
    ])
    assert [record.id for record in await reader.find_matches("TENANT-A", "intrusion_detected")] == ["SUB-1"]
    assert [record.id for record in await reader.find_matches("TENANT-B", "intrusion_detected")] == ["SUB-B"]
    assert await reader.find_matches("TENANT-C", "intrusion_detected") == []


def test_memory_rejects_invalid_configuration():
    with pytest.raises(InvalidSubscription):
        InMemorySubscriptionReader([subscription_document(channel="email")])


async def test_valid_malformed_valid_cosmos_records(cosmos, caplog):
    reader, container, _, _, _ = cosmos
    bad = subscription_document(id='SECRET-TOKEN', tenant_id=None, telegram_chat_id='SECRET-CHAT')
    container.query_items.return_value = PagedRows([
        [subscription_document(id='SUB-FIRST'), bad],
        [{}, subscription_document(tenant_id=123), subscription_document(id='SUB-LAST')]])
    records = await reader.find_matches('TENANT-A','intrusion_detected')
    assert [r.id for r in records] == ['SUB-FIRST','SUB-LAST']
    assert caplog.text.count('skipping malformed') == 3
    assert 'SECRET' not in caplog.text


async def test_unexpected_query_exception_propagates(cosmos):
    reader, container, _, _, _ = cosmos
    container.query_items.side_effect = RuntimeError('unexpected infrastructure failure')
    with pytest.raises(RuntimeError):
        await reader.find_matches('TENANT-A','intrusion_detected')
