"""Receive notification events from Service Bus and settle them.

The Notification Service consumes from the `notification-events` queue.

Settlement behaviour:

    notification delivered successfully -> complete
    InvalidIncident                    -> dead-letter
    any other exception                -> abandon for retry

The queue is configured with maxDeliveryCount=5, so repeated transient
delivery failures eventually move the message to the Service Bus DLQ.
"""

import asyncio
import logging
import os

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import TransportType
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.processor import InvalidIncident, NotificationProcessor

logger = logging.getLogger("app.consumer")

MAX_DEAD_LETTER_DESCRIPTION = 4096


def _transport_type() -> TransportType:
    if os.environ.get("SERVICE_BUS_TRANSPORT", "").lower() == "amqp":
        return TransportType.Amqp
    return TransportType.AmqpOverWebsocket


def _body_bytes(message) -> bytes:
    """Flatten the received Service Bus message body to bytes."""
    body = message.body

    if isinstance(body, (bytes, bytearray)):
        return bytes(body)

    if isinstance(body, str):
        return body.encode("utf-8")

    return b"".join(
        chunk
        if isinstance(chunk, (bytes, bytearray))
        else str(chunk).encode("utf-8")
        for chunk in body
    )


class NotificationConsumer:
    def __init__(
        self,
        processor: NotificationProcessor,
        fully_qualified_namespace: str | None = None,
        queue_name: str | None = None,
        credential=None,
        max_message_count: int | None = None,
        max_wait_time: float | None = None,
    ):
        self.processor = processor

        self.fully_qualified_namespace = (
            fully_qualified_namespace or os.environ.get("SERVICE_BUS_FQDN")
        )

        self.queue_name = queue_name or os.environ.get(
            "SERVICE_BUS_QUEUE",
            "notification-events",
        )

        self.max_message_count = max_message_count or int(
            os.environ.get("RECEIVE_BATCH_SIZE", "10")
        )

        self.max_wait_time = max_wait_time or float(
            os.environ.get("RECEIVE_WAIT_SECONDS", "5")
        )

        if not self.fully_qualified_namespace:
            raise ValueError(
                "SERVICE_BUS_FQDN is required for NotificationConsumer, e.g. "
                "incident-bus-xxxx.servicebus.windows.net"
            )

        self._credential = credential
        self._owns_credential = credential is None
        self._client: ServiceBusClient | None = None
        self._receiver = None

    async def start(self) -> None:
        if self._receiver is not None:
            return

        if self._credential is None:
            self._credential = DefaultAzureCredential()

        self._client = ServiceBusClient(
            fully_qualified_namespace=self.fully_qualified_namespace,
            credential=self._credential,
            transport_type=_transport_type(),
        )

        self._receiver = self._client.get_queue_receiver(
            queue_name=self.queue_name
        )

        logger.info(
            "notification receiver ready namespace=%s queue=%s transport=%s batch=%s",
            self.fully_qualified_namespace,
            self.queue_name,
            _transport_type().name,
            self.max_message_count,
        )

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Receive and settle messages until stop is requested."""
        if self._receiver is None:
            await self.start()

        stop = stop or asyncio.Event()
        backoff = 1.0

        while not stop.is_set():
            try:
                messages = await self._receiver.receive_messages(
                    max_message_count=self.max_message_count,
                    max_wait_time=self.max_wait_time,
                )

            except ServiceBusError as exc:
                logger.error(
                    "receive failed, retrying in %.0fs: %s",
                    backoff,
                    exc,
                )

                await self._sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            backoff = 1.0

            for message in messages:
                await self.settle(message)

    async def settle(self, message) -> None:
        """Deliver one notification and settle its Service Bus message."""
        try:
            await self.processor.process(_body_bytes(message))

        except InvalidIncident as exc:
            await self._dead_letter(message, exc)
            return

        except Exception as exc:
            logger.error(
                "notification delivery failed message_id=%s; "
                "abandoning for redelivery (delivery_count=%s): %s",
                message.message_id,
                getattr(message, "delivery_count", None),
                exc,
            )

            await self._settle_call(
                self._receiver.abandon_message(message),
                message,
                "abandon",
            )
            return

        await self._settle_call(
            self._receiver.complete_message(message),
            message,
            "complete",
        )

    async def _dead_letter(
        self,
        message,
        exc: InvalidIncident,
    ) -> None:
        logger.error(
            "dead-lettering invalid notification message_id=%s: %s",
            message.message_id,
            exc,
        )

        await self._settle_call(
            self._receiver.dead_letter_message(
                message,
                reason="invalid-incident",
                error_description=str(exc)[:MAX_DEAD_LETTER_DESCRIPTION],
            ),
            message,
            "dead-letter",
        )

    async def _settle_call(
        self,
        awaitable,
        message,
        action: str,
    ) -> None:
        try:
            await awaitable

        except ServiceBusError as exc:
            logger.warning(
                "could not %s message_id=%s; it will be redelivered: %s",
                action,
                message.message_id,
                exc,
            )

    async def _sleep_or_stop(
        self,
        stop: asyncio.Event,
        seconds: float,
    ) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        if self._receiver is not None:
            await self._receiver.close()
            self._receiver = None

        if self._client is not None:
            await self._client.close()
            self._client = None

        if self._credential is not None and self._owns_credential:
            await self._credential.close()
            self._credential = None