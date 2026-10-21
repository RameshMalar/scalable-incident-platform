# Consolidated SOC, access management and Telegram implementation review

Review date: 19 October 2026. Checkout: `scalable-incident-platform-grafana`, branch `feature/grafana-observability`.

**Recommendation: NO-GO for the unified demonstration release.** Local dashboard code and offline domain tests are available for review. The shared Subscription API, ingestion and notification integration is not installed. Native Entra login, Telegram ownership, and live delivery have not been verified. Passing mock tests does not establish those release gates.

This is the authoritative consolidated report. `SOC_REVIEW.md`, `UNIFIED_SOC_READINESS.md` and `SHARED_BACKEND_REVIEW.md` retain historical findings; their earlier separate user-to-tenant maps/app-role assignment proposals are superseded here. There is one application role model, one proposed authorization registry in the existing Subscription API, and the existing notification pipeline. No additional account store, service or Azure resource is introduced.

## Approved identity — unchanged

- Directory: `5ba5ef5e-3109-4e77-85bd-cfeb0d347e82`.
- Platform Admin object ID: `ded95a62-f8b6-4589-b49a-2041a35d10d7`.
- Tenant scope: `TENANT-01`, `TENANT-02`, `TENANT-03`; dashboard/evidence reads and controlled test simulation only in those tenants.
- `access-policy.json` remains byte-for-byte unchanged: SHA-256 `7bf104a7eac2a3579c18f4fe212436d101fca7f0e069391e6df8f7e7ff9ac09b`.
- No team account was added. UUIDs in tests are synthetic fixtures, not proposed grants. Other persisted tenant IDs remain excluded.

## Individual access-management traceability

“Local” means implemented in dashboard code/offline tests. “Proposal” means review-only backend source/contract, not an effective endpoint or a production guarantee.

| # | Requirement | Implementation and evidence | Remaining gate |
|---|---|---|---|
| 1 | Preserve approved Admin | Packaged policy unchanged; `access_roles.parse_grant` protects exact root role/status/tenant scope. `test_bootstrap_exact_mapping_and_no_automatic_registration`, `test_immutable_admin_and_compare_and_swap`. | Verify configured login belongs to this same directory and object ID. |
| 2 | Secure admin-controlled registry | `review-proposals/central_registry.py`: existing Subscription Cosmos container, typed records in reserved `__platform_access__` partition, immutable approved bootstrap, atomic grant+audit transaction, ETag/version concurrency. No browser/session grants. | Owner-approved routes, delegated JWT dependency, persistence validation and integration tests. This is not wired into Subscription Service. |
| 3 | Three application roles | `access_roles.py`: Platform Admin = read/simulate/manage access/self subscription; SOC Operator = read/simulate/self subscription; SOC Viewer = read/self subscription. Active grant and tenant assignment are always required. `test_three_roles_and_simulator_authorization`, unknown-role rejection. | Backend must consume this same role definition. No second role/permission map. |
| 4 | Admin add/update/disable/revoke verified identities | Separate `admin_access.py` forms require fresh Admin authorization and explicit confirmed action. Registry proposal allows new registration only by original approved Admin, requires a verified identity receipt and approval reference, protects root, audits mutations, and rejects stale writes. Tests cover non-Admin/self escalation, unverified UUIDs, concurrency and immutable root. | Implement Entra identity receipt and routes; UUID syntax alone never counts as verification. No new user approval has been granted. Revoked records cannot be silently re-enabled; disabled records can be explicitly re-enabled. |
| 5 | Specific tenant assignment | Forms offer current Admin tenant scope only. Parser caps all grants to the approved three tenants; registry rejects out-of-scope management/expansion. Tenant removal takes effect on next check. | Validate persistent tenant mutation routes with Cosmos. |
| 6 | Explicit operator simulation | Dashboard defaults off and checks identity/permission/tenant, operator confirmation and protected capabilities before one POST. Revised `simulation-authorization.proposed.patch` checks central active Admin/Operator grant on every capability/POST, validates delegated token, binds event tenant; default off. | Actual ingestion simulation endpoint remains unmodified. Apply reviewed patch only after owner approval and revalidate API denial tests before enabling. |
| 7 | Server authorization: query/evidence/simulation | Streamlit server `require_operator` precedes incident reads, cached data use, evidence retrieval and UI actions. Central mode fetches `/access/me` fresh and validates response subject/directory/status/role/tenants. Evidence URI must belong to authorized persisted records before bounded MI Blob fetch. Simulator patch protects backend directly. | Central routes and backend patch are not installed. This requirement is not end-to-end complete until backend integration passes. A cached image already downloaded to a browser cannot be retracted by revocation. |
| 8 | Deny unknown users | Unknown/unregistered/inactive/foreign tenant/directory denied; central mode has no packaged-policy fallback during an outage. Bootstrap mode only accepts the existing approved policy, with signed identity. Tests cover unknown, outage, malformed role, subject substitution and direct Admin entry. | Verify live token issuance and protected-origin configuration. |
| 9 | No Azure management roles for ordinary users | Team users authenticate through Entra; application authorization is registry data. No Azure CLI, subscription IAM, Cosmos/Blob roles or resource setup required from team users. Only server managed identities access data. | Platform owner must configure login/API consent and review existing service identity scope; no assignments made in this work. |
| 10 | Separate authenticated Admin interface | `app.py` registers Admin Access Management independently of incident monitoring for authorized Admin only. `require_admin` repeats server checks; direct Viewer entry denied, default-disabled page issues no API requests. | Enable central mode only when backend contract is verified; exercise real Admin form flow after review. |
| 11 | Telegram optional and separate | Account Setup remains separate from incident monitoring/Admin access. Role authorization works without any Telegram subscription. Ownership alone does not enable notifications. Tests cover absent subscription with allowed dashboard access and unverified activation denial. | Complete existing Subscription API self-service lifecycle below. Optional user opt-in does not make platform lifecycle optional. |
| 12 | Authorization regression tests | `test_central_access.py`, `test_security.py`, `test_dashboard.py`: roles, cross-tenant denial, revocation, removed tenants, outages, stale versions and unauthorized requests. Revised copied-service tests cover backend simulation denial even with stale signed role claims. | Cosmos transaction/HTTP integration and real Entra login acceptance tests remain required. |

