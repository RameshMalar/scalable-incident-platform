# Platform Observability: System Events and single-login assessment

Prepared from Grafana commit `540c422a4e7a775410014b1a58c76ebcf3724e27`. Implementation/local validation only. No Azure settings, provisioning, authentication, traffic, labels, identity or deployed image changed; no push or merge. Product names remain **LML SENTINEL Business Dashboard**, **Platform Observability**, and **Grafana**.

## System Events root cause

Panel 5 queried `ContainerAppSystemLogs_CL` in workspace `incident-logs` (`c424063b-39b1-4ffc-bdf6-1d8e9b55c932`), using the dashboard-selected interval (default six hours) and `EnvironmentName_s == 'incident-env'`.

Read-only inspection confirms the ARM environment resource is named `incident-env`, but exported `EnvironmentName_s` is **`calmmushroom-4789bfc0`**. The original filter discarded every matching log record. This is a query identifier mismatch, not a permission, workspace, absent-export or time-window failure. The environment's existing destination is log-analytics with the expected customer ID. The legacy system table contains real records; the resource-specific table exists but its seven-day query was empty. No additional diagnostic settings are required for this correction.

The original six-hour query succeeded with zero rows; the corrected six-hour query returned **396 events in 55 buckets** during this validation. An independent count matched. Sources included all six apps, Normal/Warning event classifications and ContainerAppController/KEDA/Scaling event sources. These are log-record counts, not necessarily unique platform events, failures, replicas or uptime. No raw Log_s messages are displayed or logged by this work.

## Minimal targeted implementation

Only panel 5 changes. Its ID/title, workspace/data-source identity, app filter, time bucketing and selected-range counting intent remain. It is now a table so timestamps, source, classification, counts and explicit delivery states can be read reliably; the other 34 panels are unchanged. No extra panels or infrastructure.

Canonical KQL: `workbook/system-events.kql`; the JSON target must match it. Observed rows contain bucket time, app Source, Severity (original Type_s classification), EventSource, Events, LastEventAt and Status.

- Real selected-range rows: actual counts and actual most-recent timestamp per bucket/source/classification.
- Empty selected range with a system record witnessed within one hour: a clearly labelled zero-event summary row. This is not a fabricated event or a zero-filled time series.
- Empty range without a recent witness: `Telemetry unavailable`, Events=null, with last observed system timestamp if available. It means delivery cannot be verified from recent system records; quiet healthy environments can also lack a witness. It is not proof the exporter or platform failed. Exact dependency is a matching recent record in the existing system-log export, not a new agent/heartbeat.
- Missing table, schema, permission or query failures remain native datasource errors. They are not silently converted to zero or an empty table.

The independent seven-day delivery-evidence lookback and one-hour freshness guard are new presentation policies for panel 5, not changes to Yixin's time windows. They never add events to selected-range totals. The target explicitly uses `dashboardTime=false`, allowing the KQL's selected-range macros and independent evidence interval to apply separately. Grafana 12.2 source confirms a request-wide timespan is attached only when dashboardTime is true. Read-only testing via the deployed plugin verified this behavior for an empty selected future range. No guarantee of continuous export can be inferred solely from a recent record.

Panel type change is intentional: no claim of a line-chart rendering test. Its bucketed counts retain the original monitoring intent. `queue-contract.json` preserves the original panel snapshot for provenance; validator exempts only the authorized panel 5 correction and still checks its original title/source/workspace.

## Local and read-only validation

- 18 Grafana configuration/navigation/mutation tests passed.
- Original query: success, zero rows. Corrected query: 396 events/55 buckets; independent count equal.
- Empty-range case with real delivery evidence: zero count and explicit status.
- Test-only absent-environment case: unknown/null count with Telemetry unavailable.
- Test-only invalid table: error propagated, no zero result. These ad hoc reads neither create events nor alter provisioned Grafana queries.
- Business Dashboard tests run against unchanged header-fix source `ce69ee09753fc112b1b91214009d2822b772f6b9`, Streamlit 1.63.0: 184 passed, 2 skipped. Tests cover existing login/roles/expiry/revocation/logout, Overview, filters/evidence/navigation/subscriptions; no Business Dashboard file changed.
- Workbook catalogue/provisioning and all other dashboard panels are unchanged. All 35 IDs including original 1–13 remain.
- Local image provisioning/authentication results and exact image digest are recorded in the external final preparation report after build.

## Single-login: STOP before implementation

The current Business Dashboard uses `DASHBOARD_AUTH_MODE=local`. Its username/scrypt verification creates a Streamlit server-state session; idle expiry is 30 minutes, absolute expiry 8 hours, and account/role is reread from the mounted file. Sign Out removes only that Streamlit session. It produces no verified bearer assertion, cross-origin session or common logout channel that Grafana can accept.

Grafana has separate Basic/form authentication, seven-day idle/thirty-day maximum sessions, anonymous/proxy/OAuth disabled, and a resource-group-wide Azure Monitor MSI. Existing Business Dashboard code contains dormant Entra/OIDC verification capability, but that is not the active deployed authentication mode. A shared identity provider by itself would not enforce Business Dashboard authorization, immediate sign-out/revocation, or the active Business Dashboard session requirement. A URL, client header or Streamlit cookie is not an authentication bridge.

Grafana Viewer prevents configuration writes; it does **not** enforce data-source row/tenant isolation. Viewers can issue other queries against accessible data sources. The current 35-panel view contains platform-wide apps/queues/Cosmos/workspace information. Merely hiding panels, disabling Explore, filtering a dashboard variable, or mapping every Business Dashboard viewer to Grafana Viewer would not meet tenant isolation.

