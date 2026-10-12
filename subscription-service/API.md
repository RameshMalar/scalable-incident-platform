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

## Local image provenance and security limitations

Verified against Docker Hub's official `library/python` registry on 2026-10-11:
`python:3.12.15-slim-trixie` is available and the Dockerfile pins its immutable
multi-platform index digest:
`sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`.
The index selects this Linux amd64 manifest:
`sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe`.
`uvicorn[standard]` is pinned to `0.54.0`. The image upgrades pip to the
PyPI-verified `26.2` release (normalized `26.2.0`, Python >=3.10) before
installing application dependencies, runs `pip check`, then uninstalls pip and
removes `/usr/local/lib/python3.12/ensurepip`. Build-time assertions and
non-root runtime checks verify pip executables, metadata and bundled components
are absent. Application urllib3 2.8.0 and required runtime libraries remain.
UID 10001, port 8000 and the
existing Uvicorn startup command are preserved; no Debian packages are removed.

A fresh local `linux/amd64` build completed with Python 3.12.15 and passed
`pip check`, health/readiness, CRUD/pagination, locally signed RS256 JWT
validation (including expiry and audience rejection), and certificate-verified
HTTPS checks. Development-mode readiness used memory storage; it does not
verify live Cosmos access, Entra configuration or Azure deployment readiness.
The JWT check injected a local signing key rather than contacting Entra.
The existing `apt-get upgrade` and unlocked transitive Python dependencies mean
future builds can differ even with the base image digest pinned.

Trivy 0.75.0 scanned the final local image on 2026-10-11 with unfixed findings
included. Before cleanup: 0 CRITICAL, 48 HIGH, 60 MEDIUM, 61 LOW and 1 UNKNOWN.
After cleanup: 0 CRITICAL, 44 HIGH, 58 MEDIUM, 61 LOW and 1 UNKNOWN.
No findings were added. Removing pip's bundled components removed HIGH
GHSA-6v7p-g79w-8964 (msgpack), CVE-2025-47273 (setuptools),
CVE-2026-97687/CVE-2026-97689 (urllib3), and MEDIUM
CVE-2026-59890 (setuptools)/CVE-2026-97688 (urllib3).
The earlier six pip 25.0.1 findings also remain absent.

The 44 HIGH Debian package findings remain unchanged:
CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410
(util-linux family); CVE-2026-54369 (libacl1); CVE-2025-69720
(ncurses family); CVE-2026-16742 (libsystemd0/libudev1); and CVE-2026-9538
(perl-base). No fixed version is listed for these HIGH findings; perl-base is
marked `fix_deferred`. Lower-severity Debian findings remain in the full report.

The fresh Linux amd64 build passed all 79 API, 73 integration and 79
workflow-security tests, documentation/workflow validation including 53 shell
blocks, and non-root runtime checks for native/application imports, TLS trust,
JWT valid/invalid signatures, expiry/audience rejection, health/readiness,
CRUD and pagination. Its local image index digest is
`sha256:7423fa182809f892cc32a68e800de5b34271439646cebc7b453877c01f78170f`.
Runtime pip installation, `pip check` and `ensurepip` bootstrapping are
intentionally unavailable; dependency checking runs before cleanup during build.
Tests and diagnostic tooling must use separate environments. Deleted base-image
files can remain in lower image layers, though absent from the final filesystem.
Retain the complete scan rather than treating the image as vulnerability-free.
CI thresholds and CVE filtering are unchanged. No security exception is granted:
remaining vulnerabilities require remediation or separate security review before
production use. This local validation neither publishes nor deploys the image.

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


Enabled subscription writes now require operator-approved tenant destinations.
See [destination policy and migration](DESTINATION_POLICY.md). Unapproved
subscriptions remain readable; disabling and deletion do not require approval.
No policy records or Azure role assignments are provisioned automatically.