## Telegram lifecycle — separately traced, mandatory release capability

`account_setup.py` is a local authenticated UI against the proposed existing-API extension. `review-proposals/telegram_lifecycle.py` is a tested domain reference with explicit store ports. It is not a running webhook, Cosmos adapter, or replacement notification service. Feature flag remains false unless explicitly configured. All tests use local stores and make no Telegram requests.

| Lifecycle step | Local implementation / tests | Backend work still required |
|---|---|---|
| Authenticated account setup | Fresh identity/tenant checks; no chat-ID, bot-token or arbitrary owner entry. Foreign-tenant response rejected. | Validate delegated API token and resolve central registry on every `/me` route; derive owner exclusively from token. |
| Secure bot linking | 32-byte random five-minute `/start` challenge; hash-only store port, private chat/sender identity match, webhook secret constant-time comparison, one-time atomic consume port. Tests reject replay, expiry, forged callback and group chat. | Existing approved bot ownership/configuration must be verified; secret-protected webhook routed and rate/size limited; durable atomic consumption, unique binding, no token/private-ID logging. No webhook registered here. Deep-link forwarding remains a bearer-challenge risk; approve expiry and any additional confirmation UX before activation. |
| Explicit opt-in | New verified binding defaults disabled; consent is a separate PUT. Tests prove ownership is not opt-in. | Persist consent audit and versions in existing Subscription API; successful linking must not send a notification or auto-enable. |
| Activate subscriptions | Enable requires active authorized tenant grant, verified owner and approved destination. | Adapt to existing Subscription/destination models and approval rules. Manual destination allowlisting alone is not proof of ownership. |
| Preferences | Supported event allowlist and idempotent self-service PUT. Account UI supports eligible event selection. | Preserve existing event type semantics and validation; reject unknown request fields. |
| Authorized delivery | Domain eligibility checks fresh user access, tenant, ownership, destination approval, opt-in, enabled state, event type and production purpose. Tests deny wrong tenant, disabled/revoked owner, unselected events and test-purpose sends. | Worker must use existing routing plus central eligibility; notifier must recheck immediately before every initial/retry send. Current notifier trusts queued envelopes, so this is a blocker. Registry outages must fail closed into existing retry/DLQ behavior, never fail open or silently send. |
| Unsubscribe | UI and domain support idempotent opt-out, even if ownership was invalidated. Tests ensure subsequent eligibility false and unrelated dashboard permission unchanged. | Persist via existing API and recheck queued delivery. Define a dispatch-lease/drain contract if “no sends after unsubscribe returns” is required; a send already in flight cannot be guaranteed cancelled by a pre-send check alone. |
| Revocation/tenant removal | Central access takes effect on next operation, independent of a still-valid login token; registry failure is not permission. | Integrate fresh checks into publication and dispatch. Decide documented in-flight semantics before demo acceptance. A revoked user cannot use authenticated account routes; Admin revocation must immediately suppress eligibility. |

