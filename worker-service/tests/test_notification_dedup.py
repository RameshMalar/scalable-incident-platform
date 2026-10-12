"""Fake broker contract tests; no Azure connection or Telegram delivery."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from azure.servicebus.exceptions import ServiceBusError
from common_event_model import IncidentEvent

from app.notification_publisher import NotificationPublishError, ServiceBusNotificationPublisher
from app.processor import Outcome
from tests.test_notification_models import incident_payload, subscription_document
from tests.test_subscription_integration import pipeline


class FakeBroker:
    """Models time-bounded, non-partitioned broker ingress deduplication only."""
    def __init__(self):
        self.now = 0
        self.seen = {}
        self.accepted = []
        self.attempts = []
        self.fail_before = None
        self.lose_ack = False
        self.lock = asyncio.Lock()

    async def send_messages(self, message):
        await asyncio.sleep(0)  # Allow independent publishers to interleave.
        async with self.lock:
            self.attempts.append(message.message_id)
            if len(self.attempts) == self.fail_before:
                raise ServiceBusError("rejected before acceptance")
            if self.seen.get(message.message_id, -86401) + 86400 <= self.now:
                self.seen[message.message_id] = self.now
                self.accepted.append(message)
            if self.lose_ack:
                self.lose_ack = False
                raise asyncio.TimeoutError("accepted but acknowledgement lost")


def publisher(broker):
    result = ServiceBusNotificationPublisher(fully_qualified_namespace="fake.invalid")
    result._sender = broker
    return result


def routed_pipeline(broker):
    handler, store, _, _ = pipeline([
        subscription_document(id="SUB-1"),
        subscription_document(id="SUB-2"),  # Same chat, separate matching subscription.
        subscription_document(id="SUB-3", telegram_chat_id="-100999"),
    ])
    handler.publisher = publisher(broker)
    return handler, store


async def test_partial_publication_retries_stable_ids_without_duplicate_enqueue():
    broker = FakeBroker()
    broker.fail_before = 3
    handler, store = routed_pipeline(broker)
    store.handle.side_effect = [Outcome.PROCESSED, Outcome.DUPLICATE]
    event = IncidentEvent(**incident_payload())
    with pytest.raises(NotificationPublishError):
        await handler.handle(event)
    assert len(broker.accepted) == 2
    assert await handler.handle(event) == Outcome.DUPLICATE
    assert broker.attempts[:3] == broker.attempts[3:]
    assert len(broker.accepted) == 3
    assert len(set(broker.attempts)) == 3


async def test_accepted_send_with_lost_ack_is_safe_to_retry():
    broker = FakeBroker()
    broker.lose_ack = True
    handler, store = routed_pipeline(broker)
    store.handle.side_effect = [Outcome.PROCESSED, Outcome.DUPLICATE]
    event = IncidentEvent(**incident_payload())
    with pytest.raises(NotificationPublishError):
        await handler.handle(event)
    assert len(broker.accepted) == 1
    await handler.handle(event)
    assert broker.attempts[0] == broker.attempts[1]
    assert len(broker.accepted) == 3


async def test_concurrent_workers_enqueue_once_per_matching_subscription():
    broker = FakeBroker()
    first, _ = routed_pipeline(broker)
    second, store = routed_pipeline(broker)
    store.handle.return_value = Outcome.DUPLICATE
    event = IncidentEvent(**incident_payload())
    await asyncio.gather(first.handle(event), second.handle(event))
    assert len(broker.attempts) == 6
    assert len(broker.accepted) == 3
    assert len(set(broker.attempts)) == 3


async def test_retry_after_window_can_enqueue_again():
    broker = FakeBroker()
    handler, _ = routed_pipeline(broker)
    event = IncidentEvent(**incident_payload())
    await handler.handle(event)
    broker.now = 86401
    await handler.handle(event)
    assert len(broker.accepted) == 6
