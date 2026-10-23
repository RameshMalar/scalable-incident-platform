> Historical report. The current authoritative scope, contracts, test results and blockers are in [CONSOLIDATED_IMPLEMENTATION_REPORT.md](CONSOLIDATED_IMPLEMENTATION_REPORT.md). Earlier per-service user allowlists and per-user Entra app-role assignments below are superseded by the single central registry. Do not apply historical instructions.

# Unified SOC readiness â€” October 19, 2026

**NO-GO for deployment.** Local dashboard code and the approved identity policy
are implemented; shared backend patches and live identity-provider configuration
require review. No partially integrated version is authorized for deployment.

## Approved policy and authentication implementation

Directory: `5ba5ef5e-3109-4e77-85bd-cfeb0d347e82`.
Only approved user: `ded95a62-f8b6-4589-b49a-2041a35d10d7`
(`ramesh.malar_21@u.nus.edu`, confirmed by read-only signed-in-user lookup).
Only approved platform tenants: `TENANT-01`, `TENANT-02`, `TENANT-03`.
Approved permissions: dashboard read/evidence access and controlled simulation.
The policy is packaged in `access-policy.json`; no other user or tenant is added.

The minimum approach is native Streamlit OIDC with Entra, using the existing
dashboard Container App and `streamlit[auth]==1.63.0`. No new Azure compute,
storage account or streaming service is proposed. A dedicated login app
registration and its user role assignments require administrator approval.
The existing API app is not silently repurposed into a browser login client.

Before data reads, the application independently checks the exposed ID token's
RS256 signature, trusted Entra issuer/JWKS, directory, configured client audience,
expiry, user object ID and `Dashboard.Read` role against the packaged policy.
Simulation additionally needs `Incidents.Simulate` in both policy and signed
claims, a delegated API token and the protected backend capability check.
Session variables, unsigned principal headers and UI checkboxes cannot grant
data or simulation authorization. No development bypass is included.

OIDC state/nonce handling is delegated to Streamlit/Authlib. Tokens stay server
side and are never rendered or logged. The expiry check also applies on dialog,
account and refresh reruns, rather than trusting the long-lived login cookie.
An explicit `easyauth` alternative can consume signed tokens from ACA headers,
but native OIDC is the proposed default; Easy Auth is not currently configured.

