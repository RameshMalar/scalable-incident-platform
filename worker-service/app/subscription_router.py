"""Select notification routes without changing the active processing pipeline."""

from common_event_model import IncidentEvent
from app.notification_handler import should_notify
from app.notification_models import NotificationEnvelope
from app.subscription_store import (
    SubscriptionReader,
    matches,
    validate_subscription,
)


class SubscriptionRouter:
    def __init__(self, reader: SubscriptionReader):
        self.reader = reader

    async def route(self, incident: IncidentEvent) -> list[NotificationEnvelope]:
        # Reuse the existing policy rather than allowing a second copy to drift.
        # Suppressed traffic performs no Cosmos lookup.
        if not should_notify(incident):
            return []

        records = await self.reader.find_matches(
            incident.tenant_id, incident.event_type
        )
        routes = []
        for document in records:
            # Recheck even injected/custom readers. No foreign-tenant route can
            # be created merely because a reader returned the wrong document.
            record = validate_subscription(document)
            if not matches(record, incident.tenant_id, incident.event_type):
                continue
            routes.append(NotificationEnvelope(
                subscription_id=record.id,
                channel=record.channel,
                telegram_chat_id=record.telegram_chat_id,
                incident=incident.model_copy(deep=True),
            ))
        # An invalid later document raises before any routes are returned.
        # Nothing here persists incidents or publishes messages to Service Bus.
        return routes