Existing Subscription API, notification sender, purpose filtering, retries, DLQ and image/evidence send behavior are untouched. Their regressions pass, but this does not prove the new lifecycle is integrated with them.

## One backend integration contract for owner review

Use existing Subscription Service/container. Preserve legacy client-based subscription CRUD and producer contracts unchanged. New delegated-user endpoints must have a separate user-auth dependency rather than reuse a client-wide `TENANT_MAP` as user authorization. Validate signature/RS256, issuer, audience, expiry, directory, user OID, approved dashboard client and `user_impersonation` delegated scope; deny application-only tokens. User application roles come from central registry, not extra per-user Azure/Entra resource assignments.

| Method / path | Required behavior |
|---|---|
| GET `/api/v1/access/me` | Fresh active registry grant of authenticated user plus `contract:"central-access-v1"`. Response fields: directory_id, object_id, role, tenants, status, version. Unknown/inactive = 403; invalid identity = 401; unavailable = 503. No cache/fallback. |
| POST `/api/v1/access/identity-proof` | Empty body; validate a genuine delegated user token and record short-lived identity-only receipt bound to OID/directory. Does not grant access or register a dashboard user. Needed for Admin registration; not implemented in current dashboard/client or backend proposal. Review public sign-in landing flow for unregistered users. |
| GET `/api/v1/admin/access/users?cursor=<OID>` | Fresh Access.Manage; cursor/page size bounded (default50/max100), stable OID ordering, sanitized public grant fields. UI supports next/first pages. |
| POST `/api/v1/admin/access/users` | Original approved Admin only; verified identity receipt + explicit approval reference + approved role/tenants. Atomic create+audit; no upsert or automatic directory enrollment. |
| PATCH `/api/v1/admin/access/users/{OID}` | Fresh Admin and managed tenant scope; expected_version and reason mandatory. Only role/tenants/status fields; optimistic concurrency, atomic audit; root immutable; privilege expansion reserved original Admin. |
| GET `/api/v1/me/telegram?tenant_id=...` | `self-service-v1`, tenant_id, verified:boolean, enabled:boolean, event_types:string[], supported_event_types:string[]. No chat ID/token. |
| POST `/api/v1/me/telegram/challenges?tenant_id=...` | Empty body; secure one-time deep link and expiry only. |
| PUT `/api/v1/me/telegram/preferences?tenant_id=...` | Only enabled:boolean,event_types:string[]. Derive owner; fresh tenant authorization, ownership/destination checks for enable; idempotent disable. Return GET representation. |
| POST `/api/v1/telegram/webhook` | Verify Telegram secret header, private sender identity and bounded body; atomically consume challenge/bind owner. No authentication assertion from browser. |

`central_registry.py` supplies domain and Cosmos batch adapter; `telegram_lifecycle.py` supplies domain checks only. HTTP route wiring, delegated authentication middleware, identity-proof storage, durable Telegram store, legacy record compatibility and routing integration are deliberately not represented as completed. Before installing, review reserved-partition query isolation so legacy “list all” operations cannot leak access/audit/proof records. Validate existing container partition path `/tenant_id`, unique constraints and identity write scope; no container is created automatically. Treat corrupt/absent container as unavailable, not an authorization grant. Add durable concurrent-write/replay tests and audit retention/redaction decisions.

