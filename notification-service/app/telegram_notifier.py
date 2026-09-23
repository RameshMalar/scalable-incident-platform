from __future__ import annotations

import os

import aiohttp

from .models import IncidentEvent
from .notifier import Notifier


class TelegramNotifier(Notifier):
    """Send incident alerts to Telegram."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required")

    async def notify(self, event: IncidentEvent) -> None:
        message = self._format_message(event)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise RuntimeError(
                        f"Telegram delivery failed "
                        f"status={response.status}: {body}"
                    )

    @staticmethod
    def _format_message(event: IncidentEvent) -> str:
        return (
            "Incident Alert\n\n"
            f"Event ID: {event.event_id}\n"
            f"Tenant: {event.tenant_id}\n"
            f"Site: {event.site_id}\n"
            f"Source: {event.source_name}\n"
            f"Analytics: {event.analytics_type}\n"
            f"Event: {event.event_type}\n"
            f"Severity: {event.severity}\n"
            f"Time: {event.timestamp.isoformat()}\n"
            f"Confidence: {event.confidence}\n"
            f"Description: {event.description or '-'}"
        )