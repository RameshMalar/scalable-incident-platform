# Grafana access and preservation preparation

Based on `156e18a2e7aca80fbac8731edd47fd6947bb779b`, in isolated branch `fix/grafana-workbook-panel-preservation`. No Streamlit/SOC, backend, infrastructure or authentication configuration changes.

## Authentication findings

Read-only inspection found Grafana revision `incident-grafana--0000003` using a secret-referenced admin password, anonymous access disabled, sign-up disabled, and no enabled proxy, Microsoft Entra ID or Generic OAuth authentication. Unauthenticated dashboard navigation redirects to login; the unauthenticated dashboard API returns 401. Authenticated API reads succeed. Effective sessions expire after seven inactive days or thirty days maximum, with ten-minute token rotation. Earlier source `6a3c2085` already disabled anonymous access. This local Workbook candidate has never been deployed and cannot have changed current authentication.

Streamlit local login and Grafana login are independent. The earlier no-prompt experience is consistent with an existing Grafana browser cookie. A different browser/profile, cleared/expired cookies, or loss of server-side sessions can require login. Without that browser's history/session evidence, the exact trigger is not established. Grafana uses ephemeral SQLite with no persistent volume; restarts/scale-to-zero can also lose runtime state. Do not claim a proven session-expiry incident or fix this by removing authentication.

Keep authenticated access for the demonstration. Prefer individually assigned Viewer accounts, with credentials distributed privately, not shared administrator credentials. Creating viewers is a separate approved action and their runtime records require a persistence/recreation plan. Longer term, tenant- and team-restricted Microsoft Entra ID OAuth with Viewer mapping offers convenient authenticated access; app registration, assignments, secrets, role mapping and any persistent storage require separate review. SOC local login alone does not provide SSO. No account, OAuth, cookie or proxy setting is changed here.

The effective `cookie_secure=false` setting is recorded as an existing hardening opportunity behind HTTPS; proposing `GF_SECURITY_COOKIE_SECURE=true` is separate from this image-only update. Do not enable arbitrary trusted headers or public anonymous Viewer access. Current Grafana Viewer permissions are read-only for changes but do not inherently restrict users to just the displayed metric queries.

References: [Grafana authentication](https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/), [Entra ID](https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/entraid/), [cookie hardening](https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-security-hardening/).

## Restored panels

Panels 11 Incoming Observed Messages by Queue, 12 Outgoing Observed Messages by Queue, and 13 Incoming Share of Observed Traffic were read from the deployed dashboard and confirmed exactly equal to source commit `6a3c2085`. The original guide panel 6 is also restored so all original panel IDs 1–13 remain. Only grid positions differ. Queue bar/donut panels appear below incoming/outgoing stats; later rows are shifted to avoid overlap. No panel ID collision or new data source.

The extended `workbook/queue-contract.json` is an offline preservation fixture, not a Workbook query/export change. Tests reject removal, title drift, transformation drift and time-range changes in restored panels. Their selected-range totals, Total aggregation, null filtering, aliases, units, visualization options and sources are unchanged. New explanatory text distinguishes these alternate traffic views from unique events. The original guide is preserved verbatim; its historical five-minute refresh references do not override the consolidated dashboard's actual manual refresh setting.

UID `incident-platform-ops`, default six-hour range, fixed seven-day Workbook queries, datasource UID `azure-monitor`, provisioning YAML and Dockerfile remain unchanged. All nine original Workbook queries, CPU/memory/latency/normalized-RU percentile populations and rounding are unchanged. The existing coverage audit remains the historical audit of `156e18a2`, not the current panel count. Current inventory has 35 panels: 25 query panels, six section rows and four explanatory text panels.

## Metric distinctions

- Current replicas: panel 33 latest exported observation, timestamp and 15-minute guard; unknown if missing/stale. Panel 28 remains the separate seven-day peak. An exported Maximum bucket is not an instantaneous replica census.
- Consumed RU: panel 34 retains the empty original exported summary, while 35 queries actual TotalRequestUnits using hourly Total buckets. It does not reproduce the original mean/peak and is not normalized RU percentage.
- Latency: panel 25 is Cosmos server-side latency in ms, with the original percentile of Maximum samples. It is not end-to-end incident or worker latency. Legacy ServerSideLatency is retained for fidelity rather than silently migrated.
- CPU/memory, normalized RU and storage preserve the original seven-day aggregations. Storage is peak GiB over that period, not current usage.

## Deployment boundary

Local build and read-only queries only. No push, revision creation, labels, routing, secrets, roles, database migration, authentication settings or Azure Workbook changes are authorized by this preparation task. Existing Grafana is Single mode, current healthy revision `incident-grafana--0000003`. Isolated staging needs explicit approval for Multiple mode and pinning old revision at 100% before candidate creation. Keep old revision active and copy its environment, identity, secret references, volumes and resources. Do not promote after mere health readiness; require authenticated provisioning, permission and live-panel validation.

Ephemeral database means new revision has independent sessions/runtime settings. Inventory dashboards, users and ACLs before creation; if runtime-only customizations exist, stop to prepare an approved migration. Existing admin secret and provisioned configuration can recreate the currently inventoried single-admin/provisioned-dashboard setup. Do not export credentials or database files in logs or reports.

No new infrastructure/retention/diagnostics/permissions. Manual refresh limits background query multiplication; restoring three chart panels adds six metric targets per full refresh before any plugin caching. Active extra replica compute, registry storage and existing metric/log meters may incur incremental costs. Keep old image/revision for rollback. Separate explicit approval is required before any Azure-changing operation; production promotion requires additional approval after live validation.
