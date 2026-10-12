from __future__ import annotations

import json
import logging

from common_event_model import IncidentEvent
from pydantic import ValidationError

from .notification_models import NotificationEnvelope
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
    def __init__(self, notifier: Notifier, *, accept_legacy: bool = True) -> None:
        self._notifier = notifier
        # Temporary compatibility while the worker still sends flat incidents.
        self.accept_legacy = accept_legacy

    async def process(self, body: bytes | str) -> None:
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8")

            payload = json.loads(body)
            route_fields = {
                "schema_version", "subscription_id", "channel",
                "telegram_chat_id", "incident",
            }
            routed = isinstance(payload, dict) and bool(route_fields & payload.keys())
            if routed:
                # Never retry a failed envelope as a flat/global notification.
                if "schema_version" not in payload:
                    raise InvalidIncident("routed message requires schema_version")
                envelope = NotificationEnvelope.model_validate(payload)
                event = envelope.incident
            else:
                if not self.accept_legacy:
                    raise InvalidIncident("legacy notification messages are disabled")
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

        if routed:
            await self._notifier.notify(
                event, telegram_chat_id=envelope.telegram_chat_id
            )
        else:
            # Only this legacy branch can select the configured global chat.
            await self._notifier.notify(event)