The revised simulation patch is an exact patch against the existing ingestion files, tested in a copied tree. It replaces the prior patch artifact; `SIMULATION_USER_TENANTS` is no longer used, and signed token role claims alone cannot authorize simulation. It calls the same Subscription API `/access/me` on every operation. It preserves production ingestion, test purpose, supported types and evidence associations.

Shared-service changes remain unapplied because the supplied instruction says: **“If implementing backend authentication would affect teammate-owned services or existing integrations, prepare the exact proposed patch and stop for approval before applying it.”** This is the reason the full end-to-end capability remains blocked. No claim is made that deploying dashboard files alone closes the existing backend simulator bypass.

## Dashboard behavior and production considerations

- Existing SOC theme, executive cards and comparison deltas, charts, filtering, evidence gallery, full incident table, image expansion and Grafana navigation remain. No camera/inference/streaming pipeline is added; live video remains future work.
- Incidents come only from persisted Cosmos records. All continuation pages are consumed within authorized tenant partitions using parameterized predicates. Latest-first order is timestamp descending, then tenant/event ID ascending. HTTP202 is acceptance, not persistence proof.
- Shared read-only snapshots are capped at two entries/300 seconds; manual refresh is rate limited and automatic refresh choices are controlled. Central authorization remains fresh even on cache hits. Many overlapping tenant scopes can reduce snapshot reuse: measure concurrency before increasing refresh rates.
- Earlier read-only measurement: 110,282 approved-tenant records, roughly57.3MB raw JSON/3,744.66RU per complete three-tenant snapshot. Individual tenant query times were2.609–3.779s. This is historical evidence, not a new load test or a multiuser memory guarantee. Pandas/sorting/image memory under the existing1Gi container still needs runtime observation. No new Cosmos read/load test was run in this consolidation.
- Evidence uses existing managed identity restrictions and persisted URI association. Preview640x360,48 cached previews/600s; originals2 entries/60s; bounded Blob read and16MP preview cap. No public SAS generation, credentials or bot secrets introduced.
- Streamlit1.63.0/PyJWT2.15.0/Authlib1.9.1 tested on Windows Python3.12; modern `width='stretch'` used. Linux image/runtime and browser behavior remain unverified because no build/deploy was performed.

## Test report

Current machine-readable results: `TEST_RESULTS.json`. Commands used existing isolated Python environments and pytest with cache disabled; no event/notification endpoints called. Dashboard tests exercise staged copies identical to the installed local dashboard modules; backend tests exercise unchanged services. Proposal tests are explicitly separate.

| Suite | Result |
|---|---:|
| Dashboard, auth, Account Setup, central-access proposal and Telegram domain | 78 passed (Python3.12 pinned dashboard requirements) |
| Existing ingestion | 71 passed |
| Existing Subscription API | 98 passed |
| Existing Notification Service | 80 passed |
| Existing worker | 166 passed |
| Existing load simulator | 86 passed |
| Common event model | 22 passed |
| Producer SDK | 54 passed |
| **Combined dashboard/domain + unchanged backend regressions** | **655 passed** |
| Revised simulator patch in copied service | **90 passed**, separately; overlaps baseline ingestion and is not added to655 |

The dashboard/domain78 include review-only registry and Telegram unit tests; do not interpret655 as655 production integration assertions. Earlier368 baseline passed previously; the current service suites supersede previous619/620 totals. Existing FastAPI/Starlette httpx deprecation warning remains; no backend dependency change made. An initial copied-service collection failed for an omitted local script fixture; restoring existing fixture copies resolved it. Tests do not verify live auth, Cosmos transactional behavior, webhook delivery, send-time suppression, Linux image, UI performance at production concurrency or budget headroom.

## Deployment readiness, configuration and rollback

