from unittest.mock import AsyncMock, MagicMock

import pytest

from app.consumer import NotificationConsumer
from app.processor import InvalidIncident


def make_message():
    message = MagicMock()
    message.body = b'{"event_id":"EVT-001"}'
    message.message_id = "EVT-001"
    message.delivery_count = 1
    return message


@pytest.mark.asyncio
async def test_successful_notification_completes_message():
    processor = AsyncMock()
    processor.process.return_value = None

    consumer = NotificationConsumer(
        processor=processor,
        fully_qualified_namespace="test.servicebus.windows.net",
    )

    consumer._receiver = AsyncMock()

    message = make_message()

    await consumer.settle(message)

    consumer._receiver.complete_message.assert_awaited_once_with(message)
    consumer._receiver.abandon_message.assert_not_called()
    consumer._receiver.dead_letter_message.assert_not_called()


@pytest.mark.asyncio
async def test_transient_failure_abandons_message():
    processor = AsyncMock()
    processor.process.side_effect = RuntimeError("Telegram unavailable")

    consumer = NotificationConsumer(
        processor=processor,
        fully_qualified_namespace="test.servicebus.windows.net",
    )

    consumer._receiver = AsyncMock()

    message = make_message()

    await consumer.settle(message)

    consumer._receiver.abandon_message.assert_awaited_once_with(message)
    consumer._receiver.complete_message.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_incident_is_dead_lettered():
    processor = AsyncMock()
    processor.process.side_effect = InvalidIncident("invalid incident")

    consumer = NotificationConsumer(
        processor=processor,
        fully_qualified_namespace="test.servicebus.windows.net",
    )

    consumer._receiver = AsyncMock()

    message = make_message()

    await consumer.settle(message)

    consumer._receiver.dead_letter_message.assert_awaited_once()
    consumer._receiver.complete_message.assert_not_called()
    consumer._receiver.abandon_message.assert_not_called()