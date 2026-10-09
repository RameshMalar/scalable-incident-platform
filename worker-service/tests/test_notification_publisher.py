import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from azure.servicebus.exceptions import ServiceBusError

from common_event_model import IncidentEvent
from app.notification_models import NotificationEnvelope
from app.notification_publisher import (
    NotificationPublishError, ServiceBusNotificationPublisher, route_message_id,
)
from tests.test_notification_models import incident_payload


def make_route(**changes):
    return NotificationEnvelope(
        **{"subscription_id": "SUB-1", "channel": "telegram",
           "telegram_chat_id": "-100123", "incident": IncidentEvent(**incident_payload()),
           **changes}
    )


def test_route_id_is_deterministic_bounded_and_handles_long_event_ids():
    route = make_route(incident=IncidentEvent(**incident_payload(event_id="x" * 2000)))
    assert route_message_id(route) == route_message_id(route.model_copy(deep=True))
    assert len(route_message_id(route)) == 73


@pytest.mark.parametrize("changes", [
    {"subscription_id": "SUB-2"}, {"telegram_chat_id": "-100999"},
    {"incident": IncidentEvent(**incident_payload(event_id="EVT-2"))},
    {"incident": IncidentEvent(**incident_payload(tenant_id="TENANT-B"))},
])
def test_different_route_identity_has_different_id(changes):
    assert route_message_id(make_route()) != route_message_id(make_route(**changes))


async def test_mocked_servicebus_receives_complete_envelope_and_route_id():
    publisher = ServiceBusNotificationPublisher(fully_qualified_namespace="test.invalid")
    publisher._sender = AsyncMock()
    route = make_route()
    await publisher.publish(route)
    message = publisher._sender.send_messages.call_args.args[0]
    assert json.loads(b"".join(message.body)) == route.model_dump(mode="json")
    assert message.message_id == route_message_id(route)
    assert message.content_type == "application/json"


@pytest.mark.parametrize("error", [ServiceBusError("offline"), asyncio.TimeoutError()])
async def test_publish_failure_wrapped_for_retry(error):
    publisher = ServiceBusNotificationPublisher(fully_qualified_namespace="test.invalid")
    publisher._sender = AsyncMock()
    publisher._sender.send_messages.side_effect = error
    with pytest.raises(NotificationPublishError):
        await publisher.publish(make_route())


async def test_close_failure_still_releases_owned_resources():
    publisher = ServiceBusNotificationPublisher(fully_qualified_namespace="test.invalid")
    sender, client, credential = AsyncMock(), AsyncMock(), AsyncMock()
    publisher._sender, publisher._client, publisher._credential = sender, client, credential
    sender.close.side_effect = RuntimeError("sender close failed")
    with pytest.raises(RuntimeError):
        await publisher.close()
    client.close.assert_awaited_once()
    credential.close.assert_awaited_once()