1. Obtain shared-owner approval, integrate/review the new Subscription API routes/stores and revised ingestion patch, then worker/notifier central eligibility checks. No new user accounts until exact verified OIDs and assignments are explicitly approved. Preserve root mapping and current production routes.
2. Approve a dashboard confidential Entra login client and exact redirect URI from `auth.secrets.example.toml`, consent to the existing API delegated scope, and allowlist that client in backend user-token validation. Actual login client ID/secret and consent are not configured. No per-user Azure management role is needed.
3. Mount real Streamlit `[auth]` secrets server-side at `/app/.streamlit/secrets.toml` (never commit/build into image): client ID/secret, strong cookie secret, exact callback, directory metadata, expose ID/access tokens. Configure `DASHBOARD_AUTH_MODE=oidc` and `DASHBOARD_AUTH_CLIENT_ID=<approved login client ID>`. Secure sign-in/logout and same-subject API token behavior must be tested.
4. Configure `SUBSCRIPTION_API_ORIGIN=https://<existing approved Subscription API host>` on dashboard and proposed ingestion client. Set `ENABLE_CENTRAL_ACCESS=true` only after `central-access-v1` is installed/verified; use it for the unified release. With false/unset, only the packaged approved bootstrap identity can enter and team management remains disabled. Do not use bootstrap mode as a substitute for revocation during a central outage.
5. Keep `ENABLE_EVENT_SIMULATOR=false`, `SIMULATOR_AUTH_VERIFIED=false`, `ENABLE_TELEGRAM_ONBOARDING=false` until each corresponding backend gate passes. Later simulator enablement requires configured ingestion HTTPS origin, protected capabilities, backend `AUTH_MODE=entra`, matching `AUTH_TENANT_ID`/`AUTH_AUDIENCE`, and `SIMULATION_CLIENT_IDS=<approved login client ID>`. No per-service user map.
6. Preserve existing Cosmos/Blob/Grafana environment settings and managed identities. Earlier read-only inventory confirmed dashboard Blob reader, Cosmos reader and ACR pull. Subscription service identity write access to the existing subscription partition and notification identity eligibility-read scope require review; do not grant ordinary users data-plane/management roles. Ownership webhook secret must be mounted server-side; bot token remains with existing bot owner/notifier. No permission changes performed.
7. Run route/persistence/concurrency/tenant/revocation tests with reviewed staging configuration, then receive approval for any actual controlled event or Telegram test. Verify existing event-purpose/retry/DLQ/evidence behavior and in-flight unsubscribe semantics. Confirm Azure for Students budget headroom; no new-resource or budget assumption is made.
8. Only then prepare/approve a scoped dashboard image/deployment and any separately approved shared-service release. Do not trigger the broad existing CD workflow: it also changes services/jobs. This consolidated feature cannot be deployed independently as a fully integrated dashboard-only release. A read-only disabled-feature dashboard can technically use existing data, but would not meet the complete demonstration scope.

Earlier read-only Azure inventory recorded active dashboard revision `incident-dashboard--0000032`, Single mode/100% traffic,0.5CPU/1Gi,min0/max3. Retained image:

`incidentacr4pqaac2c4e4hs.azurecr.io/incident-dashboard@sha256:bd28cd7f7c67625076384c07cca9ea6f039598ed72e4da95650e8f05de3d16fd`

These values were not re-queried during consolidation and must be checked again immediately before any approved deployment. No new immutable image exists because no build was run. Exact executable release commands cannot be finalized without approved image/client/secrets and successful gates. Historical command templates in `UNIFIED_SOC_READINESS.md` remain non-executable review references.

Rollback options: close/restrict dashboard access first, then use the retained revision/image under separate approval; preserve/review app-level auth/secrets/ingress independently of revision rollback. The old image has no app tenant authorization, so restoring it publicly would reopen data exposure. Registry revocations/Telegram opt-outs must not be undone by dashboard rollback. Backend rollout needs compatible additive records and its own retained revision/traffic plan; do not restore consent or delete access audits to roll back presentation changes. No incident data migration is required for dashboard rollback.

No deployment, build, Azure permission/configuration change, live incident, Telegram message, staging, commit, push or merge was performed. Teammate backend/infra/Grafana files remain unchanged. Stop for review.
