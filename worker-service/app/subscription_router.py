"""Select notification routes without changing the active processing pipeline."""

import logging
from app.destination_policy import DenyDestinationPolicy
from common_event_model import IncidentEvent

logger = logging.getLogger(__name__)
from app.notification_models import InvalidSubscription
from app.notification_handler import should_notify
from app.notification_models import NotificationEnvelope
from app.owner_eligibility import owner_eligible
from app.subscription_store import (
    SubscriptionReader,
    matches,
    validate_subscription,
)


class SubscriptionRouter:
    def __init__(self, reader: SubscriptionReader, policies=None):
        self.reader = reader
        self.policies = policies if policies is not None else DenyDestinationPolicy()

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
            try:
                record = validate_subscription(document)
            except InvalidSubscription:
                logger.warning("skipping malformed subscription routing record")
                continue
            if not matches(record, incident.tenant_id, incident.event_type):
                continue
            if not await self.authorized(record.tenant_id, record.telegram_chat_id):
                continue
            if record.owner_object_id is not None and not await owner_eligible(
                    self.reader.read_platform_record, record.owner_object_id,
                    record.tenant_id, record.telegram_chat_id):
                # Revoked/disabled owner, removed tenant or replaced chat.
                logger.info("owner no longer eligible; route skipped")
                continue
            routes.append(NotificationEnvelope(
                subscription_id=record.id,
                channel=record.channel,
                telegram_chat_id=record.telegram_chat_id,
                incident=incident.model_copy(deep=True),
            ))
        # Malformed records are isolated; lookup and authorization failures propagate.
        # Nothing here persists incidents or publishes messages to Service Bus.
        return routes

    async def authorized(self, tenant_id, chat_id):
        try:
            approved = (await self.policies.approved(tenant_id, chat_id)) is True
        except Exception:
            logger.error("destination policy check failed; notification publication blocked")
            raise
        if not approved:
            logger.warning("notification destination not approved; route blocked")
        return approved
