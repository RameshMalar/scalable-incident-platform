# Final demonstration readiness: Smart Incident Monitoring Platform

NUS-ISS demonstration: **24 October 2026**. Verified **19 October 2026**.

All findings were verified with read-only checks only: Azure CLI show/list,
Azure Monitor metrics, Log Analytics queries, HTTP GETs to health endpoints, a
rejected anonymous POST, and the local test suites. Nothing was deployed,
merged, committed, purged, simulated or reconfigured to produce this report.
Identifiers that could locate a person or chat are masked.

**Decision: GO with conditions.** The deployed platform is demonstrable. The
conditions are in §8 (merge freeze) and §9 (minimum steps).

## 1. Feature status

### 1a. Live in Azure (verified 19 October)

| Capability | Evidence |
|---|---|
| 6 Container Apps running | `incident-ingestion`, `incident-worker`, `incident-notification`, `incident-subscription`, `incident-dashboard`, `incident-grafana`: all Running / Succeeded. Ingestion, Subscription API, dashboard and Grafana health endpoints returned HTTP 200 (Worker and Notification have no HTTP endpoint). Load-simulator job present (manual trigger). |
| Ingestion API with Entra auth | `/health` reports `publisher: servicebus`, `auth: entra`; `/ready` is ready. An anonymous `POST /api/v1/incidents` returns **401**. |
| Event pipeline: ingestion → Service Bus → Worker → Cosmos DB | 111,105 Worker publish events logged (20 Sep – 18 Oct). `incident-events` and `notification-events` both have **0 active** messages. |
| KEDA autoscaling | §2 |
| Subscription API (client CRUD, Entra) | `/health` reports `store: cosmos`, `auth: entra`; anonymous `GET /api/v1/subscriptions` returns **401**. |
| Subscription-based routing and Telegram delivery | 12 routed handoffs from the Worker (18 Oct). **66 "Telegram notification delivered"** log events since 1 Oct, latest **18 Oct 09:02 UTC**. |
| Business Dashboard | Serving (revision `incident-dashboard--0000032`). **Public, with no sign-in.** Its image was pushed manually; see §8. |
| Grafana 12.2.0 | Serving; anonymous API access refused (401). Reads Azure Monitor through its managed identity. Deployed manually; see §8. |
| Monitoring | 5 enabled metric alerts: incident queue backlog (sev 1); ingestion and Worker restarts; Cosmos RU approaching cap; Cosmos throttling. Resource-health workbook deployed. |
| CI/CD | CD on merge deployed commit `4b565f9` to every CD-managed service on 18 Oct (deployments `*-4b565f9`, all Succeeded). The Worker was last deployed by the routing activation (`worker-4200336`). |

### 1b. Built and tested locally, not deployed

| Capability | Status |
|---|---|
| Entra sign-in for the dashboard (staged, 0% traffic first) | Prepared and validated with `what-if`. **On hold** until Liu Yiting confirms and approves (`business-dashboard/ACTIVATION_CHECKLIST.md`). |
| Central access: Platform Admin, SOC Operator, SOC Viewer | Implemented in the Subscription API and dashboard; off by default. |
| Telegram self-service: link, verify ownership, opt in, unsubscribe | Implemented; full lifecycle integration test (`tests/local_integration/test_telegram_lifecycle.py`). |
| Owner-eligibility checks in Worker and Notification | Implemented; the Notification recheck is off by default and needs a Cosmos reader role. |
| Simulator authorization | `simulation-authorization.v2.proposed.patch`, **not applied**. |
| Webhook rate limiting; Admin destination review | Implemented and tested. |

Local test evidence: **1,032 passed** (Python 3.12, pinned requirements, no
network), plus 94 for the simulator patch in a scratch copy. The lifecycle and
permission tests were mutation-checked.

### 1c. Deferred, not for 24 October

- Production dashboard sign-in and promotion.
- Central-access activation and team registration.
- Telegram self-service activation (webhook, secret, permissions).
- Applying the simulator patch.
- Notification send-time enforcement.

Each needs a separate approval; the order is in `ACTIVATION_CHECKLIST.md` §7.

### 1d. Known live gaps (present them honestly)

| Gap | Prepared fix |
|---|---|
| The live dashboard is public, with no sign-in | Staged sign-in (1b) |
| The live `POST /api/v1/simulate` is anonymous and can page Telegram | Simulator patch v2 (1b) |
| No alert on dead-lettered messages | §3 limitations |

