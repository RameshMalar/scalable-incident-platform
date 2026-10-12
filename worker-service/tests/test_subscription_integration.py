from unittest.mock import AsyncMock, MagicMock

import pytest

from app.consumer import IncidentConsumer
from common_event_model import IncidentEvent
from app.notification_handler import PersistAndNotifyHandler
from app.notification_publisher import (
    MockNotificationPublisher, NotificationPublishError, route_message_id,
)
from app.processor import IncidentProcessor, Outcome
from app.subscription_router import SubscriptionRouter
from app.subscription_store import InMemorySubscriptionReader
from tests.test_notification_models import incident_payload, subscription_document


def pipeline(documents=None, outcome=Outcome.PROCESSED):
    store = AsyncMock()
    store.handle.return_value = outcome
    reader = InMemorySubscriptionReader(
        [subscription_document()] if documents is None else documents
    )
    reader.find_matches = AsyncMock(wraps=reader.find_matches)
    reader.start, reader.close = AsyncMock(), AsyncMock()
    publisher = MockNotificationPublisher()
    handler = PersistAndNotifyHandler(store, publisher, SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))))
    return handler, store, reader, publisher


@pytest.mark.parametrize("outcome", [Outcome.PROCESSED, Outcome.DUPLICATE])
async def test_persistence_then_lookup_then_routed_publish(outcome):
    handler, store, reader, publisher = pipeline(outcome=outcome)
    event = IncidentEvent(**incident_payload())
    order = []

    async def persist(event):
        order.append("persist")
        return outcome

    async def lookup(tenant, event_type):
        order.append("lookup")
        return [subscription_document()]

    original_publish = publisher.publish

    async def publish(route):
        order.append("publish")
        await original_publish(route)

    store.handle.side_effect = persist
    reader.find_matches.side_effect = lookup
    publisher.publish = publish
    assert await handler.handle(event) == outcome
    assert order == ["persist", "lookup", "publish"]
    route = publisher.published_events[0]
    assert route.subscription_id == "SUB-1"
    assert route.telegram_chat_id == "-100123456"
    assert route.incident.model_dump() == event.model_dump()
    assert route.incident.evidence_uri == event.evidence_uri


@pytest.mark.parametrize("documents", [
    [], [subscription_document(event_type="helmet_missing")],
    [subscription_document(tenant_id="TENANT-B")],
    [subscription_document(enabled=False)], [subscription_document(event_type="*")],
])
async def test_no_match_still_persists_and_succeeds(documents):
    handler, store, reader, publisher = pipeline(documents)
    event = IncidentEvent(**incident_payload())
    assert await handler.handle(event) == Outcome.PROCESSED
    store.handle.assert_awaited_once_with(event)
    reader.find_matches.assert_awaited_once_with(event.tenant_id, event.event_type)
    assert publisher.published_events == []


@pytest.mark.parametrize("legacy", [False, True])
async def test_suppressed_incidents_persist_without_lookup(legacy):
    handler, store, reader, publisher = pipeline()
    payload = incident_payload(event_purpose="load_test")
    if legacy:
        del payload["event_purpose"]
        payload["event_id"] = "EVT-LOAD-001"
    event = IncidentEvent(**payload)
    assert await handler.handle(event) == Outcome.PROCESSED
    store.handle.assert_awaited_once_with(event)
    reader.find_matches.assert_not_awaited()
    assert publisher.published_events == []


async def test_e2e_event_routes():
    handler, _, _, publisher = pipeline()
    await handler.handle(IncidentEvent(**incident_payload(event_purpose="e2e_test")))
    assert len(publisher.published_events) == 1


@pytest.mark.parametrize("stage", ["persistence", "lookup", "publish"])
async def test_failures_propagate(stage):
    handler, store, reader, publisher = pipeline()
    if stage == "persistence":
        store.handle.side_effect = RuntimeError("offline")
    elif stage == "lookup":
        reader.find_matches.side_effect = RuntimeError("offline")
    else:
        publisher.fail = True
    expected = NotificationPublishError if stage == "publish" else RuntimeError
    with pytest.raises(expected):
        await handler.handle(IncidentEvent(**incident_payload()))
    store.handle.assert_awaited_once()
    if stage == "persistence":
        reader.find_matches.assert_not_awaited()
    assert publisher.published_events == []


async def test_multiple_subscriptions_no_destination_deduplication():
    handler, _, _, publisher = pipeline([
        subscription_document(id="SUB-1"), subscription_document(id="SUB-2"),
        subscription_document(id="SUB-3", telegram_chat_id="-100999"),
    ])
    await handler.handle(IncidentEvent(**incident_payload()))
    assert [route.subscription_id for route in publisher.published_events] == [
        "SUB-1", "SUB-2", "SUB-3"
    ]


async def test_partial_fanout_retries_earlier_routes_on_duplicate():
    handler, store, _, publisher = pipeline([
        subscription_document(id=f"SUB-{number}") for number in range(3)
    ])
    store.handle.side_effect = [Outcome.PROCESSED, Outcome.DUPLICATE]
    attempts = []

    async def publish(route):
        attempts.append(route)
        if len(attempts) == 3:
            raise RuntimeError("third publish failed")

    publisher.publish = publish
    event = IncidentEvent(**incident_payload())
    with pytest.raises(RuntimeError, match="third publish"):
        await handler.handle(event)
    assert await handler.handle(event) == Outcome.DUPLICATE
    assert [route.subscription_id for route in attempts] == [
        "SUB-0", "SUB-1", "SUB-2", "SUB-0", "SUB-1", "SUB-2"
    ]
    assert [route_message_id(route) for route in attempts[:3]] == [
        route_message_id(route) for route in attempts[3:]
    ]


@pytest.mark.parametrize("failure", [False, True])
async def test_consumer_zero_matches_vs_lookup_failure(failure):
    handler, store, reader, _ = pipeline([])
    if failure:
        reader.find_matches.side_effect = RuntimeError("lookup offline")
    consumer = IncidentConsumer(
        processor=IncidentProcessor(handler), fully_qualified_namespace="test.invalid"
    )
    consumer._receiver = AsyncMock()
    message = MagicMock()
    message.body = IncidentEvent(**incident_payload()).model_dump_json().encode()
    message.message_id = "EVT-001"
    await consumer.settle(message)
    store.handle.assert_awaited_once()
    if failure:
        consumer._receiver.abandon_message.assert_awaited_once()
        consumer._receiver.complete_message.assert_not_awaited()
    else:
        consumer._receiver.complete_message.assert_awaited_once()
        consumer._receiver.abandon_message.assert_not_awaited()


async def test_handler_resource_lifecycle():
    handler, store, reader, publisher = pipeline()
    publisher.start, publisher.close = AsyncMock(), AsyncMock()
    await handler.start()
    await handler.close()
    for operation in [store.start, store.close, reader.start, reader.close,
                      publisher.start, publisher.close]:
        operation.assert_awaited_once()


async def test_close_failure_still_closes_reader_and_incident_store():
    handler, store, reader, publisher = pipeline()
    publisher.close = AsyncMock(side_effect=RuntimeError("close failed"))
    with pytest.raises(RuntimeError):
        await handler.close()
    reader.close.assert_awaited_once()
    store.close.assert_awaited_once()
