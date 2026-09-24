import logging

from app.models import IncidentEvent
from app.notification_publisher import NotificationEventPublisher
from app.processor import IncidentHandler, Outcome


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

    Notification publishing is attempted for both PROCESSED and DUPLICATE
    outcomes. This is intentional.

    If persistence succeeds but notification publishing temporarily fails,
    the original Service Bus message is abandoned and later redelivered.
    On that redelivery Cosmos reports DUPLICATE, but the notification handoff
    must still be retried rather than silently losing the alert.
    """

    def __init__(
        self,
        store: IncidentHandler,
        publisher: NotificationEventPublisher,
    ):
        self.store = store
        self.publisher = publisher

    async def start(self) -> None:
        start_store = getattr(self.store, "start", None)
        if start_store is not None:
            await start_store()

        await self.publisher.start()

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
        await self.publisher.publish(event)

        logger.info(
            "incident handed to notification pipeline event_id=%s outcome=%s",
            event.event_id,
            outcome.value,
        )

        return outcome

    async def close(self) -> None:
        await self.publisher.close()

        close_store = getattr(self.store, "close", None)
        if close_store is not None:
            await close_store()