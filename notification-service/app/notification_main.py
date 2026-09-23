import asyncio
import logging
import os
import signal

from app.consumer import NotificationConsumer
from app.notifier import MockNotifier
from app.processor import NotificationProcessor
from app.telegram_notifier import TelegramNotifier


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("app.main")


def build_notifier():

    mode = os.environ.get("NOTIFIER", "mock").lower()

    if mode == "telegram":
        return TelegramNotifier()

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

    notifier = build_notifier()
    processor = NotificationProcessor(notifier)

    consumer = NotificationConsumer(processor=processor)

    try:
        await consumer.start()

        logger.info(
            "notification service ready notifier=%s",
            notifier.__class__.__name__,
        )

        await consumer.run(stop)

    finally:
        await consumer.close()

        logger.info("notification service stopped")


if __name__ == "__main__":
    asyncio.run(run())