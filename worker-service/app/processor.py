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

    This is now the local-development path only. The deployed worker writes to
    Cosmos DB, where the document id is the event id, so the write itself is the
    deduplication check and it is shared across every replica -- see
    `store.CosmosIncidentStore`.
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
    """What actually happens to a valid incident.

    The handler reports the outcome rather than the processor deciding it,
    because a store that deduplicates atomically -- writing the incident and
    rejecting a repeat are one operation -- is the only thing that knows
    whether this delivery was the first.
    """

    @abstractmethod
    async def handle(self, event: IncidentEvent) -> "Outcome":
        """Return PROCESSED or DUPLICATE.

        Raise to signal a transient failure worth a redelivery.
        """


class LoggingIncidentHandler(IncidentHandler):
    """Writes one structured line per incident to stdout. Local development.

    Deduplication here is a separate step from the write, so the ordering has
    to be got right by hand: the event id is remembered only after the line has
    been emitted. Claiming it first would turn a crash midway into a silently
    dropped incident, because the redelivery would be read as a duplicate. This
    way a crash costs a reprocess, which is the cheaper mistake.

    `CosmosIncidentStore` does not need any of this reasoning, because there the
    check and the write are the same operation.
    """

    def __init__(self, deduplicator: Deduplicator | None = None):
        self.deduplicator = deduplicator or InMemoryDeduplicator()

    async def handle(self, event: IncidentEvent) -> "Outcome":
        if await self.deduplicator.seen(event.event_id):
            logger.info("skipping duplicate event_id=%s", event.event_id)
            return Outcome.DUPLICATE

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

        await self.deduplicator.remember(event.event_id)
        return Outcome.PROCESSED


class IncidentProcessor:
    def __init__(self, handler: IncidentHandler | None = None):
        self.handler = handler or LoggingIncidentHandler()

    async def process(self, body: bytes | str, message_id: str | None = None) -> Outcome:
        """Process one message body.

        Returns the outcome on success. Raises `InvalidIncident` for a message
        that can never succeed, and lets any other exception -- a failing
        handler -- propagate so the caller can hand the message back for
        redelivery.
        """
        event = self._parse(body, message_id)
        return await self.handler.handle(event)

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
