from unittest.mock import AsyncMock

import pytest

from app.notification_handler import PersistAndNotifyHandler
from app.processor import Outcome


@pytest.mark.asyncio
async def test_processed_incident_is_published():
    store = AsyncMock()
    store.handle.return_value = Outcome.PROCESSED

    publisher = AsyncMock()

    handler = PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
    )

    event = AsyncMock()
    event.event_id = "EVT-001"
    event.event_purpose = "incident"
    event.model_fields_set = {"event_purpose"}

    outcome = await handler.handle(event)

    assert outcome == Outcome.PROCESSED
    store.handle.assert_awaited_once_with(event)
    publisher.publish.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_duplicate_incident_is_still_published():
    store = AsyncMock()
    store.handle.return_value = Outcome.DUPLICATE

    publisher = AsyncMock()

    handler = PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
    )

    event = AsyncMock()
    event.event_id = "EVT-001"
    event.event_purpose = "e2e_test"
    event.model_fields_set = {"event_purpose"}

    outcome = await handler.handle(event)

    assert outcome == Outcome.DUPLICATE
    store.handle.assert_awaited_once_with(event)
    publisher.publish.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_load_test_incident_is_stored_without_notification():
    store = AsyncMock()
    store.handle.return_value = Outcome.PROCESSED

    publisher = AsyncMock()

    handler = PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
    )

    event = AsyncMock()
    event.event_id = "EVT-001"
    event.event_purpose = "load_test"
    event.model_fields_set = {"event_purpose"}

    outcome = await handler.handle(event)

    assert outcome == Outcome.PROCESSED
    store.handle.assert_awaited_once_with(event)
    publisher.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_load_test_incident_is_stored_without_notification():
    store = AsyncMock()
    store.handle.return_value = Outcome.PROCESSED

    publisher = AsyncMock()

    handler = PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
    )

    event = AsyncMock()
    event.event_id = "EVT-LOAD-000000001"
    event.event_purpose = "incident"
    event.model_fields_set = set()

    outcome = await handler.handle(event)

    assert outcome == Outcome.PROCESSED
    store.handle.assert_awaited_once_with(event)
    publisher.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_propagates_for_retry():
    store = AsyncMock()
    store.handle.return_value = Outcome.PROCESSED

    publisher = AsyncMock()
    publisher.publish.side_effect = RuntimeError("notification queue unavailable")

    handler = PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
    )

    event = AsyncMock()
    event.event_id = "EVT-001"
    event.event_purpose = "incident"
    event.model_fields_set = {"event_purpose"}

    with pytest.raises(RuntimeError):
        await handler.handle(event)
