from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp
from common_event_model import IncidentEvent
from pydantic import TypeAdapter

from .evidence import EvidenceStore, EvidenceUnavailable
from .notifier import Notifier
from .notification_models import TelegramChatId


logger = logging.getLogger("app.telegram_notifier")

DEFAULT_MIN_SEND_INTERVAL_SECONDS = 3.1
DEFAULT_RATE_LIMIT_RETRIES = 1
DEFAULT_RETRY_AFTER_SECONDS = 5.0

DEFAULT_TEXT_TIMEOUT_SECONDS = 10.0

# Uploading up to 10 MB of evidence needs more headroom than a text post.
DEFAULT_PHOTO_TIMEOUT_SECONDS = 60.0

# Telegram truncates sendPhoto captions past this length, while sendMessage
# allows 4096. The full detail therefore survives on the text-only path.
CAPTION_LIMIT = 1024


class TelegramNotifier(Notifier):
    """Send incident alerts to Telegram."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        min_send_interval_seconds: float | None = None,
        max_rate_limit_retries: int | None = None,
        evidence_store: EvidenceStore | None = None,
        session_factory=None,
        sleep=None,
        clock=None,
        require_global_chat: bool = True,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        # The global chat serves only legacy flat messages. Subscriber-only
        # deployments reject those, so they need no global chat at all.
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or None
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

        if not self.chat_id and require_global_chat:
            raise ValueError("TELEGRAM_CHAT_ID is required")

        if self.min_send_interval_seconds < 0:
            raise ValueError("TELEGRAM_MIN_SEND_INTERVAL_SECONDS cannot be negative")

        if self.max_rate_limit_retries < 0:
            raise ValueError("TELEGRAM_MAX_RATE_LIMIT_RETRIES cannot be negative")

        # Absent store means evidence delivery is switched off, and every
        # alert goes out as text.
        self._evidence_store = evidence_store

        self._session_factory = session_factory or aiohttp.ClientSession
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._send_lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def notify(
        self, event: IncidentEvent, *, telegram_chat_id: str | None = None
    ) -> None:
        # None is the isolated legacy path. An explicit invalid destination
        # raises before evidence or HTTP IO; it never falls back to self.chat_id.
        destination = (
            self.chat_id if telegram_chat_id is None
            else TypeAdapter(TelegramChatId).validate_python(telegram_chat_id)
        )
        if not destination:
            raise ValueError("no Telegram destination: legacy global chat is not configured")
        # Fetched before the send lock is taken: blob IO must not stall the
        # throttle that the whole Telegram chat shares.
        evidence = await self._load_evidence(event)

        # One notifier instance serves one replica. The deployment is capped at
        # one replica as well, so this lock and interval apply to the Telegram
        # chat globally rather than only to one process.
        async with self._send_lock:
            await self._wait_for_turn()

            async with self._session_factory() as session:
                if evidence is not None:
                    try:
                        await self._deliver(
                            session,
                            "sendPhoto",
                            lambda: {
                                "data": self._photo_payload(event, evidence, destination)
                            },
                            DEFAULT_PHOTO_TIMEOUT_SECONDS,
                            event,
                        )
                        return

                    except RuntimeError as exc:
                        # Paging a human matters more than attaching the image,
                        # so a rejected photo still goes out as text.
                        logger.warning(
                            "Telegram sendPhoto failed event_id=%s; "
                            "falling back to text: %s",
                            event.event_id,
                            exc,
                        )
                        await self._wait_for_turn()

                await self._deliver(
                    session,
                    "sendMessage",
                    lambda: {
                        "json": {
                            "chat_id": destination,
                            "text": self._format_message(event),
                        }
                    },
                    DEFAULT_TEXT_TIMEOUT_SECONDS,
                    event,
                )

    async def _deliver(
        self,
        session,
        method: str,
        payload_factory,
        timeout_seconds: float,
        event: IncidentEvent,
    ) -> None:
        """POST one Telegram API call, honouring the shared rate limit.

        `payload_factory` is rebuilt per attempt because a multipart body can
        only be consumed once.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"

        for attempt in range(self.max_rate_limit_retries + 1):
            async with session.post(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **payload_factory(),
            ) as response:
                body = await response.text()

            if response.status < 400:
                self._schedule_after(self.min_send_interval_seconds)
                logger.info(
                    "Telegram notification delivered event_id=%s method=%s",
                    event.event_id,
                    method,
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

    async def _load_evidence(self, event: IncidentEvent):
        """Return evidence for the event, or None to send text only."""
        if self._evidence_store is None or not event.evidence_uri:
            return None

        try:
            return await self._evidence_store.fetch(event.evidence_uri)

        except EvidenceUnavailable as exc:
            logger.warning(
                "evidence unavailable event_id=%s; sending text alert: %s",
                event.event_id,
                exc,
            )
            return None

    def _photo_payload(
        self, event: IncidentEvent, evidence, destination: str
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("chat_id", destination)
        form.add_field("caption", self._caption(event))
        form.add_field(
            "photo",
            evidence.data,
            filename=evidence.filename,
            content_type=evidence.content_type,
        )
        return form

    @classmethod
    def _caption(cls, event: IncidentEvent) -> str:
        message = cls._format_message(event)

        if len(message) <= CAPTION_LIMIT:
            return message

        return message[: CAPTION_LIMIT - 3] + "..."

    async def close(self) -> None:
        if self._evidence_store is not None:
            await self._evidence_store.close()

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
