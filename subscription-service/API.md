# Subscription API

Standalone FastAPI CRUD service with tenant-scoped subscription configuration.
The Cosmos-backed Worker reads enabled subscriptions by tenant and exact event
type, then publishes version-1 envelopes to the selected Telegram destinations.
Zero matches preserves incident persistence without publishing notifications.
Subscription changes affect matching when routing-enabled Worker code is active;
the API itself does not publish messages or send Telegram requests.

This branch includes infrastructure and CI/CD integration. Automatic PR checks
run without Azure authentication. Azure preview is manual-only from main with
protected-environment approval; production rollout is also manual, with optional
Worker activation gated separately. See
[rollout configuration](../.github/workflows/SUBSCRIPTION_ROLLOUT.md) and
[Azure preview](../.github/workflows/AZURE_PREVIEW.md).

## Run locally

From `subscription-service`, using Python 3.12:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
$env:AUTH_MODE = 'development'
$env:DEV_TENANT_ID = 'TENANT-DEV'
$env:SUBSCRIPTION_STORE = 'memory'
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Development mode deliberately uses one server-configured tenant without tokens.
Never expose it publicly. Memory storage is ephemeral, per process, and only
selectable through environment configuration in development mode. Unit tests
inject stores/verifiers directly and need no Azure resources.

```powershell
python -m pytest tests -v
docker build -t incident-subscription .
```

The container runs Python 3.12 as non-root UID 10001 on port 8000. Build context
is this service directory. `/docs` exposes the generated OpenAPI documentation.

## Production configuration

| Variable | Purpose |
|---|---|
| `AUTH_MODE` | `entra` by default; unsupported modes fail startup |
| `AUTH_TENANT_ID` | Entra directory issuing tokens |
| `AUTH_AUDIENCE` | Platform API audience, normally `api://<appId>` |
| `TENANT_MAP` | Operator-controlled `clientId:TENANT-A,clientId:TENANT-B` |
| `SUBSCRIPTION_STORE` | `cosmos` by default |
| `COSMOS_ENDPOINT` | Existing Cosmos account endpoint |
| `COSMOS_DATABASE` | Existing database; default `incidents` |
| `COSMOS_SUBSCRIPTIONS_CONTAINER` | New container; default `subscriptions` |
| `AZURE_CLIENT_ID` | User-assigned identity selected by DefaultAzureCredential |

Provision the subscription container separately with partition key `/tenant_id`.
The service never creates databases/containers and never accesses the incident
container by default. Grant its identity Cosmos data access scoped to the
subscription container. No account keys or Telegram bot tokens are used.
The branch includes container and identity provisioning in `infra/core.bicep`
and API deployment in `infra/subscription.bicep`. Entra role/scope assignments
and consent require separately reviewed configuration.

Tokens require RS256 signature validation, unexpired `exp`, matching audience,
and a v1/v2 issuer pinned to the configured Entra directory. `azp`/`appid` resolves
the platform tenant through `TENANT_MAP`. Malformed/duplicate mapping entries
fail startup. Reads require `Subscriptions.Read` or `Subscriptions.Write`;
writes require `Subscriptions.Write`, as application roles or delegated scopes.
`Incidents.Write` alone grants no subscription access.

This is the ingestion-style per-client authorization model: use dedicated
tenant clients. A shared frontend registration needs additional user/group
authorization before supporting users from multiple platform tenants.

## Endpoints

All subscription endpoints require `Authorization: Bearer <token>` in Entra mode.

| Method/path | Result |
|---|---|
| POST `/api/v1/subscriptions` | 201, document and Location header |
| GET `/api/v1/subscriptions?limit=50&offset=0` | Tenant-only `items`, `next_offset` |
| GET `/api/v1/subscriptions/{subscription_id}` | 200 document or 404 |
| PATCH `/api/v1/subscriptions/{subscription_id}` | 200 updated document or 404 |
| DELETE `/api/v1/subscriptions/{subscription_id}` | 204 or 404 |
| GET `/health` | Process liveness; reports storage/auth modes |
| GET `/ready` | Remote store check; 200 ready or 503 not-ready |

Example POST body:

```json
{
  "event_type": "intrusion_detected",
  "channel": "telegram",
  "telegram_chat_id": "-100123456",
  "enabled": true
}
```

`channel` defaults to `telegram`; `enabled` defaults to true. Chat IDs are signed
decimal strings. `event_type` is a trimmed, non-empty exact literal: there is no
pattern evaluation or wildcard routing. No site, severity or analytics filters.
The stored/API identifier is `id`, used as `{subscription_id}` in URLs.
`tenant_id`, UUID-based `id`, `created_at`, and `updated_at` are server-generated;
timestamps are UTC. Extra body fields, including bot tokens, are rejected (422).

PATCH accepts any non-empty subset of the four editable fields; null values
and tenant/ID/timestamp changes are rejected. Example: `{"enabled": false}`.
Foreign-tenant and nonexistent IDs return the same 404 response. Caller-supplied
tenant query parameters never select another partition.

List limit is 1–100; offset is 0–10000. Ordering is by ID. `next_offset` is null
when there is no lookahead row or the next page would start above 10000. The
current page is still returned in full up to the requested limit; this bounded
interface cannot traverse a larger registry beyond its accepted offsets.
Offset pagination is
appropriate for a small subscription registry but is not a snapshot when
concurrent creates/deletes occur. Concurrent PATCH requests use last-write-wins;
ETag conditional updates can be added later if required.

Azure storage failures return a sanitized 503. Liveness does not query storage;
readiness verifies container metadata and a data query with a five-second timeout.

## Delivery prerequisites and assumptions

The API trusts a tenant subscription administrator to select a numeric chat ID.
It does not verify Telegram ownership or reachability. Add an operator-approved
tenant destination allowlist or ownership verification before enabling delivery.
Bot credentials must remain in Notification Service / Key Vault.

Infrastructure, service tests, matching and routed notification delivery are
implemented in this branch. Before production activation, verify storage/RBAC,
Entra roles and consent, approved destinations and tenant subscription migration.
Manual CD prepares compatible Notification code, storage and the API before its
optional Worker-last activation. Existing load-test suppression and incident
persistence remain intact. Live authorization and delivery require separate
verification; local and CI tests do not establish production readiness.
