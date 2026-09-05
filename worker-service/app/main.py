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
from app.processor import IncidentHandler, IncidentProcessor, LoggingIncidentHandler
from app.store import CosmosIncidentStore

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


def build_handler() -> IncidentHandler:
    """Pick where incidents go.

    Defaults to the logging handler so the worker runs locally with no Azure
    account and no credentials. The deployed worker sets INCIDENT_STORE=cosmos;
    without it the worker would consume the queue, log each incident and store
    none of them -- which is why CD asserts on the store, not just on the drain.
    """
    if os.environ.get("INCIDENT_STORE") == "cosmos":
        return CosmosIncidentStore()
    return LoggingIncidentHandler()


def handler_name(handler: IncidentHandler) -> str:
    return "cosmos" if isinstance(handler, CosmosIncidentStore) else "logging"


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
        handler = build_handler()
        consumer = IncidentConsumer(processor=IncidentProcessor(handler))
    except ValueError as exc:
        logger.error("worker cannot start: %s", exc)
        return 2

    stop = asyncio.Event()
    install_shutdown_handler(stop)

    start = getattr(handler, "start", None)
    if start is not None:
        await start()

    await consumer.start()
    logger.info(
        "worker ready queue=%s store=%s", consumer.queue_name, handler_name(handler)
    )

    try:
        await consumer.run(stop)
    finally:
        logger.info("worker draining and shutting down")
        await consumer.close()
        close = getattr(handler, "close", None)
        if close is not None:
            await close()

    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
