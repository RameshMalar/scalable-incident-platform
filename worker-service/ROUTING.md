# Worker subscription routing — local integration

The Cosmos-backed handler persists first, applies existing purpose suppression,
reads matching subscriptions, then publishes one version-1 envelope per match.
Zero matches completes processing without publishing. The logging-only local
default remains unchanged. No HTTP calls to Subscription API are introduced.

The reader uses COSMOS_ENDPOINT, COSMOS_DATABASE (default incidents), and
COSMOS_SUBSCRIPTIONS_CONTAINER (default subscriptions), with DefaultAzureCredential
and existing worker identity selection. Matching remains exact and tenant scoped.

Persistence, lookup and publishing failures propagate to existing consumer retries.
DUPLICATE outcomes still perform eligible matching/handoff. Partial fan-out retries
may repeat previously successful routes. Overlapping subscriptions to the same chat
produce separate envelopes. Delivery is at least once; no outbox or delivery ledger.

Message IDs are `route-v1-` followed by SHA-256 hex of a compact JSON array:
[tenant_id, event_id, subscription_id, channel, telegram_chat_id]. IDs are 73 ASCII
characters, unambiguous and stable across retries. They do not guarantee exactly-once
Telegram delivery. The complete IncidentEvent, including evidence_uri, is preserved.

Before deployment: provision /tenant_id subscriptions container and identity access,
approve each destination for its tenant, deploy the envelope-capable consumer before
activating this worker, and update/test infrastructure and workflows. A missing
subscription container causes errors, not zero matches. Notification Service retains
legacy compatibility/global chat configuration. This phase performs no live operations.
