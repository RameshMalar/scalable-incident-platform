import asyncio
import logging
import os
from abc import ABC, abstractmethod

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, TransportType
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.models import IncidentEvent

logger = logging.getLogger("app.publisher")


def _transport_type() -> TransportType:
    # Native AMQP needs port 5671, which campus and corporate firewalls
    # routinely block -- the symptom is a connection timeout, not a refusal.
    # AMQP over WebSocket tunnels through 443 and works anywhere, so it is the
    # default; the deployed service sets SERVICE_BUS_TRANSPORT=amqp because its
    # egress is ours to control and the native port is the faster of the two.
    # scripts/verify-servicebus.py reads the same switch.
    if os.environ.get("SERVICE_BUS_TRANSPORT", "").lower() == "amqp":
        return TransportType.Amqp
    return TransportType.AmqpOverWebsocket


class PublishError(Exception):
    """Raised when an IncidentEvent could not be published."""


class EventPublisher(ABC):
    @abstractmethod
    async def publish(self, event: IncidentEvent) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        """Acquire whatever the publisher needs. No-op unless overridden."""

    async def close(self) -> None:
        """Release it again. No-op unless overridden."""


class ServiceBusEventPublisher(EventPublisher):
    """Publishes incidents to a Service Bus queue as a managed identity.

    There is no connection string to hold: `core.bicep` sets
    `disableLocalAuth: true` on the namespace, so SAS keys do not exist and the
    only way in is a credential holding **Azure Service Bus Data Sender**.
    Locally `DefaultAzureCredential` resolves to `az login`; in the container it
    resolves to the user-assigned identity named by `AZURE_CLIENT_ID`.

    The client and sender are opened once and reused for the life of the
    process. Opening them per message costs a TLS and AMQP handshake on every
    request -- invisible in a demo, and ruinous in the throughput and latency
    measurements this platform is assessed on.
    """

    def __init__(
        self,
        fully_qualified_namespace: str | None = None,
        queue_name: str | None = None,
        timeout_seconds: float | None = None,
        credential=None,
    ):
        self.fully_qualified_namespace = (
            fully_qualified_namespace or os.environ.get("SERVICE_BUS_FQDN")
        )
        self.queue_name = queue_name or os.environ.get(
            "SERVICE_BUS_QUEUE", "incident-events"
        )
        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("PUBLISH_TIMEOUT_SECONDS", "5")
        )

        if not self.fully_qualified_namespace:
            raise ValueError(
                "SERVICE_BUS_FQDN is required for ServiceBusEventPublisher, e.g. "
                "incident-bus-xxxx.servicebus.windows.net"
            )

        self._credential = credential
        # A credential passed in belongs to the caller; one we build is ours to
        # close.
        self._owns_credential = credential is None
        self._client: ServiceBusClient | None = None
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
        self._sender = self._client.get_queue_sender(queue_name=self.queue_name)

        logger.info(
            "service bus sender ready namespace=%s queue=%s transport=%s",
            self.fully_qualified_namespace,
            self.queue_name,
            _transport_type().name,
        )

    async def publish(self, event: IncidentEvent) -> None:
        if self._sender is None:
            async with self._lock:
                self._open()

        logger.info(
            "publishing event_id=%s tenant_id=%s to queue=%s",
            event.event_id,
            event.tenant_id,
            self.queue_name,
        )

        # message_id carries the producer's event id so the broker, and later
        # the worker's dedupe step, can recognise a redelivered incident as the
        # same incident rather than a second one.
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

    async def close(self) -> None:
        async with self._lock:
            if self._sender is not None:
                await self._sender.close()
                self._sender = None
            if self._client is not None:
                await self._client.close()
                self._client = None
            if self._credential is not None and self._owns_credential:
                await self._credential.close()
                self._credential = None


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
