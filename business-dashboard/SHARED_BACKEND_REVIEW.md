> Historical report. The current authoritative scope, contracts, test results and blockers are in [CONSOLIDATED_IMPLEMENTATION_REPORT.md](CONSOLIDATED_IMPLEMENTATION_REPORT.md). Earlier per-service user allowlists and per-user Entra app-role assignments below are superseded by the single central registry. Do not apply historical instructions.

# Shared-service approval request â€” not applied

The user-approved identity policy is in `access-policy.json`. No Azure role,
Entra application, subscription record, destination record or service was changed.

## 1. Exact ingestion patch for owner approval

Review `simulation-authorization.proposed.patch`. It changes only:

- `ingestion-service/app/main.py`: protect the existing simulation POST; use
  the authorized tenant rather than fixed `TENANT-01`; add a protected read-only
  capabilities endpoint. The production `/api/v1/incidents` contract is unchanged.
- New `ingestion-service/app/simulation_auth.py`: separate signed Entra delegated
  token validation, approved frontend-client and user-to-tenant mappings,
  `Incidents.Simulate` permission, expiry/audience/issuer checks; disabled unless
  explicitly enabled in Entra mode. There is no development bypass.
- `ingestion-service/tests/test_ingestion.py`: existing simulation behavior tests
  explicitly inject an authorized mock operator.
- New `ingestion-service/tests/test_simulation_authorization.py`: default denial,
  missing token, expiry, wrong audience/issuer/client/user/tenant, service-principal
  rejection, existing-purpose preservation and one mock publish per successful POST.

The patch was tested in a copied tree, not applied to the teammate service.
Anonymous simulation intentionally becomes 404 when disabled, 401 without a
token when enabled, and 403 for unauthorized users/tenants. Demo callers relying
on anonymous simulation must migrate. Regular ingestion producer auth is untouched.

Required backend settings, after owner approval and integration tests:

```text
AUTH_MODE=entra
AUTH_TENANT_ID=5ba5ef5e-3109-4e77-85bd-cfeb0d347e82
AUTH_AUDIENCE=api://fbbe63e1-1f8f-4d4d-b695-0f9982842081
ENABLE_EVENT_SIMULATOR=false
SIMULATION_CLIENT_IDS=<approved dashboard login client ID>
SIMULATION_USER_TENANTS={"ded95a62-f8b6-4589-b49a-2041a35d10d7":["TENANT-01","TENANT-02","TENANT-03"]}
```

Add an API `Incidents.Simulate` **User-only** role and assign it only to the
approved operator; consent to the existing delegated `user_impersonation` scope.
Never reuse `Incidents.Write` as permission to simulate. No API app registration
or permission assignments have been edited.

The dashboard checks the protected capabilities GET before making its one POST.
An old/unprotected backend has no such endpoint, so even accidental dashboard
feature enablement cannot submit through that backend. Only the existing types
are selected. Their fixed severities and evidence associations remain authoritative;
the current API does not support arbitrary severity/evidence selection. No claim
that unsupported inputs work is made.

## 2. Subscription API owner â€” exact proposed self-service contract

This contract is **not implemented in the shared API**. The account page is
implemented against mocks and disabled until the server contract is supplied.
Existing tenant-manager CRUD endpoints must remain unchanged.

Every `/me` request uses a signed delegated API token with a proposed
`Subscriptions.SelfManage` User-only permission and an allowlisted frontend
client. Bind `oid` + directory to the reviewed user policy; validate the selected
`tenant_id` query parameter against that user's tenant list. Client-wide
`TENANT_MAP` alone is insufficient. Never accept a caller-supplied owner ID.

| Method and path | Exact request / sanitized response |
| --- | --- |
| GET `/api/v1/me/telegram?tenant_id=...` | `{contract:"self-service-v1", tenant_id, verified:boolean, enabled:boolean, event_types:string[], supported_event_types:string[]}`; no chat ID, bot token or foreign user's records |
| POST `/api/v1/me/telegram/challenges?tenant_id=...` | Body `{}`; return `{deep_link:"https://t.me/<approved-bot>?start=<one-time-token>", expires_at:<UTC>}` |
| PUT `/api/v1/me/telegram/preferences?tenant_id=...` | Body `{enabled:boolean,event_types:string[]}`; return the GET representation. Reject unknown fields, unsupported events and foreign tenants. Enabling additionally requires verified ownership and an approved destination; an authenticated owner may always disable their own subscriptions. Repeating the same PUT is idempotent. |
| POST `/api/v1/telegram/webhook` | Telegram Update body plus `X-Telegram-Bot-Api-Secret-Token`; never a browser-created verification assertion. Authenticate this callback independently of Entra. |