Therefore **no single-login code, cookie sharing, credential handoff, public anonymous access or auth-proxy setting is implemented**. The requested authentication redesign/ingress/identity changes require prior explicit approval.

## Smallest viable strict architecture for approval

1. Introduce one shared server-side authentication/session boundary, deployed with the existing two Container Apps where practical (front-proxy processes/sidecars rather than a new always-on app). Preserve the Business Dashboard branding and visible login experience, but replace/bridge the current local session authority only after review. This is an authentication architecture change, not a link edit.
2. Give both entry points a common authoritative session/revocation registry and current authorization check. Evaluate reuse of existing Cosmos hosting; a session collection/TTL/write permissions and any secret needed are **not currently authorized or provisioned**. Do not store passwords or browser tokens in reports. Scope registry access to required records and fail closed on lookup failure.
3. Put Grafana backend behind its front proxy, listening on a private loopback/internal endpoint. Every HTTP/API/websocket path must traverse verified identity and active-session authorization. Strip all client-supplied identity headers; inject identity only over a protected proxy-to-backend connection. Disable Grafana login-token bypass in the proposed proxy design; no long-lived independent Grafana cookie may outlive the common session. Keep existing Basic auth unchanged until a separately approved gated migration is tested.
4. Use opaque, Secure, HttpOnly, host-scoped sessions with CSRF protection. For separate origins use a reviewed, state-bound, short-lived single-use authorization-code exchange over HTTPS/backchannel, not shared parent-domain cookies or credentials in URLs. Bind to the initiating browser/session; consume once; keep protocol artifacts out of logs/referrers. No Grafana admin credentials/service tokens are sent to the browser.
5. Default authorized team users to Grafana Viewer irrespective of Business Dashboard admin status. Initially permit this platform-wide dashboard only to explicitly authorized platform operators with access to the full monitored scope. Tenant-scoped users remain denied until server-enforced tenant data access is designed and approved; the existing broad MSI cannot safely be exposed to them. Any new observability grant/policy mapping requires approval.
6. Check active authorization/session on every protected request, cap idle/absolute lifetimes to the Business Dashboard policy, revoke the common session on logout/account removal/downgrade, and close long-lived websocket access on expiry/revocation. Revocation timing and websocket enforcement must be specified and tested; neither Grafana's native long cookie nor Entra login alone proves it.
7. Keep both existing URLs pointing to their authenticated front doors and preserve the original dashboard UID. Backend ingress/network/port changes, proxy trusted-peer policy, identity/session-store permissions and any root_url/subpath behavior require separate reviewed deployment configuration. No insecure direct-backend fallback.

Shared Entra ID using the dormant Business Dashboard code and Grafana OAuth is a simpler alternative for reducing repeated login prompts, but **does not alone satisfy the strict same-session/logout/tenant requirements**. It would still need an approved authoritative gate and scope policy, and changes the existing local login. Do not claim that configuration alone completes single-login.

### Required approvals before authentication implementation

- Approve replacing/bridging the local session authority and preserving/migrating current accounts or moving to a shared IdP.
- Approve the exact proxy/sidecar, port/ingress and direct-access restrictions while retaining public entry URLs.
- Approve session-registry placement, TTL, secret management, narrowly scoped permissions and measured cost impact.
- Approve platform-wide observability audience; tenant-only users must remain denied unless a separate scoped data boundary is implemented.
- Approve lifecycle/CSRF/replay/headers/websocket test requirements and phased rollback before touching live auth.

These choices cannot be inferred from existing approval for the Grafana dashboard image. No authentication Azure command is proposed as ready to execute.

## Authentication test status

Existing Business Dashboard session expiry, revocation/roles and logout: covered by passing unchanged regression suite. Existing Grafana unauthenticated denial and native login/logout/Viewer behavior may be checked in the temporary local container; those do not establish shared-session security. Business Dashboard → Grafana single-login, cross-app logout/revocation, tenant-safe query enforcement and forged-header/direct-backend rejection across an integrated gateway are **NOT IMPLEMENTED / NOT VERIFIED**. No end-to-end completion claim.

## Deployment boundary and rollback

Only the System Events Grafana image can become a local deployment candidate. Following separate approval: push its exact verified digest; copy a new 0%-traffic Grafana revision from `incident-grafana--workbook-540c422a`; retain old staging revision and production `incident-grafana--0000003` pinned at 100%; validate new panel/data/errors and authenticated browser rendering. Do not change mode again (already Multiple), authentication, labels, ingress, identities or other services. Do not promote without additional approval.

Preserve old images/revisions; abandon the new candidate by deactivating it while leaving production weights unchanged. No Single-mode reset while an unapproved candidate is latest. SQLite remains ephemeral and configuration rollback is not a runtime session backup. No Business Dashboard image/revision update is part of this System Events plan.

No new diagnostic ingestion or retention. Panel 5's bounded evidence lookback adds existing-table read work, not new monitoring infrastructure. An approved extra active Grafana revision would retain current 0.5 vCPU/1 GiB min0/max1 sizing, with prior estimate ~USD 0.065/active hour before allowances and ancillary charges. Single-login proxy/session-store compute, Cosmos operations, secret/network requirements are not yet sized; no zero-cost promise or approval implied.

References: [Container Apps logs](https://learn.microsoft.com/en-us/azure/container-apps/log-monitoring), [Grafana 12.2 query timespan code](https://github.com/grafana/grafana/blob/v12.2.0/pkg/tsdb/azuremonitor/loganalytics/azure-log-analytics-datasource.go), [Grafana data-source security](https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/), [auth proxy](https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/auth-proxy/).
