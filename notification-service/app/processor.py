from __future__ import annotations

import json
import logging

from common_event_model import IncidentEvent
from pydantic import ValidationError

from .notifier import Notifier


logger = logging.getLogger("app.processor")


class InvalidIncident(ValueError):
    """Raised when a notification message is not a valid incident event."""


def should_notify(event: IncidentEvent) -> bool:
    """Keep historical and future load-test traffic out of Telegram."""
    if event.event_purpose == "load_test":
        return False

    # The current queue predates event_purpose. Use the old id convention only
    # for messages where that field was absent; all newly ingested events carry
    # an explicit classification.
    if "event_purpose" not in event.model_fields_set:
        return not event.event_id.startswith("EVT-LOAD-")

    return True


class NotificationProcessor:
    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def process(self, body: bytes | str) -> None:
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8")

            payload = json.loads(body)
            event = IncidentEvent.model_validate(payload)

        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise InvalidIncident(str(exc)) from exc

        # The worker applies the same policy before publishing new messages.
        # This second check drains load-test events already queued before that
        # policy was deployed without sending them to a human channel.
        if not should_notify(event):
            logger.debug(
                "notification skipped for load-test event_id=%s",
                event.event_id,
            )
            return

        await self._notifier.notify(event)
