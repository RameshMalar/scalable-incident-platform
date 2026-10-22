"""Send-time recheck of routed envelopes. Telegram delivery is mocked."""
import json
from unittest.mock import AsyncMock, Mock

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from app.consumer import NotificationConsumer
from app.delivery_eligibility import ACCESS_PARTITION, CosmosRecordReader, DeliveryEligibility, build_eligibility
from app.notifier import MockNotifier
from app.processor import NotificationProcessor
from tests.test_telegram_notifier import make_event

OWNER = "00000000-0000-4000-8000-000000000101"
CHAT = "-100987654"


class Reader:
    def __init__(self, documents):
        self.documents = documents

    async def read(self, partition, document_id):
        return self.documents.get((partition, document_id))

    async def close(self):
        pass


def documents(owner=OWNER, **changes):
    subscription = {"id": "SUB-1", "tenant_id": "TENANT-01", "event_type": "intrusion_detected", "channel": "telegram",
                    "telegram_chat_id": CHAT, "enabled": True, "owner_object_id": owner}
    subscription.update(changes)
    return {
        ("TENANT-01", "SUB-1"): subscription,
        (ACCESS_PARTITION, "access:" + OWNER): {"record_type": "platform_access", "object_id": OWNER, "role": "SOC Operator",
                                                 "tenants": ["TENANT-01"], "status": "active"},
        (ACCESS_PARTITION, "telegram-owner:" + OWNER): {"record_type": "telegram_owner", "object_id": OWNER, "chat_id": CHAT},
    }


def envelope():
    return json.dumps({"schema_version": 1, "subscription_id": "SUB-1", "channel": "telegram",
                       "telegram_chat_id": CHAT, "incident": make_event().model_dump(mode="json")})


async def send(docs):
    notifier = MockNotifier()
    await NotificationProcessor(notifier, eligibility=DeliveryEligibility(Reader(docs))).process(envelope())
    return notifier.sent_destinations


async def test_current_subscription_is_sent():
    assert await send(documents()) == [CHAT]
    assert await send(documents(owner=None)) == [CHAT]


@pytest.mark.parametrize("docs", [
    documents(enabled=False), documents(telegram_chat_id="-1"), documents(event_type="fire_detected"),
    {k: v for k, v in documents().items() if k[0] != "TENANT-01"},
    {**documents(), (ACCESS_PARTITION, "access:" + OWNER): {"record_type": "platform_access", "object_id": OWNER,
                                                           "role": "SOC Operator", "tenants": ["TENANT-01"], "status": "revoked"}},
    {**documents(), (ACCESS_PARTITION, "access:" + OWNER): {"record_type": "platform_access", "object_id": OWNER,
                                                           "role": "SOC Operator", "tenants": ["TENANT-02"], "status": "active"}},
    {k: v for k, v in documents().items() if not k[1].startswith("telegram-owner:")},
])
async def test_queued_message_is_dropped_when_no_longer_eligible(docs):
    assert await send(docs) == []


async def test_legacy_flat_message_bypasses_routed_recheck():
    notifier = MockNotifier()
    eligibility = Mock(eligible=AsyncMock(side_effect=AssertionError("unexpected")))
    await NotificationProcessor(notifier, eligibility=eligibility).process(make_event().model_dump_json())
    assert notifier.sent_destinations == [None]


async def test_outage_abandons_for_retry_and_never_sends():
    notifier = MockNotifier()
    eligibility = Mock(eligible=AsyncMock(side_effect=RuntimeError("cosmos down")))
    consumer = NotificationConsumer(NotificationProcessor(notifier, eligibility=eligibility),
                                    fully_qualified_namespace="test.servicebus.windows.net")
    consumer._receiver = Mock(abandon_message=AsyncMock(), complete_message=AsyncMock(), dead_letter_message=AsyncMock())
    message = Mock(body=envelope().encode(), message_id="m1", delivery_count=1)
    await consumer.settle(message)
    consumer._receiver.abandon_message.assert_awaited_once_with(message)
    consumer._receiver.complete_message.assert_not_awaited()
    assert notifier.sent_events == []


def test_off_by_default_and_rejects_unknown_modes(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_ELIGIBILITY", raising=False)
    assert build_eligibility() is None
    monkeypatch.setenv("NOTIFICATION_ELIGIBILITY", "maybe")
    with pytest.raises(ValueError):
        build_eligibility()


async def test_cosmos_reader_distinguishes_absent_item_from_missing_container():
    container = Mock(read_item=AsyncMock(side_effect=CosmosResourceNotFoundError()),
                     read=AsyncMock(return_value={"partitionKey": {"paths": ["/tenant_id"]}}))
    reader = CosmosRecordReader(endpoint="https://example.documents.azure.com", credential=object())
    reader.container = container
    assert await reader.read("TENANT-01", "SUB-1") is None
    container.read.side_effect = CosmosResourceNotFoundError()
    with pytest.raises(CosmosResourceNotFoundError):
        await reader.read("TENANT-01", "SUB-1")
