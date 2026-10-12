# Tenant-approved Telegram destinations

Destination approvals are operator-controlled records in a separate Cosmos
container, `approved-destinations`, partitioned by `/tenant_id`. Configure its
name with `COSMOS_DESTINATIONS_CONTAINER`; endpoint/database reuse the existing
Cosmos configuration. No service creates the container or writes policy records.
The API exposes no policy creation or approval endpoints.

Each record uses `id = "telegram:" + telegram_chat_id` within the tenant partition,
and contains `tenant_id`, `channel: "telegram"`, `telegram_chat_id` (a string),
and strict boolean `enabled`. A missing/disabled/foreign-tenant record denies
approval. Malformed records or unavailable storage fail closed. Chat IDs must
match exactly; no alternate representations are automatically normalized.

Enabled POST/PATCH results require approval under the authenticated server-bound
tenant and return sanitized 403 on denial, 503 on policy unavailability. Reads
remain available for old unapproved subscriptions. Disabled writes and DELETE
remain available, including disabling revoked subscriptions. Re-enabling requires
current approval. API permission checks remain unchanged.

Worker independently reads policy during routing and again immediately before
publishing each envelope. Denial skips the route with a sanitized warning;
policy outages raise an operational error, preserving broker retry behavior.
Incident persistence occurs first. No global-chat fallback is introduced.

`infra/core.bicep` provisions the container and its read-only assignments (see
below), so CD creates them. Approval records are still written by an operator,
who must review tenant ownership of every approved destination first; no
destination is auto-approved from existing subscription records.
Missing configuration/permissions must not be treated as an empty approval list.

## Container validation and failure classification

The configured container must have its exact configured ID (by default
`approved-destinations`) and `partitionKey.paths = ["/tenant_id"]` with
`partitionKey.kind = "Hash"`. A different path, hierarchical partition layout,
kind or container ID fails validation. Endpoint and database configuration must
point to the operator-reviewed policy store; no container is created by the adapter.

Every approval lookup first reads and validates container metadata, then performs
a point item read using `id = "telegram:" + chat_id` and the tenant partition.
A missing-item 404 triggers a second metadata read to detect parent-container
deletion during lookup. Only a missing approval record in a still-valid container
returns ordinary denial. Disabled or foreign-tenant records also deny approval.
Missing database/container, invalid metadata, permission failures, throttling,
network/service failures and malformed records raise sanitized `PolicyUnavailable`.
The API returns 503; Worker propagates the failure for broker retry without sending.
No infrastructure failure is interpreted as approval.

There is one explicit metadata request per lookup in addition to the item request,
and another metadata request after an item 404. Worker checks policy both during
route selection and immediately before publication, multiplying these operations
per matched route. SDK retries may add requests. Include this traffic in Cosmos
RU/latency planning; no metadata-validation cache is implemented.

## Permissions

`infra/core.bicep` grants, on the `approved-destinations` container only:

| Identity | Role | Why |
|---|---|---|
| Subscription API | Cosmos DB Built-in Data Reader | Approval check on enabled POST/PATCH |
| GitHub deploy identity | Cosmos DB Built-in Data Reader (also on `subscriptions`) | `scripts/verify-evidence-routing.py` preflight in the Evidence workflow |

The built-in reader carries both actions the adapter needs: metadata read
(`Microsoft.DocumentDB/databaseAccounts/readMetadata`) and item read
(`Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/read`).
No application identity is given policy writes.

Worker reads the container through its existing account-wide Cosmos Contributor
assignment, which also lets it write approvals. Narrowing that assignment to
incident writes plus subscription and destination reads is still open; because
CD deploys incrementally, it also needs the live account-wide assignment removed
by hand. Developers listed in `developerPrincipalIds` hold account-wide Cosmos
Contributor and are the operators who write approval records.

Migration is default-deny: inventory existing records, verify ownership and bot
membership, explicitly seed approvals, then enable routing. Existing unapproved
subscriptions remain readable but do not deliver. Destination authorization is
not proof of Telegram ownership or successful delivery.

Limitations: legacy flat notification delivery is unchanged and bypasses this
policy. Already-queued envelopes are not revalidated by Notification Service.
Revocation after the final Worker check can race with publication. Partial
publication retries and duplicate Telegram alerts remain a separate blocker.
Policy outages can stall affected processing until recovered; monitor retries and
dead-letter queues. No security acceptance or live deployment readiness is implied.

The two services retain identical read-only policy adapters because the API has
an isolated Docker build context; regression tests cover both implementations.
