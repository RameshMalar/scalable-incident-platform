import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.servicebus.exceptions import ServiceBusError

from app.models import IncidentEvent
from app.publisher import MockEventPublisher, PublishError, ServiceBusEventPublisher

pytestmark = pytest.mark.asyncio


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


def async_context_manager(return_value):
    """A MagicMock usable with `async with`, yielding return_value."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def test_mock_publisher_records_published_events():
    publisher = MockEventPublisher()
    event = sample_event()

    await publisher.publish(event)

    assert publisher.published_events == [event]


async def test_mock_publisher_raises_publish_error_when_configured_to_fail():
    publisher = MockEventPublisher(fail=True)
    event = sample_event()

    with pytest.raises(PublishError):
        await publisher.publish(event)

    assert publisher.published_events == []


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_sends_message_with_timeout(mock_client_cls):
    mock_sender = MagicMock()
    mock_sender.send_messages = AsyncMock()
    mock_client = MagicMock()
    mock_client.get_topic_sender.return_value = async_context_manager(mock_sender)
    mock_client_cls.from_connection_string.return_value = async_context_manager(
        mock_client
    )

    publisher = ServiceBusEventPublisher(
        connection_string="Endpoint=sb://test/;SharedAccessKeyName=x;SharedAccessKey=y",
        topic_name="incident-events",
        timeout_seconds=2.5,
    )
    event = sample_event()

    await publisher.publish(event)

    mock_client.get_topic_sender.assert_called_once_with(
        topic_name="incident-events"
    )
    mock_sender.send_messages.assert_called_once()


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_wraps_send_failures_as_publish_error(
    mock_client_cls,
):
    mock_sender = MagicMock()
    mock_sender.send_messages = AsyncMock(
        side_effect=ServiceBusError("upstream failure")
    )
    mock_client = MagicMock()
    mock_client.get_topic_sender.return_value = async_context_manager(mock_sender)
    mock_client_cls.from_connection_string.return_value = async_context_manager(
        mock_client
    )

    publisher = ServiceBusEventPublisher(
        connection_string="Endpoint=sb://test/;SharedAccessKeyName=x;SharedAccessKey=y",
        topic_name="incident-events",
    )

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_wraps_timeout_as_publish_error(mock_client_cls):
    async def slow_send(*args, **kwargs):
        await asyncio.sleep(10)

    mock_sender = MagicMock()
    mock_sender.send_messages = slow_send
    mock_client = MagicMock()
    mock_client.get_topic_sender.return_value = async_context_manager(mock_sender)
    mock_client_cls.from_connection_string.return_value = async_context_manager(
        mock_client
    )

    publisher = ServiceBusEventPublisher(
        connection_string="Endpoint=sb://test/;SharedAccessKeyName=x;SharedAccessKey=y",
        topic_name="incident-events",
        timeout_seconds=0.05,
    )

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())


async def test_service_bus_publisher_requires_connection_string():
    with pytest.raises(ValueError):
        ServiceBusEventPublisher(connection_string=None)
