import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, TransportType
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from app.models import IncidentEvent

logger = logging.getLogger("app.publisher")

# The audience a Service Bus data-plane token is minted for. Asking the
# credential for one is the cheapest honest proof that this replica can reach
# the namespace at all.
SERVICE_BUS_SCOPE = "https://servicebus.azure.net/.default"

# A failed DefaultAzureCredential explains itself across every credential it
# tried -- around two kilobytes. The readiness probe runs every ten seconds, so
# returning and logging that whole thing would bill Log Analytics by the
# kilobyte for the same message forever. The first line names the failure; the
# rest is in the SDK's own troubleshooting output.
MAX_READINESS_DETAIL = 200

# How many AMQP links one replica may hold open at once, which is also how many
# incidents it can be publishing at once. Each link is a connection, so this is
# not free; eight is well above what a replica sees before the autoscaler gives
# it a sibling at fifty concurrent requests.
DEFAULT_POOL_SIZE = 8


def _summarise(exc: Exception) -> str:
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
    return first_line[:MAX_READINESS_DETAIL]


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

    async def ready(self) -> tuple[bool, str]:
        """Whether this replica can actually publish right now.

        Returns (ready, detail). A publisher with nothing to reach is always
        ready, so this is only overridden where there is a real dependency.
        """
        return True, "ready"


