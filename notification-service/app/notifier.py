from __future__ import annotations

from abc import ABC, abstractmethod

from .models import IncidentEvent


class Notifier(ABC):
    """Sends an incident notification to an external delivery channel."""

    @abstractmethod
    async def notify(self, event: IncidentEvent) -> None:
        """Deliver one incident notification."""
        raise NotImplementedError


class MockNotifier(Notifier):
    """In-memory notifier used for local development and unit tests."""

    def __init__(self) -> None:
        self.sent_events: list[IncidentEvent] = []

    async def notify(self, event: IncidentEvent) -> None:
        self.sent_events.append(event)