Affected ownership/files before implementation: Subscription API `app/auth.py`
(or a separate user-auth module), `app/main.py`, `app/models.py`, `app/store.py`,
new challenge/link logic and corresponding tests. No changes are applied here.

### Ownership and registry invariants to implement

1. Use a CSPRNG one-time token (32 bytes, base64url; within Telegram's start-token
   limit). Store only its SHA-256 digest with directory, owner OID, platform tenant,
   expiry and consumed status. A new challenge invalidates the owner's old one.
2. Authenticate the Telegram webhook using a high-entropy secret held as an Azure
   secret reference. Only bind a private chat with matching sender/chat user IDs.
   Validate token expiry and atomically consume it; replay and webhook duplicates
   must not create another link. No bot API send is necessary for verification.
3. Keep the existing bot token in Notification Service; it is never copied into
   the dashboard or browser. Configuring the existing bot webhook requires separate
   approval and an inventory of any current webhook/getUpdates consumer.
4. Store typed challenge/link records in the existing subscription registry, using
   the existing `/tenant_id` partition. Use ETags/transactional batches to consume
   a challenge once. No new Azure service is proposed. Validate schema/index/RBAC
   compatibility before writing these supplementary document types.
5. Use deterministic subscription IDs derived from tenant + owner OID + exact
   event type. Store owner and ownership-verification metadata on self-service
   subscriptions. Atomic PUT updates desired enabled states and disables removed
   types; no new UUID subscription is created on every login or save.
6. Proof of chat ownership does not bypass the existing operator-approved tenant
   destination policy. Both checks must succeed before enabling delivery. Unapproved
   chats stay pending rather than silently adding destination approvals.
7. Never place private chat IDs or bot credentials in responses, links, exceptions
   or logs. The public bot username and one-use challenge link are sufficient.

## 3. Worker / Notification owners â€” eligibility and unsubscribe semantics

Existing Worker matching verifies enabled subscriptions and approved destinations
when publishing. Notification Service currently trusts queued routes and does not
re-read subscription state before dispatch. Consequently queued messages can be
sent after unsubscribe. This cannot be fixed solely in a Streamlit form.

Required reviewed changes: Worker `app/subscription_store.py` / routing validation
must require the new ownership proof for self-service subscriptions without
silently reclassifying old administrator records. Notification `app/processor.py`
and startup wiring need a read-only eligibility adapter that checks subscription
ID, tenant, event type, destination, enabled/verified owner state and destination
approval **before each delivery attempt**. Disabled/revoked routes complete
without sending; lookup outages retry through the existing queue/DLQ behavior.
Do not change sendPhoto, evidence lookup, text fallback or event-purpose rules.

An already in-flight Telegram send cannot be recalled by an ordinary disable
write. If the requirement means zero deliveries after the unsubscribe response,
the shared contract must also implement coordinated dispatch leases/revocation:
disable prevents new leases and the unsubscribe response waits for prior leases
to finish. Otherwise explicitly document that pre-existing in-flight sends can
complete. Do not claim a stronger guarantee from a single enabled flag.

Legacy queued messages also require an owner-approved migration/drain strategy;
otherwise the legacy global-chat branch bypasses per-user preferences. No routing
flag, legacy acceptance policy, queue, DLQ or bot setting was changed here.

## Required approval boundary

Approve the exact ingestion patch separately from the self-service API and
delivery contract changes above. These affect teammate-owned services and
existing integrations. The user's request explicitly says to prepare the proposed
patch and stop before applying shared backend changes. Production remains NO-GO
until the owners approve, implement and validate these contracts. This approval
does not authorize deployment or real Telegram/event tests.
