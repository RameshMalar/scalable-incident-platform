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
from app.notification_handler import PersistAndNotifyHandler
from app.notification_publisher import (
    MockNotificationPublisher,
    ServiceBusNotificationPublisher,
)
from app.processor import IncidentHandler, IncidentProcessor, LoggingIncidentHandler
from app.store import CosmosIncidentStore
from app.subscription_store import CosmosSubscriptionReader
from app.subscription_router import SubscriptionRouter
from app.destination_policy import CosmosDestinationPolicy

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
    """Pick the worker processing pipeline.

    Local development defaults to the logging handler so the worker can run
    without Azure credentials.

    The deployed worker uses:
    - INCIDENT_STORE=cosmos
    - NOTIFICATION_PUBLISHER=servicebus

    This produces the processing flow:

        incident-events
              |
              v
        Processing Worker
              |
              +--> Cosmos DB
              |
              +--> enabled subscription matches --> notification-events

    When Cosmos reports an incident as a duplicate, the notification publisher
    still attempts the downstream handoff. This protects against the case where
    persistence succeeded but notification publishing failed on an earlier
    delivery.

    Subscription reads reuse COSMOS_ENDPOINT/COSMOS_DATABASE and the worker
    identity, with COSMOS_SUBSCRIPTIONS_CONTAINER defaulting to subscriptions.
    """

    if os.environ.get("INCIDENT_STORE") != "cosmos":
        return LoggingIncidentHandler()

    store = CosmosIncidentStore()

    if os.environ.get("NOTIFICATION_PUBLISHER") == "servicebus":
        publisher = ServiceBusNotificationPublisher()
    else:
        # Allows Cosmos-related local/unit testing without requiring access to
        # Service Bus.
        publisher = MockNotificationPublisher()

    return PersistAndNotifyHandler(
        store=store,
        publisher=publisher,
        router=SubscriptionRouter(CosmosSubscriptionReader(), policies=CosmosDestinationPolicy()),
    )


def handler_name(handler: IncidentHandler) -> str:
    if isinstance(handler, PersistAndNotifyHandler):
        return "cosmos+notification"

    if isinstance(handler, CosmosIncidentStore):
        return "cosmos"

    return "logging"


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
        consumer = IncidentConsumer(
            processor=IncidentProcessor(handler)
        )
    except ValueError as exc:
        logger.error("worker cannot start: %s", exc)
        return 2

    stop = asyncio.Event()
    install_shutdown_handler(stop)

    try:
        start = getattr(handler, "start", None)
        if start is not None:
            await start()
        await consumer.start()
        logger.info(
            "worker ready queue=%s store=%s",
            consumer.queue_name,
            handler_name(handler),
        )
        await consumer.run(stop)

    finally:
        logger.info("worker draining and shutting down")

        try:
            await consumer.close()
        finally:
            close = getattr(handler, "close", None)
            if close is not None:
                await close()

    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
