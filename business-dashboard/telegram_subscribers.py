"""Who receives Telegram alerts: a read-only view of the routing data.

Notifications are subscriber-only. The Worker publishes an alert only for an
enabled subscription (tenant + exact event type + chat) whose chat is also an
enabled, operator-approved destination for that tenant; with no match, nothing
is sent. This module reads those same two containers with the dashboard's
read-only Cosmos identity, so the page shows exactly what the Worker will use.
It never writes: subscriptions and approvals are changed by an operator
(scripts/telegram-demo-subscribers.py), not from the browser.

Chat IDs are never shown in full.
"""
import os
from dataclasses import dataclass

SUBSCRIPTIONS = os.environ.get("COSMOS_SUBSCRIPTIONS_CONTAINER", "subscriptions")
DESTINATIONS = os.environ.get("COSMOS_DESTINATIONS_CONTAINER", "approved-destinations")


@dataclass(frozen=True)
class Subscriber:
    tenant_id: str
    chat: str          # masked
    kind: str          # Person or Group
    label: str
    approved: bool
    event_types: tuple[str, ...]  # enabled subscriptions only

    @property
    def receives_alerts(self) -> bool:
        return self.approved and bool(self.event_types)


def mask_chat(chat_id) -> str:
    text = str(chat_id or "")
    return "…" + text[-3:] if len(text) > 3 else "…"


def chat_kind(chat_id) -> str:
    # Telegram: private chats have positive IDs, groups and channels negative.
    return "Group" if str(chat_id).startswith("-") else "Person"


def _query(container, query, tenant):
    return list(container.query_items(query=query, parameters=[{"name": "@t", "value": tenant}], partition_key=tenant))


def load_subscribers(database, tenants) -> list[Subscriber]:
    """Destinations and enabled subscriptions per tenant, read from Cosmos."""
    subscriptions = database.get_container_client(SUBSCRIPTIONS)
    destinations = database.get_container_client(DESTINATIONS)
    result = []
    for tenant in tenants:
        events, approved, labels = {}, {}, {}
        for row in _query(subscriptions, "SELECT c.event_type, c.telegram_chat_id, c.enabled, c.channel FROM c "
                                         "WHERE c.tenant_id = @t AND IS_DEFINED(c.event_type)", tenant):
            chat = row.get("telegram_chat_id")
            if row.get("channel") == "telegram" and isinstance(chat, str) and row.get("enabled") is True:
                events.setdefault(chat, set()).add(str(row["event_type"]))
            elif isinstance(chat, str):
                events.setdefault(chat, set())
        for row in _query(destinations, "SELECT c.telegram_chat_id, c.enabled, c.label FROM c WHERE c.tenant_id = @t", tenant):
            chat = row.get("telegram_chat_id")
            if isinstance(chat, str):
                approved[chat] = row.get("enabled") is True
                if isinstance(row.get("label"), str):
                    labels[chat] = row["label"][:40]
        for chat in sorted(set(events) | set(approved), key=lambda c: (labels.get(c, "~"), c[-3:])):
            result.append(Subscriber(tenant, mask_chat(chat), chat_kind(chat), labels.get(chat, ""),
                                     approved.get(chat, False), tuple(sorted(events.get(chat, ())))))
    return result


def recipients(subscribers, tenant_id: str, event_type: str) -> list[Subscriber]:
    """Who an event of this tenant and type would reach."""
    return [s for s in subscribers if s.tenant_id == tenant_id and s.approved and event_type in s.event_types]