## 2. Scalability: actual configuration and evidence

### 2a. Scaling configuration (`az containerapp show`, 19 Oct)

| App | Min–max replicas | Scale rule |
|---|---|---|
| `incident-worker` | **1–10** | **KEDA `azure-servicebus`**, queue `incident-events`, `messageCount` 20 |
| `incident-notification` | **1–1** | KEDA `azure-servicebus` on `notification-events` (20), but capped at one replica on purpose (§3) |
| `incident-ingestion` | 0–10 | HTTP, 50 concurrent requests |
| `incident-dashboard` | 0–3 | HTTP, 50 concurrent requests |
| `incident-subscription` | 0–1 | HTTP, 10 concurrent requests |
| `incident-grafana` | 0–1 | HTTP, 10 concurrent requests |

### 2b. Load-test evidence (Azure Monitor)

| Day | Service Bus incoming messages | Max replicas |
|---|---:|---|
| **23 Sep 2026** | **222,004** | Worker **10**, Ingestion **10**, Notification **10** (before the cap) |
| 20 Sep 2026 | 10,113 | Worker 10, Ingestion 4 |
| 4 Oct 2026 | 96 | Worker 3 |

The Worker reached its configured maximum under queue pressure, and both
queues later drained to zero.

## 3. Resilience case study: the 7,704 dead-lettered notifications

