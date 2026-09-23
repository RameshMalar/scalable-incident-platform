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

    outcome = await handler.handle(event)

    assert outcome == Outcome.DUPLICATE
    store.handle.assert_awaited_once_with(event)
    publisher.publish.assert_awaited_once_with(event)


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

    with pytest.raises(RuntimeError):
        await handler.handle(event)