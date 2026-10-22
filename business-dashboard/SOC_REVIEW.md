# SOC dashboard — local review

Scope: `business-dashboard` on `feature/grafana-observability`. The pre-existing
uncommitted Platform Observability link is preserved. Grafana, infrastructure,
ingestion, worker, subscription and notification services are unchanged.

## Behavior

- Navy theme with cyan, blue, purple and amber accents, responsive KPI grid,
  text-labelled severity and escaped incident values in HTML cards.
- Total, critical, high and latest-timestamp KPIs use only queried Cosmos rows
  after the existing tenant, analytics, severity and date filters. Site, tenant,
  medium severity and analytics totals, charts and period insights remain.
- The feed shows eight newest matching persisted records, their event IDs,
  tenant, site, camera/source, UTC timestamp and corresponding evidence previews.
  Missing or unreadable evidence does not remove the incident. Expansion uses
  the existing dialog with the full image.
- Manual refresh is the default. Optional 60-second or five-minute refresh uses
  a Streamlit fragment; there is no background event submission or tight loop.
  Cosmos reads retain the existing 60-second cache. The displayed refresh time
  is the last successful query time, not the most recent widget interaction.
  A manual refresh within the cache TTL may reuse the same snapshot.
- Gallery categories come only from filtered records with references accepted
  by the existing Blob store. No missing categories or demonstration images are
  inserted. One selected category and 12 records per page render at a time.
  Gallery/feed previews are at most 640×360, with a bounded 48-entry, ten-minute
  cache. Originals are fetched only for expansion. Existing managed-identity
  reads, account/container validation and Blob size limit are reused unchanged.

## Controlled Event Simulator

Disabled unless all three server-side settings are supplied:

| Setting | Purpose |
| --- | --- |
| `ENABLE_EVENT_SIMULATOR=true` | Explicit operator enablement |
| `SIMULATOR_INGESTION_ORIGIN` | Configured HTTPS ingestion origin, no path/query/credentials |
| `SIMULATOR_OPERATOR_SECRET` | Server-held secret required on each submission |

No settings were enabled during implementation. The form requires the operator
secret and an explicit acknowledgement of possible notifications. It submits
exactly one POST to the existing `/api/v1/simulate/{analytics_type}` endpoint,
with a ten-second timeout, no automatic retry, no redirect following and a
60-second session cooldown. The cooldown is a UI guard, not a distributed rate
limit. The secret gate is not an authenticated account system.

Verified API types: intrusion, PPE, crowd, fire and smoke. The existing service
fixes demo tenant/site to `TENANT-01` / `SITE-01` and purpose to `e2e_test`.
The panel states that these events may trigger real Telegram notifications.
HTTP 200/202 plus `status=accepted` only produces a pending state. Persistence
is confirmed only by a matching event ID and tenant in a subsequent Cosmos
snapshot. Timeout results are ambiguous and never retried automatically.

## Authentication and subscription blocker

The dashboard has no registration, login, verified user identity or account
setup. `infra/dashboard.bicep` explicitly describes public unauthenticated
ingress and cross-tenant reads. Its tenant checkboxes are data filters, not
authorization. This existing access model is unchanged by this UI patch.

The Subscription API exists and enforces `Subscriptions.Read` / `.Write`, a
server-controlled client-to-tenant binding and approved Telegram destinations.
Its auth module explicitly says a shared frontend client needs additional user
authorization to serve multiple tenants. No verified dashboard user/tenant
binding or delegated Subscription API flow exists here. Consequently no
Telegram preference controls, token-paste workaround or subscription writes
were added. Preferences remain future work inside authenticated registration
or account setup, after that integration is supplied. Existing routing rules
and API are unchanged.

## Video boundary

Repository inspection found event producers/simulators and stored evidence, but
no functioning camera capture integration, inference pipeline, streaming endpoint
or mechanism linking video detections to event IDs. Live video analytics remains
a future enhancement. There are no simulated live feeds, bounding boxes, video
players or new streaming resources in this change.

## Validation

`test_dashboard.py` uses Streamlit AppTest and mocked cloud/API calls. It checks
the overview, empty state, same-interaction tenant filters, newest-first feed,
category selection, cached thumbnail bounds, simulator authorization/cooldown,
pending-versus-persisted state, single-POST contract and HTML escaping. Run:

```powershell
python -m pytest business-dashboard/test_dashboard.py -q -p no:cacheprovider
```

No Azure reads, event submissions, notifications, deployment, image/container
build, staging, commits, pushes or merges were performed. Live Cosmos dataset
performance and managed-identity permissions cannot be verified offline; local
records test UI behavior, and repository image files can validate preview cost.
The final implementation report records the exact versions and test results.


### Final verification results

- Dashboard: **16 passed**, using pinned **Streamlit 1.63.0**.
- Ingestion: **71 passed**; subscriptions: **98 passed**; notification evidence:
  **17 passed**; worker: **166 passed**. Total: **368 passed**.
- JWT regressions used the services' pinned **PyJWT 2.15.0**. Initial runs with
  the shared environment's older 2.14.0 failed two JWT hardening tests; those
  pass with the required version. No service source or requirements changed.
- Existing FastAPI/Starlette test tooling emitted an httpx deprecation warning.
  No deprecated Streamlit UI parameters remain in the dashboard.
- Local synthetic evidence benchmark: **43 JPEGs**, **0.073 s** total preview
  conversion, **1,516,922 bytes** original versus **733,376 bytes** in previews
  (about **52% smaller**). This measures local preview conversion only, not
  Cosmos query, Blob network or browser rendering latency.
- The existing Dockerfile now copies the theme configuration. No image was built.
- Streamlit's [fragment documentation](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment)
  describes the controlled interval mechanism used here; offline tests also
  exercised the pinned 1.63.0 runtime.

Remaining review limits: no live persisted dataset was available locally, so
production dataset performance, end-to-end managed-identity Blob reads and
visual appearance in the deployed environment remain unverified. Subscription
preferences are blocked on the authenticated account/tenant integration above.
The simulator remains disabled by default. Live video remains future work.


## October 19 unified implementation update — supersedes earlier limitations

The historical review above is preserved. See `UNIFIED_SOC_READINESS.md` for the
current approved identity mapping, local OIDC/server authorization implementation,
tenant-scoped queries, restored KPI deltas, bounded image caches and revised
five-minute snapshot/refresh policy. Thumbnails still download original Blob
bytes on a cache miss; browser payload savings are not Blob network savings.

Shared backend changes remain proposed, not applied. See
`simulation-authorization.proposed.patch` and `SHARED_BACKEND_REVIEW.md`.
The account setup UI is tested against the proposed self-service contract and
stays disabled until the real ownership/subscription/delivery flow is implemented.
No Azure writes, live event submissions or Telegram notifications were performed.
