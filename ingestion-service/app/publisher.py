import asyncio
import logging
import os
from abc import ABC, abstractmethod

from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.models import IncidentEvent

logger = logging.getLogger("app.publisher")


class PublishError(Exception):
    """Raised when an IncidentEvent could not be published."""


class EventPublisher(ABC):
    @abstractmethod
    async def publish(self, event: IncidentEvent) -> None:
        raise NotImplementedError


class ServiceBusEventPublisher(EventPublisher):
    def __init__(
        self,
        connection_string: str | None = None,
        topic_name: str | None = None,
        timeout_seconds: float | None = None,
    ):
        self.connection_string = connection_string or os.environ.get(
            "SERVICE_BUS_CONNECTION_STRING"
        )
        self.topic_name = topic_name or os.environ.get(
            "SERVICE_BUS_TOPIC", "incident-events"
        )
        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("PUBLISH_TIMEOUT_SECONDS", "5")
        )

        if not self.connection_string:
            raise ValueError(
                "SERVICE_BUS_CONNECTION_STRING is required for ServiceBusEventPublisher"
            )

    async def publish(self, event: IncidentEvent) -> None:
        logger.info(
            "publishing event_id=%s tenant_id=%s to topic=%s",
            event.event_id,
            event.tenant_id,
            self.topic_name,
        )

        try:
            async with ServiceBusClient.from_connection_string(
                self.connection_string
            ) as client:
                async with client.get_topic_sender(
                    topic_name=self.topic_name
                ) as sender:
                    message = ServiceBusMessage(event.model_dump_json())
                    await asyncio.wait_for(
                        sender.send_messages(message),
                        timeout=self.timeout_seconds,
                    )
        except ServiceBusError as exc:
            logger.error(
                "failed to publish event_id=%s: %s", event.event_id, exc
            )
            raise PublishError(
                f"failed to publish event {event.event_id}: {exc}"
            ) from exc
        except asyncio.TimeoutError as exc:
            logger.error(
                "timed out publishing event_id=%s after %ss",
                event.event_id,
                self.timeout_seconds,
            )
            raise PublishError(
                f"timed out publishing event {event.event_id} after "
                f"{self.timeout_seconds}s"
            ) from exc
        except Exception as exc:
            logger.error(
                "unexpected error publishing event_id=%s: %s", event.event_id, exc
            )
            raise PublishError(
                f"unexpected error publishing event {event.event_id}: {exc}"
            ) from exc

        logger.info("published event_id=%s", event.event_id)


class MockEventPublisher(EventPublisher):
    """Test double. Records published events; can be told to fail."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.published_events: list[IncidentEvent] = []

    async def publish(self, event: IncidentEvent) -> None:
        if self.fail:
            logger.error("mock publish failure for event_id=%s", event.event_id)
            raise PublishError(f"mock publish failure for event {event.event_id}")

        logger.info("mock published event_id=%s", event.event_id)
        self.published_events.append(event)
