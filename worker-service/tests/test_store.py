from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.cosmos.exceptions import CosmosResourceExistsError

from app.models import IncidentEvent
from app.processor import Outcome
from app.store import CosmosIncidentStore

pytestmark = pytest.mark.asyncio

ENDPOINT = "https://incident-cosmos-test.documents.azure.com:443/"


def sample_event(**overrides):
    fields = {
        "event_id": "EVT-TEST0001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": datetime(2026, 8, 21, 10, 0, 0),
        "confidence": 0.95,
        "description": "Person detected inside restricted area",
        "evidence_uri": "/evidence/test.jpg",
    }
    fields.update(overrides)
    return IncidentEvent(**fields)


def fake_credential():
    credential = MagicMock()
    credential.close = AsyncMock()
    return credential


def wire_client(mock_client_cls, create_item=None):
    """Point the patched CosmosClient at a container, and hand it back."""
    container = MagicMock()
    container.create_item = create_item or AsyncMock()
    database = MagicMock()
    database.get_container_client.return_value = container
    client = MagicMock()
    client.get_database_client.return_value = database
    client.close = AsyncMock()
    mock_client_cls.return_value = client
    return client, container


def build_store(**overrides):
    kwargs = {
        "endpoint": ENDPOINT,
        "database_name": "incidents",
        "container_name": "incidents",
        "credential": fake_credential(),
    }
    kwargs.update(overrides)
    return CosmosIncidentStore(**kwargs)


async def test_an_endpoint_is_required(monkeypatch):
    monkeypatch.delenv("COSMOS_ENDPOINT", raising=False)

    with pytest.raises(ValueError):
        CosmosIncidentStore(endpoint=None)


@patch("app.store.CosmosClient")
async def test_a_new_incident_is_stored_and_reported_processed(mock_client_cls):
    _, container = wire_client(mock_client_cls)

    outcome = await build_store().handle(sample_event())

    assert outcome is Outcome.PROCESSED
    container.create_item.assert_awaited_once()


@patch("app.store.CosmosClient")
async def test_the_document_id_is_the_producers_event_id(mock_client_cls):
    # This is what makes the store's own uniqueness constraint the
    # deduplication, rather than something the worker has to remember.
    _, container = wire_client(mock_client_cls)

    await build_store().handle(sample_event(event_id="EVT-ABC123"))

    document = container.create_item.call_args.args[0]
    assert document["id"] == "EVT-ABC123"
    assert document["event_id"] == "EVT-ABC123"
    assert document["tenant_id"] == "TENANT-01"


@patch("app.store.CosmosClient")
async def test_a_redelivered_incident_is_reported_duplicate_not_overwritten(
    mock_client_cls,
):
    # An upsert would succeed here and silently overwrite the stored incident,
    # so a duplicate could never be observed. Creating and catching the conflict
    # makes it a fact the caller can act on.
    _, container = wire_client(
        mock_client_cls,
        create_item=AsyncMock(side_effect=CosmosResourceExistsError()),
    )
    store = build_store()

    outcome = await store.handle(sample_event())

    assert outcome is Outcome.DUPLICATE
    assert not hasattr(container, "upsert_item") or not container.upsert_item.called


@patch("app.store.CosmosClient")
async def test_a_credential_failure_is_raised_so_the_message_is_redelivered(
    mock_client_cls,
):
    # Swallowing this would complete the message and lose the incident. Raising
    # lets the consumer abandon it, which is what keeps maxDeliveryCount honest.
    wire_client(
        mock_client_cls,
        create_item=AsyncMock(side_effect=ClientAuthenticationError("no identity")),
    )

    with pytest.raises(ClientAuthenticationError):
        await build_store().handle(sample_event())


@patch("app.store.CosmosClient")
async def test_the_client_is_opened_once_and_reused(mock_client_cls):
    _, container = wire_client(mock_client_cls)
    store = build_store()

    await store.handle(sample_event(event_id="EVT-1"))
    await store.handle(sample_event(event_id="EVT-2"))
    await store.handle(sample_event(event_id="EVT-3"))

    assert mock_client_cls.call_count == 1
    assert container.create_item.await_count == 3


@patch("app.store.CosmosClient")
async def test_timestamps_survive_serialisation(mock_client_cls):
    # model_dump() alone leaves a datetime, which the Cosmos client cannot
    # serialise; the incident would fail to store for a reason unrelated to it.
    _, container = wire_client(mock_client_cls)

    await build_store().handle(sample_event())

    document = container.create_item.call_args.args[0]
    assert isinstance(document["timestamp"], str)
    assert document["timestamp"].startswith("2026-08-21T10:00:00")


@patch("app.store.CosmosClient")
async def test_close_releases_the_client_and_owned_credential(mock_client_cls):
    client, _ = wire_client(mock_client_cls)
    credential = fake_credential()
    store = build_store(credential=credential)

    await store.start()
    await store.close()

    client.close.assert_awaited_once()
    # Supplied by the caller, so not ours to close.
    assert not credential.close.await_count
