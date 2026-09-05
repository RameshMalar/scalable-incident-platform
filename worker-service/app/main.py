"""Entry point for the Processing Worker.

A background consumer, so there is no HTTP server and no ingress: the platform
knows it is alive from the queue draining, not from a health probe. That is
also why it is scaled by queue depth rather than by request concurrency --
see the `azure-servicebus` KEDA rule in `infra/worker.bicep`.
"""

import asyncio
import logging
import os
import signal
import sys

from app.consumer import IncidentConsumer
from app.processor import IncidentProcessor

logger = logging.getLogger("app.main")


def configure_logging() -> None:
    # Uvicorn is not here to configure logging for us, and the root logger
    # defaults to WARNING -- without this every line the worker writes about
    # the incidents it processes is dropped before reaching stdout, and with it
    # the Log Analytics workspace the container streams to.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def install_shutdown_handler(stop: asyncio.Event) -> None:
    """Turn the platform's stop signal into a clean end to the receive loop.

    Container Apps sends SIGTERM before it removes a replica -- on a scale-in,
    a new revision, or a node move. Without this the process is killed with
    messages still locked, and every one of them waits out its lock before the
    broker redelivers it. Draining the current batch first costs a second and
    saves that.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Signal handlers are not available on every platform; the loop
            # still exits when the process is torn down.
            logger.warning("no signal handler for %s on this platform", sig.name)


async def run() -> int:
    configure_logging()

    try:
        consumer = IncidentConsumer(processor=IncidentProcessor())
    except ValueError as exc:
        logger.error("worker cannot start: %s", exc)
        return 2

    stop = asyncio.Event()
    install_shutdown_handler(stop)

    await consumer.start()
    logger.info("worker ready queue=%s", consumer.queue_name)

    try:
        await consumer.run(stop)
    finally:
        logger.info("worker draining and shutting down")
        await consumer.close()

    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