class ServiceBusEventPublisher(EventPublisher):
    """Publishes incidents to a Service Bus queue as a managed identity.

    There is no connection string to hold: `core.bicep` sets
    `disableLocalAuth: true` on the namespace, so SAS keys do not exist and the
    only way in is a credential holding **Azure Service Bus Data Sender**.
    Locally `DefaultAzureCredential` resolves to `az login`; in the container it
    resolves to the user-assigned identity named by `AZURE_CLIENT_ID`.

    Senders are pooled and reused for the life of the process. Opening one per
    message costs a TLS and AMQP handshake on every request -- invisible in a
    demo, and ruinous in the throughput and latency measurements this platform
    is assessed on.

    A pool rather than one shared sender, because a sender is not safe to use
    from two coroutines at once. An AMQP link is a state machine, and
    concurrent sends interleave frames on it: the SDK detaches the link
    mid-send, shuts the handler down, and every request after that fails
    against a half-dismantled object. That is not theoretical -- the load
    simulator's first run at five incidents a second failed 94 of 100 requests
    exactly that way, while every check that published one incident at a time
    had passed for weeks. So each sender is held by one coroutine at a time,
    and a sender that failed is discarded rather than handed to the next
    caller.
    """

    def __init__(
        self,
        fully_qualified_namespace: str | None = None,
        queue_name: str | None = None,
        timeout_seconds: float | None = None,
        credential=None,
        readiness_timeout_seconds: float | None = None,
        pool_size: int | None = None,
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
        # Kept short: a readiness probe that hangs is a replica the platform
        # cannot make a decision about.
        self.readiness_timeout_seconds = readiness_timeout_seconds or float(
            os.environ.get("READINESS_TIMEOUT_SECONDS", "3")
        )
        self.pool_size = pool_size or int(
            os.environ.get("PUBLISH_POOL_SIZE", DEFAULT_POOL_SIZE)
        )

        if self.pool_size < 1:
            raise ValueError("PUBLISH_POOL_SIZE must be at least 1")

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
        # Holds senders that are free, and None for a slot no sender has been
        # built for yet. Taking from it is what reserves the right to send.
        self._pool: asyncio.LifoQueue | None = None
        self._lock = asyncio.Lock()
        self._token_expires_at: float = 0.0
        # Only state *changes* are logged, so a replica that stays unready does
        # not write the same line every ten seconds for as long as it is down.
        self._was_ready: bool | None = None

    async def start(self) -> None:
        async with self._lock:
            self._open()

    def _open(self) -> None:
        if self._pool is not None:
            return

        if self._credential is None:
            self._credential = DefaultAzureCredential()

        self._client = ServiceBusClient(
            fully_qualified_namespace=self.fully_qualified_namespace,
            credential=self._credential,
            transport_type=_transport_type(),
        )

        # One sender built now so the first incident does not pay for it, and
        # the rest of the slots left empty. Building a sender does no I/O, but
        # a replica that never sees two incidents at once has no reason to
        # hold eight links open.
        #
        # Last in, first out, so that replica keeps reusing the one sender it
        # has. A queue would hand back the slots in turn and open a link for
        # every one of them, which is the opposite of what a pool is for.
        self._pool = asyncio.LifoQueue()
        for _ in range(self.pool_size - 1):
            self._pool.put_nowait(None)
        self._pool.put_nowait(self._client.get_queue_sender(queue_name=self.queue_name))

        logger.info(
            "service bus sender pool ready namespace=%s queue=%s transport=%s size=%d",
            self.fully_qualified_namespace,
            self.queue_name,
            _transport_type().name,
            self.pool_size,
        )

    async def publish(self, event: IncidentEvent) -> None:
        if self._pool is None:
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

        # Waiting here, when every sender is busy, is back pressure: the
        # replica is already publishing as many incidents at once as it holds
        # links for, and the autoscaler answers sustained concurrency with
        # another replica.
        sender = await self._pool.get()
        sent = False
        try:
            if sender is None:
                sender = self._client.get_queue_sender(queue_name=self.queue_name)

            await asyncio.wait_for(
                sender.send_messages(message),
                timeout=self.timeout_seconds,
            )
            sent = True
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
        finally:
            if sent:
                self._pool.put_nowait(sender)
            else:
                # The slot is given back before the close is attempted, and
                # empty rather than holding this sender. The timeout above
                # cancels a send that was part-way onto the wire, and a
                # request cancelled by its client does the same, so what is
                # left is a link nobody can reason about -- while closing it
                # may itself be interrupted.
                self._pool.put_nowait(None)
                if sender is not None:
                    await self._discard(sender)

        logger.info("published event_id=%s", event.event_id)

    async def _discard(self, sender) -> None:
        """Close a sender that failed, without letting the close raise.

        A link that has already broken often refuses to shut down tidily, and
        the caller is on its way to reporting the send failure, which is the
        error that matters.
        """
        try:
            await sender.close()
        except Exception as exc:
            logger.debug("could not close a failed sender: %s", exc)

    async def ready(self) -> tuple[bool, str]:
        """Prove the credential can mint a Service Bus token.

        This is the probe's whole point, and the reason it cannot simply check
        that the sender object exists. Constructing the client and the sender
        does no network I/O -- the AMQP link is not opened until the first send
        -- so a process pointed at a namespace that does not exist starts
        cleanly and reports itself healthy. That has been observed directly.

        Asking for a token exercises what a bare object reference cannot: that
        a managed identity is actually attached, that it resolves, and that the
        token endpoint is reachable. `azure-identity` caches the token, so
        calling this every ten seconds costs a dictionary lookup rather than a
        round trip; the expiry is tracked here too so a cached token is not
        re-requested until it is close to running out.
        """
        if time.time() < self._token_expires_at - 300:
            return True, "ready"

        if self._credential is None:
            async with self._lock:
                self._open()

        try:
            token = await asyncio.wait_for(
                self._credential.get_token(SERVICE_BUS_SCOPE),
                timeout=self.readiness_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._record(
                False,
                f"timed out acquiring a Service Bus token after "
                f"{self.readiness_timeout_seconds}s",
            )
        except Exception as exc:
            return self._record(
                False, f"could not acquire a Service Bus token: {_summarise(exc)}"
            )

        self._token_expires_at = float(token.expires_on)
        return self._record(True, "ready")

    def _record(self, ready: bool, detail: str) -> tuple[bool, str]:
        if ready != self._was_ready:
            if ready:
                logger.info("ready: can acquire a Service Bus token")
            else:
                logger.warning("not ready: %s", detail)
            self._was_ready = ready
        return ready, detail

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                # Only the free senders are here to be closed. Any still out
                # with a request are closed by the client below, which is the
                # reason shutdown waits for requests to drain first.
                while not self._pool.empty():
                    sender = self._pool.get_nowait()
                    if sender is not None:
                        await self._discard(sender)
                self._pool = None
            if self._client is not None:
                await self._client.close()
                self._client = None
            if self._credential is not None and self._owns_credential:
                await self._credential.close()
                self._credential = None
            # Otherwise a reopened publisher would report ready on the strength
            # of a token the credential behind it no longer holds.
            self._token_expires_at = 0.0
            self._was_ready = None


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
