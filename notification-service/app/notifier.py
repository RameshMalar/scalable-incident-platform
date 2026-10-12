from __future__ import annotations

from abc import ABC, abstractmethod

from common_event_model import IncidentEvent


class Notifier(ABC):
    """Sends an incident notification to an external delivery channel."""

    @abstractmethod
    async def notify(
        self, event: IncidentEvent, *, telegram_chat_id: str | None = None
    ) -> None:
        """Deliver to an explicit chat; None is legacy configured-chat delivery."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release any long-lived resources. Channels without any do nothing."""


class MockNotifier(Notifier):
    """In-memory notifier used for local development and unit tests."""

    def __init__(self) -> None:
        self.sent_events: list[IncidentEvent] = []
        self.sent_destinations: list[str | None] = []

    async def notify(
        self, event: IncidentEvent, *, telegram_chat_id: str | None = None
    ) -> None:
        self.sent_events.append(event)
        self.sent_destinations.append(telegram_chat_id)