References: [Streamlit login](https://docs.streamlit.io/develop/api-reference/user/st.login),
[token access and expiry](https://docs.streamlit.io/develop/api-reference/user/st.user),
[ACA authentication alternative](https://learn.microsoft.com/en-us/azure/container-apps/authentication).

## Local dashboard behavior

- Restored previous-period deltas for medium severity, sites, tenants and analytics
  types, alongside the four executive cards. Zero previous values display an
  unavailable delta rather than an invented percentage.
- Incident and evidence ordering uses timestamp descending, then tenant and
  event ID ascending. Gallery pagination resets when its incident snapshot changes.
- Cosmos reads are parameterized and partition-scoped to verified tenant IDs.
  All continuation pages are consumed. No TOP/LIMIT truncates KPI totals.
- Authorized snapshots are shared read-only resources for five minutes, with at
  most two tenant-scope entries. This avoids copying the full DataFrame for each
  evidence authorization check. Views never mutate the shared snapshot.
- Default refresh is manual; automatic options are five/fifteen minutes. Manual
  refresh invalidates the snapshot and has a one-minute per-session cooldown.
  This is cost control, not a distributed authorization or rate-limit mechanism.
- Evidence retrieval rechecks identity and requires the exact URI to be referenced
  by the authorized incident dataset before accessing Blob. Existing account,
  container, HTTPS and traversal checks remain. Blob transfers are capped at the
  configured byte limit plus one byte, with oversize rejection.
- Previews: 640Ã—360 maximum, 48 entries, ten minutes. Compressed originals:
  separate two-entry, one-minute cache (at the default limit, approximately 20 MiB
  maximum cached original bytes). Preview decode rejects images over 16 MP.
  A preview cache miss still downloads the source Blob; no network savings are
  falsely claimed. No thumbnails are uploaded or new storage created.
- Account Setup is separate from incident pages. It exposes only optional
  Telegram preference controls against the proposed self-service API, never
  private chat IDs or bot credentials. It is disabled by default. Frontend tests
  against mocks do not establish real ownership or delivery integration.
- Simulation is hidden from read-only users and disabled by default. Accepted
  API responses and Cosmos persistence are separate states. An ambiguous timeout
  blocks automatic repeat submission until reviewed. No actual event was sent.
- Existing evidence integration, filters, charts, table, Grafana link and all
  uncommitted Grafana/infrastructure contributions are preserved. Live video is
  still a future extension; stored evidence is not labelled as a camera stream.

## Azure state observed read-only

Subscription: Azure for Students, `a3bb9202-635d-4ab5-aaa4-5458f296b64e`.
Resource group: `rg-incident-platform`; region: Malaysia West.

| App | Ready revision | Image digest |
| --- | --- | --- |
| Dashboard | `incident-dashboard--0000032` | `sha256:bd28cd7f7c67625076384c07cca9ea6f039598ed72e4da95650e8f05de3d16fd` |
| Ingestion | `incident-ingestion--0000048` | `sha256:3dedbf5f7f37a0156253a8a0aef26b325739d5dca8697d7badf75f96ab9d0e72` |
| Subscription | `incident-subscription--0000003` | `sha256:b93efd1ce627d0acf272e09e2f25015c37eeab43133879d85d4350f8b57c58f3` |
| Worker | `incident-worker--0000038` | `sha256:df402a3e2215717834df10374abafee0d69e4b6e72c058784df297635ec988da` |
| Notification | `incident-notification--0000020` | `sha256:9049cce06ba8a94d76b1045d152f99a1ab8abcccf6e0056b88991f9f636c788b` |

Registry: `incidentacr4pqaac2c4e4hs.azurecr.io`; repositories are
`incident-dashboard`, `incident-ingestion`, `incident-subscription`,
`incident-worker`, `incident-notification` respectively. All inspected apps are
in Single revision mode. Dashboard has one listed healthy active revision and
100% latest-revision traffic, 0.5 vCPU / 1 GiB, min 0 / max 3 replicas.
Grafana remains `incident-grafana--0000003`; no Grafana settings were changed.

Dashboard identity: client ID `31df7490-4abc-4bb4-8d89-22f6b7dbc7dd`, principal
`ececbe0e-9f93-47a6-8e4b-a56432b7c31d`. Live RBAC reads confirm AcrPull,
evidence-container Storage Blob Data Reader, and Cosmos built-in reader.
Dashboard has no configured auth provider or secrets. The API registration
`fbbe63e1-1f8f-4d4d-b695-0f9982842081` currently has Incidents.Write and
Subscriptions.Read/Write, but no dashboard roles or simulation role/callback.

Other persisted tenant IDs discovered: ANY-TENANT, T, TENANT-CD, TENANT-DEMO,
TENANT-DEV and TENANT-LOADTEST. They are excluded from the approved policy.
Only a DISTINCT tenant-ID query crossed tenant partitions; performance/data
inspection subsequently queried only the three explicitly approved partitions.

## Dataset and cost validation

| Approved tenant | Persisted rows | Query seconds | DataFrame bytes | Query RU |
| --- | ---: | ---: | ---: | ---: |
| TENANT-01 | 36,798 | 3.779 | 18,930,386 | 1,249.39 |
| TENANT-02 | 36,737 | 2.927 | 19,174,420 | 1,247.47 |
| TENANT-03 | 36,747 | 2.609 | 19,183,465 | 1,247.80 |

110,282 rows were read without truncation: ~57.3 MB of DataFrame data and
3,744.66 RU for these snapshots. These are live records, not test fixture totals.
No incident bodies or images were exported. DataFrame bytes are not peak process
RSS, and local latency is not the deployed 0.5-vCPU browser experience.

The reads consumed existing provisioned Cosmos capacity. New full snapshots can
compete with ingestion and may affect costs depending on account configuration.
No new resources, throughput settings or scale limits were created or changed.
Default manual refresh and five-minute caching reduce repeat work; they do not
make full-history queries free. Remaining student credit was not established:
the existing budget query returned no budget entries, which is not proof of
available credit. Confirm remaining credit/spending limit before approval.

## Deployment gates â€” all must pass

1. Ingestion owner approves/applies the exact proposed simulation-auth patch;
   anonymous, wrong-role, foreign-user and foreign-tenant POSTs are rejected.
2. Subscription/Worker/Notification owners approve and implement the ownership,
   user-bound registry and send-time eligibility contracts in
   `SHARED_BACKEND_REVIEW.md`. Verify subscribe/unsubscribe, queued messages,
   concurrency and legacy-message migration without sending real notifications.
3. Approve a dedicated Entra login client, exact callback
   `https://incident-dashboard.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback`,
   single-directory settings, appropriate ID/API token role assignments and
   delegated API consent. Only the approved user may be assigned.
4. Inject real OIDC TOML through an ACA secret volume (same cookie secret on all
   replicas), not Docker COPY or source control. Use
   `auth.secrets.example.toml` only as schema/reference. No real secret was generated,
   retrieved, printed or stored in this working tree.
5. Run live sign-in and negative-user/tenant checks, browser layout and memory/
   concurrency tests. No real simulation or bot delivery without separate consent.
6. Confirm remaining budget and acceptable query/refresh impact. Record a fresh
   rollback snapshot immediately before deployment; observed revisions may change.
7. Build/scan an immutable dashboard image only after review, record its digest,
   then present fully resolved dashboard-only commands for explicit deployment
   approval. Do not run the current broad CD workflow: it redeploys backends/jobs.

The simulator settings must remain `ENABLE_EVENT_SIMULATOR=false` and
`SIMULATOR_AUTH_VERIFIED=false`. Telegram remains
`ENABLE_TELEGRAM_ONBOARDING=false` until end-to-end contracts are verified.
New dashboard config: `DASHBOARD_AUTH_MODE=oidc`,
`DASHBOARD_AUTH_CLIENT_ID=<approved login client ID>`,
`SIMULATOR_INGESTION_ORIGIN=https://incident-ingestion.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io`,
`SUBSCRIPTION_API_ORIGIN=https://incident-subscription.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io`.
Retain existing Cosmos/Blob endpoints, container names, managed identity and RBAC.
Native OIDC requires no new dashboard Cosmos/Blob write permission or bot secret.

## Deployment command shape â€” blocked, not executed

There is no new approved image digest or login app ID yet, so exact executable
deployment commands cannot honestly be finalized. After **all** gates above pass,
the dashboard-only image update has this shape; auth secret-volume configuration
must be included in the separately reviewed deployment manifest:

```powershell
# BLOCKED: fill only from the reviewed deployment manifest, not guessed values.
az containerapp update --subscription a3bb9202-635d-4ab5-aaa4-5458f296b64e `
  --resource-group rg-incident-platform --name incident-dashboard `
  --image $ApprovedImmutableDashboardImage `
  --set-env-vars "DASHBOARD_AUTH_MODE=oidc" "DASHBOARD_AUTH_CLIENT_ID=$ApprovedLoginClientId" `
    "ENABLE_EVENT_SIMULATOR=false" "SIMULATOR_AUTH_VERIFIED=false" "ENABLE_TELEGRAM_ONBOARDING=false"
```

This command alone is **not** the complete unified release: shared service
changes and auth configuration have separate owner/security gates. No deployment
execution approval is requested while those gates fail.

## Rollback procedure â€” retained image, security caveat

Known-good pre-SOC image:
`incidentacr4pqaac2c4e4hs.azurecr.io/incident-dashboard@sha256:bd28cd7f7c67625076384c07cca9ea6f039598ed72e4da95650e8f05de3d16fd`.

In current Single mode, a reviewed rollback can copy the retained revision:

```powershell
# REQUIRES EXPLICIT APPROVAL. Not executed.
az containerapp revision copy --subscription a3bb9202-635d-4ab5-aaa4-5458f296b64e `
  --resource-group rg-incident-platform --name incident-dashboard `
  --from-revision incident-dashboard--0000032
```

If retention removes that revision, create a revision using the saved immutable
image and recorded previous template. Preserve/review app-scoped secrets, auth
and ingress separately; image rollback does not restore them automatically.

**Security limitation:** the old image queries every tenant and has no app-level
user mapping. Do not reopen it publicly as a security rollback. First close access
or use a separately approved maintenance restriction, and reopen only after tenant
authorization is preserved. Under native OIDC, the old app would not execute the
new guards merely because a secrets file is mounted. No data migration is needed;
an image rollback cannot retract already sent Telegram messages or persisted tests.

Reference: [Azure revision copy](https://learn.microsoft.com/en-us/cli/azure/containerapp/revision?view=azure-cli-latest),
[revision/application configuration boundaries](https://learn.microsoft.com/en-us/azure/container-apps/revisions).


## Final test results
The original **368-test baseline passed again before these changes**.
Final offline run: pinned Streamlit 1.63.0, PyJWT 2.15.0 and Authlib 1.9.1; Python 3.14. Tests use mocks/local signing keys, not live event or bot calls. Live Entra/browser login remains unverified.
- dashboard: 42 passed; exit code 0.
- ingestion: 71 passed; exit code 0.
- subscriptions: 98 passed; exit code 0.
- notification: 80 passed; exit code 0.
- worker: 166 passed; exit code 0.
- load-simulator: 86 passed; exit code 0.
- common-model: 22 passed; exit code 0.
- producer-sdk: 54 passed; exit code 0.
- proposed-ingestion-only: 84 passed; exit code 0.

Applied-tree total: **619 passed**. The proposed-ingestion-copy result is separate, not counted twice.

All listed suites passed. This does not replace live identity, ownership, notification-revocation and deployment gates.

No image was built, Azure permission/configuration changed, backend patch applied, subscription created, event triggered, notification sent, or Git staging/commit/push/merge performed.

### Python 3.12 compatibility verification
All 42 dashboard, authorization and account-setup tests also passed in 14.89 seconds on the installed Windows Python 3.12 runtime, using a clean isolated environment installed from the exact dashboard requirements plus pytest 9.1.1. This repeats the same 42 cases for compatibility; it does not increase the 619 unique applied-tree test total. Linux image build/runtime, browser-based live Entra login and Telegram ownership/delivery remain gated and unverified.


### Final account opt-out regression
The final UI refinement permits an authenticated owner to disable alerts even if their ownership verification was revoked; enabling remains denied. The corresponding proposed API contract explicitly preserves this distinction. All **43 dashboard/security/account tests pass on Python 3.12** after this refinement. The earlier 619-case full-suite result remains recorded; these 43 cases supersede its 42 dashboard cases. No backend or Azure changes were made.
