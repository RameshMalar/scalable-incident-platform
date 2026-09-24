from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

from .models import IncidentEvent
from .notifier import Notifier


logger = logging.getLogger("app.telegram_notifier")

DEFAULT_MIN_SEND_INTERVAL_SECONDS = 3.1
DEFAULT_RATE_LIMIT_RETRIES = 1
DEFAULT_RETRY_AFTER_SECONDS = 5.0


class TelegramNotifier(Notifier):
    """Send incident alerts to Telegram."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        min_send_interval_seconds: float | None = None,
        max_rate_limit_retries: int | None = None,
        session_factory=None,
        sleep=None,
        clock=None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.min_send_interval_seconds = (
            min_send_interval_seconds
            if min_send_interval_seconds is not None
            else float(
                os.environ.get(
                    "TELEGRAM_MIN_SEND_INTERVAL_SECONDS",
                    str(DEFAULT_MIN_SEND_INTERVAL_SECONDS),
                )
            )
        )
        self.max_rate_limit_retries = (
            max_rate_limit_retries
            if max_rate_limit_retries is not None
            else int(
                os.environ.get(
                    "TELEGRAM_MAX_RATE_LIMIT_RETRIES",
                    str(DEFAULT_RATE_LIMIT_RETRIES),
                )
            )
        )

        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required")

        if self.min_send_interval_seconds < 0:
            raise ValueError("TELEGRAM_MIN_SEND_INTERVAL_SECONDS cannot be negative")

        if self.max_rate_limit_retries < 0:
            raise ValueError("TELEGRAM_MAX_RATE_LIMIT_RETRIES cannot be negative")

        self._session_factory = session_factory or aiohttp.ClientSession
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._send_lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def notify(self, event: IncidentEvent) -> None:
        message = self._format_message(event)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }

        # One notifier instance serves one replica. The deployment is capped at
        # one replica as well, so this lock and interval apply to the Telegram
        # chat globally rather than only to one process.
        async with self._send_lock:
            await self._wait_for_turn()

            async with self._session_factory() as session:
                for attempt in range(self.max_rate_limit_retries + 1):
                    async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        body = await response.text()

                    if response.status < 400:
                        self._schedule_after(self.min_send_interval_seconds)
                        logger.info(
                            "Telegram notification delivered event_id=%s",
                            event.event_id,
                        )
                        return

                    if response.status == 429:
                        retry_after = self._retry_after(response, body)
                        self._schedule_after(retry_after)

                        if attempt < self.max_rate_limit_retries:
                            logger.warning(
                                "Telegram rate limited event_id=%s; "
                                "retrying in %.1fs",
                                event.event_id,
                                retry_after,
                            )
                            await self._wait_for_turn()
                            continue
                    else:
                        self._schedule_after(self.min_send_interval_seconds)

                    raise RuntimeError(
                        f"Telegram delivery failed "
                        f"status={response.status}: {body}"
                    )

    async def _wait_for_turn(self) -> None:
        delay = self._next_send_at - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _schedule_after(self, seconds: float) -> None:
        self._next_send_at = self._clock() + max(0.0, seconds)

    @staticmethod
    def _retry_after(response, body: str) -> float:
        try:
            payload = json.loads(body)
            value = payload.get("parameters", {}).get("retry_after")
            if value is not None:
                return max(0.0, float(value))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass

        value = response.headers.get("Retry-After")
        if value is not None:
            try:
                return max(0.0, float(value))
            except ValueError:
                pass

        return DEFAULT_RETRY_AFTER_SECONDS

    @staticmethod
    def _format_message(event: IncidentEvent) -> str:
        heading = (
            "Incident Alert [E2E TEST]"
            if event.event_purpose == "e2e_test"
            else "Incident Alert"
        )
        return (
            f"{heading}\n\n"
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