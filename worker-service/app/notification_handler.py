import logging
from typing import TYPE_CHECKING

from common_event_model import IncidentEvent

from app.notification_publisher import NotificationEventPublisher
from app.processor import IncidentHandler, Outcome

if TYPE_CHECKING:
    from app.subscription_router import SubscriptionRouter


logger = logging.getLogger("app.notification_handler")


def should_notify(event: IncidentEvent) -> bool:
    """Keep load-test traffic out of human notification channels."""
    if event.event_purpose == "load_test":
        return False

    # Messages already queued before event_purpose joined the contract have no
    # explicit classification. Keep the old prefix check only for that finite
    # backlog; every newly ingested event serialises event_purpose.
    if "event_purpose" not in event.model_fields_set:
        return not event.event_id.startswith("EVT-LOAD-")

    return True


class PersistAndNotifyHandler(IncidentHandler):
    """Persist an incident, then hand it to the notification pipeline.

    Eligible subscription routing is attempted for both PROCESSED and DUPLICATE
    outcomes. Zero matches completes without publishing. This is intentional.

    If persistence succeeds but notification publishing temporarily fails,
    the original Service Bus message is abandoned and later redelivered.
    On that redelivery Cosmos reports DUPLICATE, but the notification handoff
    must still be retried rather than silently losing the alert.
    """

    def __init__(
        self,
        store: IncidentHandler,
        publisher: NotificationEventPublisher,
        router: "SubscriptionRouter",
    ):
        self.store = store
        self.publisher = publisher
        self.router = router

    async def start(self) -> None:
        start_store = getattr(self.store, "start", None)
        if start_store is not None:
            await start_store()

        await self.publisher.start()
        await self.router.reader.start()

    async def handle(self, event: IncidentEvent) -> Outcome:
        outcome = await self.store.handle(event)

        if not should_notify(event):
            logger.debug(
                "notification skipped for load-test event_id=%s outcome=%s",
                event.event_id,
                outcome.value,
            )
            return outcome

        # Publish even when Cosmos reports DUPLICATE. A previous delivery may
        # have persisted successfully but failed before notification handoff.
        routes = await self.router.route(event)
        for route in routes:
            if await self.router.authorized(route.incident.tenant_id, route.telegram_chat_id):
                await self.publisher.publish(route)

        logger.info(
            "incident routing complete event_id=%s outcome=%s routes=%d",
            event.event_id,
            outcome.value,
            len(routes),
        )

        return outcome

    async def close(self) -> None:
        try:
            await self.publisher.close()
        finally:
            try:
                try:
                    await self.router.reader.close()
                finally:
                    await self.router.policies.close()
            finally:
                close_store = getattr(self.store, "close", None)
                if close_store is not None:
                    await close_store()