| | Finding (Log Analytics, Service Bus) |
|---|---|
| **What** | `notification-events` holds **7,704** dead-lettered messages; the queue has 0 active messages. |
| **When** | Every delivery failure occurred on **23 Sep 2026 13:06–14:26 UTC**, during the 222k-message load test. No failures appear in the 30-day window outside it. |
| **Root cause** | **Load-test events reached the notification path.** 38,916 failures concern `EVT-LOAD` events (10 others `EVT-CD`). Telegram rejected them: **HTTP 400 "Bad Request: chat not found" × 35,503** for the destination configured then, and **HTTP 429 rate limit × 3,310**, plus 13 connection errors. The notifier scaled to 10 replicas and multiplied the pressure. |
| **Why nothing was lost** | Each message was retried up to `maxDeliveryCount` 5 and then moved to the dead-letter queue: about 38,926 attempts ÷ 5 ≈ 7,785, consistent with 7,704. Failed messages were preserved, not dropped, and incident persistence was unaffected. |
| **Corrective actions (24 Sep, PR #36)** | `6a88fa5` "Classify notification traffic explicitly": an `event_purpose` of incident, load_test or e2e_test in the event contract. `191de8d` "Prevent Telegram notification floods": load-test events are excluded in the Worker and the notifier, and Telegram backpressure is honoured. |
| **Configuration since** | Notification capped at 1 replica; `TELEGRAM_MIN_SEND_INTERVAL_SECONDS` 3.1; `TELEGRAM_MAX_RATE_LIMIT_RETRIES` 1. Later: deterministic message IDs and tenant subscription routing (#61). |
| **Outcome** | 103 successful deliveries in the 30 days to 19 Oct (66 since 1 Oct) and no delivery failure after 23 Sep; queues drain to 0. |

**Remaining limitations:**
- **The dead-letter queue has not been cleared.** It must **not** be replayed
  blindly: these are load-test messages. Purging is a separate, approved action.
- **There is no alert on the dead-letter count.** The backlog alert watches the
  incident queue only.
- **One notification replica.** At 3.1 seconds per message, Telegram throughput
  is about 19 messages per minute by design.
- **Delivery is at least once.** There is no outbox and no exactly-once guarantee.
- **The legacy global chat is still configured** for flat messages.

## 4. Screenshots for the final presentation

Before capturing, check which pages the live dashboard actually shows (§8): its
image differs from both committed and local code. **Never capture** tokens,
secrets, chat IDs, the Key Vault secret value, or the Grafana admin password.

| # | Where | Capture | Shows |
|---|---|---|---|
| 1 | Live dashboard → **Overview** | KPI cards and "Incidents per day" | Business view of persisted incidents |
| 2 | Live dashboard → **All incidents** | Filtered table (tenant, analytics, severity, date) | Tenant-partitioned data |
| 3 | Live dashboard → **Evidence** | Gallery and one expanded image | Evidence from Blob Storage through managed identity |
| 4 | Grafana → `/d/incident-platform-ops` ("Platform Observability \| Health & Performance"), **last 30 days** | Full dashboard | Observability |
| 5 | Grafana → *Service Bus \| Incoming Messages* around **23 Sep** | Spike panel | Load test |
| 6 | Grafana → *Service Bus \| Dead-Lettered Messages* and the *Dead-Lettered* stat | The 7,704 | Resilience case study |
| 7 | Grafana → *Container Apps \| System Events by App and Severity* | Panel | Platform events |
| 8 | Azure portal → `incident-worker` → Scale | Rule `queue-depth`, `azure-servicebus`, 1–10 | KEDA configuration |
| 9 | Azure portal → Metrics → Replicas (Max), Worker and ingestion, 19–25 Sep | Chart reaching 10 | Scale-out evidence |
| 10 | Service Bus namespace → Metrics → Incoming Messages, 30 days | 222,004 on 23 Sep | Load evidence |
| 11 | Service Bus → Queues | Active 0; DLQ counts | Queue health |
| 12 | Monitor → Alert rules | The 5 enabled rules | Operational alerting |
| 13 | Log Analytics query (§6) | Counts of delivered and failed notifications | Telegram delivery evidence |
| 14 | GitHub → Actions → CD on merge for `4b565f9`; a reviewed PR | Green run; approval | CI/CD and two-person rule |
| 15 | Terminal → local test summary | 1,032 passed; Telegram lifecycle test | Testing |
| 16 | Terminal → `what-if` of the staged dashboard sign-in | Single additive changes | Security by design (roadmap) |
| 17 | Telegram app → the existing alert chat | A delivered alert from 18 Oct, chat name masked | Real delivery, with no new incident |

## 5. Demonstration sequence (read-only), with fallbacks

Prepare screenshots 1–17 beforehand as the universal fallback. If something is
unavailable, say so and switch to the screenshot; do not redeploy during the demo.

| Step | Show (read-only) | If unavailable |
|---|---|---|
| 1 | Architecture slide (§7) | — |
| 2 | Browser: ingestion `/health` and `/ready`; Subscription API `/health` | Screenshot. Apps that scale to zero may need 30–60 s to wake: open them 5 minutes before. |
| 3 | Terminal: anonymous `POST /api/v1/incidents` → 401 (nothing is written) | Screenshot |
| 4 | Live dashboard: Overview → All incidents → Evidence | Screenshots 1–3. Do not fall back to a local run: the local code now requires Microsoft sign-in, which is not configured yet. |
| 5 | Grafana, 30-day range: load spike, dead-letter panel, Container Apps events | Screenshots 4–7 |
| 6 | Azure portal: Worker scale rule and Replicas metric | Screenshots 8–9 |
| 7 | Resilience story (§3) on slides | — |
| 8 | Telegram: show the alerts delivered on 18 Oct | Screenshot 17 |
| 9 | GitHub Actions and PR review | Screenshot 14 |
| 10 | Roadmap: staged sign-in, central roles, Telegram self-service (lifecycle test), simulator authorization | Screenshots 15–16 |

**No live incident by default.** A single controlled event is possible only
with the platform owner's explicit approval, a known recipient chat, and the
understanding that the anonymous simulator is the known gap in §1d. The
load-simulator job must **not** be run.

## 6. Architecture and DevSecOps evidence checklist

| Area | Evidence | Source |
|---|---|---|
| Event-driven architecture | Ingestion → `incident-events` → Worker → Cosmos (`/tenant_id`) → `notification-events` → Notification → Telegram | Container Apps, Service Bus, `infra/*.bicep` |
| Multi-tenancy | Cosmos partition `/tenant_id`; tenant-bound clients (`TENANT_MAP`); tenant-scoped routing | Cosmos containers; Subscription API |
| Scalability | KEDA Service Bus rule; 222k messages; 10 replicas | §2 |
| Resilience | Retries, dead-letter queue, load-test suppression, backpressure, idempotent persistence | §3 |
| Identity | Entra on APIs (401 anonymous); one user-assigned managed identity per service | Health endpoints; `az identity list` |
| Least privilege | Cosmos roles scoped per container (Subscription API: `subscriptions` contributor, `approved-destinations` reader) | `az cosmosdb sql role assignment list` |
| Secrets | Telegram bot token only in Key Vault (RBAC), as a Container Apps secret reference | Key Vault (names only) |
| Destination control | Operator-approved Telegram destinations, separate from the identity that writes subscriptions | `approved-destinations`; `DESTINATION_POLICY.md` |
| CI | Unit, integration and workflow-security tests; Bicep build; image build and smoke tests | `.github/workflows/ci.yml` |
| DevSecOps | SAST and SCA reports per run, DAST with OWASP ZAP (#73), image scanning; the scanner image and action are pinned by digest/SHA (other actions use version tags) | `ci.yml`, `dast.yml` |
| CD | CD on merge (#76); approved-PR preflight (two-person rule); OIDC federation | `cd.yml`, `cd-on-merge.yml` |
| Observability | Grafana, Log Analytics, workbook, 5 alerts | §1a |
| Testing | 1,032 local tests; Telegram lifecycle integration; mutation checks | §1b |
| Security roadmap | Staged sign-in with lockout guard; least-privilege grants not yet applied; simulator patch | `ACTIVATION_CHECKLIST.md` |

Log Analytics query for screenshot 13 (counts only, no message bodies):

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(30d) and ContainerAppName_s == 'incident-notification'
| extend outcome = case(Log_s has 'Telegram notification delivered', 'delivered',
                        Log_s has 'delivery failed', 'failed (retried)', 'other')
| where outcome != 'other'
| summarize events = count(), latest = max(TimeGenerated) by outcome
```

## 7. Architecture summary for the slide

Producers (Entra app tokens) → **Ingestion API** (Container App, HTTP scaling)
→ **Service Bus `incident-events`** → **Worker** (KEDA 1–10) → **Cosmos DB**
(incidents, subscriptions, approved destinations; `/tenant_id`) → **Service Bus
`notification-events`** → **Notification Service** (1 replica, rate-limited) →
**Telegram**. The **Business Dashboard** and **Grafana** read Cosmos, Blob and
Azure Monitor through managed identities. Secrets live in **Key Vault**; telemetry
goes to **Log Analytics**. GitHub Actions builds, scans and deploys through OIDC.

## 8. Risk: a merge to `main` before the demo

| Item | Finding |
|---|---|
| Live dashboard image | `incident-dashboard@sha256:bd28cd7f…3d16fd` (tag `platform-observability-20261019014825`), **pushed manually** on 18 Oct 17:48 UTC from uncommitted work. CD's own build (revision 30, `7f079cec…`) was superseded. |
| Live Grafana image | `incident-grafana@sha256:6e26149c…f9f8f3` (same tag), pushed manually 17:49 UTC. Its source (`grafana/`, `infra/grafana*.bicep`) is **not tracked in Git**. |
| On the next merge to `main` | CD on merge rebuilds the dashboard **from committed code** and deploys it, **replacing the live dashboard** and any observability changes it contains. |
| Grafana on merge | **Not overwritten**: CD does not reference Grafana, and core deploys incrementally. But it **cannot be rebuilt from the repository** until its source is committed. |
| The prepared guard | `dashboard_release: keep` would stop CD replacing the dashboard, but it is itself uncommitted and protects nothing until merged. |

**Mitigations:**
1. **Freeze merges to `main` until after 24 October** (team agreement).
2. Commit the live Grafana and observability source through a reviewed PR
   **owned by its author**. Deploy nothing new before the demo.
3. Keep the two digests above as the rollback reference. Restoring either is a
   one-line image update that needs approval.

## 9. Minimum safe steps before 24 October

| Priority | Step | Owner |
|---|---|---|
| P0 | Agree a merge freeze on `main` until after the demo | Team |
| P0 | Rehearse §5 read-only; confirm the pages and panels actually live; capture screenshots 1–17 | Presenter |
| P1 | Decide the dead-letter queue: **leave it and present §3 (recommended)**, or purge it with explicit approval | Platform owner |
| P1 | Grafana and observability author opens a PR for the live source (no deployment) | Grafana teammate |
| P2 | Optional: one approved, controlled live event with a known recipient | Platform owner (explicit approval) |
| — | Sign-in test, central access, Telegram self-service, simulator patch: **after the demo** unless Liu Yiting approves early; never promote to production before 24 October | As in `ACTIVATION_CHECKLIST.md` |
