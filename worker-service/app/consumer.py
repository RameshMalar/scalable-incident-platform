"""Receives incidents from the Service Bus queue and settles them.

Authentication is the same managed-identity story as the ingestion service:
`core.bicep` sets `disableLocalAuth: true` on the namespace, so there is no
connection string to hold and the only way in is a credential carrying **Azure
Service Bus Data Receiver**. Locally `DefaultAzureCredential` resolves to
`az login`; in the container it resolves to the user-assigned identity named by
`AZURE_CLIENT_ID`.

This module owns exactly one decision: how the outcome of `IncidentProcessor`
becomes an AMQP settlement.

    processed / duplicate  -> complete       the incident is done with
    InvalidIncident        -> dead-letter    retrying will never help
    any other exception    -> abandon        transient; let the broker redeliver

Abandoning is what makes the queue's `maxDeliveryCount: 5` mean something: five
transient failures and Service Bus dead-letters the message itself, so a
poison message never blocks the queue behind it.
"""

import asyncio
import logging
import os

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import TransportType
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.processor import IncidentProcessor, InvalidIncident, Outcome

logger = logging.getLogger("app.consumer")

# Service Bus caps the dead-letter description at 4096 characters. A pydantic
# validation error listing every bad field comfortably exceeds that, and an
# over-long value fails the settlement rather than truncating -- which would
# turn an unprocessable message into a stuck one.
MAX_DEAD_LETTER_DESCRIPTION = 4096


def _transport_type() -> TransportType:
    # Same switch, and the same reasoning, as the ingestion publisher: native
    # AMQP needs port 5671, which campus and corporate firewalls routinely
    # block, so WebSocket over 443 is the portable default and the deployed
    # worker sets SERVICE_BUS_TRANSPORT=amqp to opt back into the faster path.
    if os.environ.get("SERVICE_BUS_TRANSPORT", "").lower() == "amqp":
        return TransportType.Amqp
    return TransportType.AmqpOverWebsocket


def _body_bytes(message) -> bytes:
    """Flatten a received message body to bytes.

    `ServiceBusReceivedMessage.body` is a generator of byte chunks, not a
    `bytes`, because a message may arrive as several AMQP data sections.
    """
    body = message.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b"".join(
        chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        for chunk in body
    )


class IncidentConsumer:
    def __init__(
        self,
        processor: IncidentProcessor | None = None,
        fully_qualified_namespace: str | None = None,
        queue_name: str | None = None,
        credential=None,
        max_message_count: int | None = None,
        max_wait_time: float | None = None,
    ):
        self.processor = processor or IncidentProcessor()
        self.fully_qualified_namespace = (
            fully_qualified_namespace or os.environ.get("SERVICE_BUS_FQDN")
        )
        self.queue_name = queue_name or os.environ.get(
            "SERVICE_BUS_QUEUE", "incident-events"
        )
        # A batch amortises the round trip to the broker over several messages.
        # Kept modest because every message in a batch holds a one-minute lock
        # from the moment it is received, and this worker settles them one at a
        # time; scaling out is KEDA's job, not this loop's.
        self.max_message_count = max_message_count or int(
            os.environ.get("RECEIVE_BATCH_SIZE", "10")
        )
        # How long an idle receive waits before returning empty. It is also the
        # worst-case delay on shutdown, so it stays well inside the platform's
        # termination grace period.
        self.max_wait_time = max_wait_time or float(
            os.environ.get("RECEIVE_WAIT_SECONDS", "5")
        )

        if not self.fully_qualified_namespace:
            raise ValueError(
                "SERVICE_BUS_FQDN is required for IncidentConsumer, e.g. "
                "incident-bus-xxxx.servicebus.windows.net"
            )

        self._credential = credential
        # A credential passed in belongs to the caller; one we build is ours to
        # close.
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
        self._receiver = self._client.get_queue_receiver(queue_name=self.queue_name)

        logger.info(
            "service bus receiver ready namespace=%s queue=%s transport=%s batch=%s",
            self.fully_qualified_namespace,
            self.queue_name,
            _transport_type().name,
            self.max_message_count,
        )

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Receive and settle until `stop` is set."""
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
                # A broker blip should cost a pause, not the container. Backing
                # off matters because the alternative is a tight reconnect loop
                # that looks to Service Bus exactly like an attack.
                logger.error(
                    "receive failed, retrying in %.0fs: %s", backoff, exc
                )
                await self._sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            backoff = 1.0

            # Messages already received hold a lock, so the batch is finished
            # even when a shutdown lands mid-way. Settling them now is cheaper
            # than letting the locks expire and the whole batch be redelivered.
            for message in messages:
                await self.settle(message)

    async def settle(self, message) -> Outcome | None:
        """Process one message and tell the broker what became of it."""
        try:
            outcome = await self.processor.process(
                _body_bytes(message), message.message_id
            )
        except InvalidIncident as exc:
            await self._dead_letter(message, exc)
            return None
        except Exception as exc:
            logger.error(
                "transient failure on message_id=%s, abandoning for redelivery "
                "(delivery_count=%s): %s",
                message.message_id,
                getattr(message, "delivery_count", None),
                exc,
            )
            await self._settle_call(
                self._receiver.abandon_message(message), message, "abandon"
            )
            return None

        await self._settle_call(
            self._receiver.complete_message(message), message, "complete"
        )
        return outcome

    async def _dead_letter(self, message, exc: InvalidIncident) -> None:
        logger.error(
            "dead-lettering unprocessable message_id=%s: %s",
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

    async def _settle_call(self, awaitable, message, action: str) -> None:
        """Await a settlement, tolerating a lock that expired under us.

        Losing the lock is not a failure of this worker -- the broker simply
        redelivers the message -- but an unhandled exception here would take
        the receive loop down with it.
        """
        try:
            await awaitable
        except ServiceBusError as exc:
            logger.warning(
                "could not %s message_id=%s, it will be redelivered: %s",
                action,
                message.message_id,
                exc,
            )

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
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
