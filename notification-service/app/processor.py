from __future__ import annotations

import json

from pydantic import ValidationError

from .models import IncidentEvent
from .notifier import Notifier


class InvalidIncident(ValueError):
    """Raised when a notification message is not a valid incident event."""


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

        await self._notifier.notify(event)