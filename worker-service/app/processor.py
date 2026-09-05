"""Turns one raw queue message into a processed incident.

Deliberately free of Service Bus types: `process` takes bytes and either
returns an outcome or raises, so the whole decision table -- complete the
message, dead-letter it, or let the broker redeliver it -- is testable without
a broker. `consumer.py` owns the translation from that decision to an AMQP
settlement.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from enum import Enum

from pydantic import ValidationError

from app.models import IncidentEvent

logger = logging.getLogger("app.processor")


class InvalidIncident(Exception):
    """The message is malformed, so no number of retries will make it valid.

    Distinct from a transient failure on purpose: a message that raises this
    goes straight to the dead-letter subqueue instead of being redelivered five
    times first, which keeps a single bad producer from occupying the queue's
    delivery budget.
    """


class Outcome(str, Enum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"


class Deduplicator(ABC):
    """Remembers which incidents have already been processed.

    Service Bus delivers at least once: a worker that is killed mid-batch, or
    that takes longer than the queue's one-minute lock, will see the same
    incident again. Downstream consumers should see it once.
    """

    @abstractmethod
    async def seen(self, event_id: str) -> bool:
        """True if `event_id` has already been processed to completion."""

    @abstractmethod
    async def remember(self, event_id: str) -> None:
        """Record `event_id` as processed."""


class InMemoryDeduplicator(Deduplicator):
    """Bounded, per-replica dedupe.

    Two honest limitations. It is per-process, so a redelivery that lands on a
    different replica is not recognised; and it is bounded, so an incident that
    falls out of the window is not recognised either. It therefore suppresses
    the common case -- the same replica seeing the same message again after a
    lock expiry or a crash-free retry -- and nothing more.

    The real answer is a shared store keyed on `event_id`, which arrives with
    the Cosmos DB incident store: an idempotent upsert makes the dedupe check
    and the write the same operation. This class exists so the worker is
    correct about *why* it is only best-effort today rather than silently
    reprocessing everything.
    """

    def __init__(self, capacity: int = 10_000):
        self.capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    async def remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)


class IncidentHandler(ABC):
    """What actually happens to a valid, first-seen incident."""

    @abstractmethod
    async def handle(self, event: IncidentEvent) -> None:
        """Raise to signal a transient failure worth a redelivery."""


class LoggingIncidentHandler(IncidentHandler):
    """Writes one structured line per incident to stdout.

    Container Apps streams stdout to the Log Analytics workspace provisioned in
    `core.bicep`, so this is queryable today and is the seam the Cosmos DB
    writer replaces when the state layer lands.
    """

    async def handle(self, event: IncidentEvent) -> None:
        logger.info(
            "processed incident event_id=%s tenant_id=%s site_id=%s "
            "analytics_type=%s event_type=%s severity=%s confidence=%s "
            "source_id=%s occurred_at=%s",
            event.event_id,
            event.tenant_id,
            event.site_id,
            event.analytics_type,
            event.event_type,
            event.severity,
            event.confidence,
            event.source_id,
            event.timestamp.isoformat(),
        )


class IncidentProcessor:
    def __init__(
        self,
        handler: IncidentHandler | None = None,
        deduplicator: Deduplicator | None = None,
    ):
        self.handler = handler or LoggingIncidentHandler()
        self.deduplicator = deduplicator or InMemoryDeduplicator()

    async def process(self, body: bytes | str, message_id: str | None = None) -> Outcome:
        """Process one message body.

        Returns the outcome on success. Raises `InvalidIncident` for a message
        that can never succeed, and lets any other exception -- a failing
        handler -- propagate so the caller can hand the message back for
        redelivery.
        """
        event = self._parse(body, message_id)

        if await self.deduplicator.seen(event.event_id):
            logger.info(
                "skipping duplicate event_id=%s message_id=%s",
                event.event_id,
                message_id,
            )
            return Outcome.DUPLICATE

        await self.handler.handle(event)

        # Remembered only after the handler succeeded. The other order -- claim
        # first, then handle -- turns a crash midway through the handler into a
        # silently dropped incident, because the redelivery would be mistaken
        # for a duplicate. This way a crash costs a reprocess, which is the
        # cheaper of the two mistakes.
        await self.deduplicator.remember(event.event_id)
        return Outcome.PROCESSED

    def _parse(self, body: bytes | str, message_id: str | None) -> IncidentEvent:
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidIncident(
                    f"message_id={message_id} body is not valid UTF-8: {exc}"
                ) from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InvalidIncident(
                f"message_id={message_id} body is not valid JSON: {exc}"
            ) from exc

        try:
            return IncidentEvent(**payload)
        except (ValidationError, TypeError) as exc:
            raise InvalidIncident(
                f"message_id={message_id} does not satisfy the Common Event "
                f"Model: {exc}"
            ) from exc
