import asyncio
import logging
import os
from abc import ABC, abstractmethod

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, TransportType
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.models import IncidentEvent


logger = logging.getLogger("app.notification_publisher")


def _transport_type() -> TransportType:
    if os.environ.get("SERVICE_BUS_TRANSPORT", "").lower() == "amqp":
        return TransportType.Amqp

    return TransportType.AmqpOverWebsocket


class NotificationPublishError(Exception):
    """Raised when an incident cannot be handed to notification-events."""


class NotificationEventPublisher(ABC):
    @abstractmethod
    async def publish(self, event: IncidentEvent) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


class ServiceBusNotificationPublisher(NotificationEventPublisher):
    """Publish processed incidents to the notification-events queue."""

    def __init__(
        self,
        fully_qualified_namespace: str | None = None,
        queue_name: str | None = None,
        credential=None,
        timeout_seconds: float | None = None,
    ):
        self.fully_qualified_namespace = (
            fully_qualified_namespace or os.environ.get("SERVICE_BUS_FQDN")
        )

        self.queue_name = queue_name or os.environ.get(
            "NOTIFICATION_QUEUE",
            "notification-events",
        )

        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("PUBLISH_TIMEOUT_SECONDS", "5")
        )

        if not self.fully_qualified_namespace:
            raise ValueError(
                "SERVICE_BUS_FQDN is required for "
                "ServiceBusNotificationPublisher"
            )

        self._credential = credential
        self._owns_credential = credential is None
        self._client = None
        self._sender = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            self._open()

    def _open(self) -> None:
        if self._sender is not None:
            return

        if self._credential is None:
            self._credential = DefaultAzureCredential()

        self._client = ServiceBusClient(
            fully_qualified_namespace=self.fully_qualified_namespace,
            credential=self._credential,
            transport_type=_transport_type(),
        )

        self._sender = self._client.get_queue_sender(
            queue_name=self.queue_name
        )

        logger.info(
            "notification publisher ready namespace=%s queue=%s",
            self.fully_qualified_namespace,
            self.queue_name,
        )

    async def publish(self, event: IncidentEvent) -> None:
        if self._sender is None:
            async with self._lock:
                self._open()

        message = ServiceBusMessage(
            event.model_dump_json(),
            content_type="application/json",
            message_id=event.event_id,
        )

        try:
            await asyncio.wait_for(
                self._sender.send_messages(message),
                timeout=self.timeout_seconds,
            )

        except (ServiceBusError, asyncio.TimeoutError) as exc:
            raise NotificationPublishError(
                f"failed to publish event {event.event_id} "
                f"to {self.queue_name}: {exc}"
            ) from exc

        logger.info(
            "published event_id=%s to queue=%s",
            event.event_id,
            self.queue_name,
        )

    async def close(self) -> None:
        if self._sender is not None:
            await self._sender.close()
            self._sender = None

        if self._client is not None:
            await self._client.close()
            self._client = None

        if self._credential is not None and self._owns_credential:
            await self._credential.close()
            self._credential = None


class MockNotificationPublisher(NotificationEventPublisher):
    """Local/test publisher that requires no Azure account."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.published_events: list[IncidentEvent] = []

    async def publish(self, event: IncidentEvent) -> None:
        if self.fail:
            raise NotificationPublishError(
                f"mock notification publish failed for {event.event_id}"
            )

        self.published_events.append(event)