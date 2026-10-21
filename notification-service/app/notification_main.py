import asyncio
import logging
import os
import signal

from app.consumer import NotificationConsumer
from app.delivery_eligibility import build_eligibility
from app.evidence import EvidenceStore
from app.notifier import MockNotifier
from app.processor import NotificationProcessor
from app.telegram_notifier import TelegramNotifier


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("app.main")


def accept_legacy() -> bool:
    """False makes delivery subscriber-only: a flat (unrouted) message is
    rejected and dead-lettered instead of going to the global chat. Only the
    Worker's routed envelopes -- one per approved, matching subscription --
    are sent. Defaults to true so an unchanged deployment behaves as before."""
    return os.environ.get("NOTIFICATION_ACCEPT_LEGACY", "true").strip().lower() != "false"


def build_notifier(legacy: bool = True):

    mode = os.environ.get("NOTIFIER", "mock").lower()

    if mode == "telegram":
        return TelegramNotifier(evidence_store=EvidenceStore.from_env(), require_global_chat=legacy)

    return MockNotifier()

async def run() -> None:
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops may not support add_signal_handler.
            pass

    legacy = accept_legacy()
    notifier = build_notifier(legacy)
    eligibility = build_eligibility()
    processor = NotificationProcessor(notifier, accept_legacy=legacy, eligibility=eligibility)

    consumer = NotificationConsumer(processor=processor)

    try:
        await consumer.start()

        logger.info(
            "notification service ready notifier=%s delivery=%s",
            notifier.__class__.__name__,
            "legacy+subscribers" if legacy else "subscribers-only",
        )

        await consumer.run(stop)

    finally:
        await consumer.close()
        await notifier.close()
        if eligibility is not None:
            await eligibility.close()

        logger.info("notification service stopped")


if __name__ == "__main__":
    asyncio.run(run())