# Yixin's Workbook coverage audit — 22 October 2026

Baseline: `47eb721af870b51a7d33496cb306db9d2e9ae766`. Corrections are isolated on `integration/yixin-workbook-coverage`; no SOC application file is changed. All nine saved queries in `workbook/queries.json` were compared to the deployed Workbook and match exactly. The original Workbook is not modified.

Sources: **ARG** = Azure Resource Graph through the existing `azure-monitor` datasource. **LA** = `AzureMetrics` in the existing `incident-logs` workspace, using that datasource. **Metrics API** = existing Azure Monitor metric endpoint through the same datasource. Workbook summaries retain fixed `ago(7d)` filters and seven-day panel overrides. ARG inventory is the current ARM resource snapshot, not a seven-day uptime calculation.

## Metric-by-metric matrix

Status describes configuration coverage; runtime availability is stated separately. Panel numbers are stable Grafana IDs. “Present” does not assert a live Grafana MSI query has succeeded.

| Workbook metric | Azure data source | Grafana panel | Query / aggregation | Baseline status → updated status | Required correction / interpretation |
| --- | --- | --- | --- | --- | --- |
| Running status: incident-dashboard | ARG `Resources`, Microsoft.App/containerApps | 20 — Current Resource Inventory | `tostring(properties.runningStatus)`; RG filter | Present → Present | None; preserve original query. Running is not an uptime SLO. |
| Running status: incident-grafana | Same ARG query | 20 | Same | Present → Present | None |
| Running status: incident-ingestion | Same ARG query | 20 | Same | Present → Present | None |
| Running status: incident-notification | Same ARG query | 20 | Same | Present → Present | None |
| Running status: incident-subscription | Same ARG query | 20 | Same | Present → Present | None |
| Running status: incident-worker | Same ARG query | 20 | Same | Present → Present | None |
| Current replicas: incident-ingestion | LA `Replicas.Maximum` | 28 — Peak Replicas; **33 — Latest Reported Replicas** | Original: `max(Maximum)` over 7d. Added: latest sample via `arg_max(TimeGenerated, Maximum)` | Missing as current → Partially implemented | Retain original peak; add separate timestamped latest observation. Current count is null when older than 15m or absent; not an instantaneous control-plane census. |
| Current replicas: incident-worker | Same | 28 and 33 | Same | Missing as current → Partially implemented | Same freshness limitation |
| Current replicas: incident-dashboard | Same | 28 and 33 | Same | Missing as current → Partially implemented | Same freshness limitation |
| CPU Average: ingestion, worker, dashboard | LA `CpuPercentage` | 29 — CPU / Memory, 7d | `avg(Average)`, round to 1 decimal, grouped by Resource/MetricName | Present → Present | Preserve original calculation; not a time-weighted/request-weighted average |
| CPU Maximum: all three apps | Same | 29 | `max(Maximum)`, round 1 | Present → Present | Preserve |
| CPU P95: all three apps | Same | 29 | `percentile(Maximum,95)`, round 1 | Present → Present | Percentile of metric maxima, not raw CPU samples |
| Memory Average: all three apps | LA `MemoryPercentage` | 29 | `avg(Average)`, round 1 | Present → Present | Preserve |
| Memory Maximum: all three apps | Same | 29 | `max(Maximum)`, round 1 | Present → Present | Preserve |
| Memory P95: all three apps | Same | 29 | `percentile(Maximum,95)`, round 1 | Present → Present | Preserve original percentile population |
| Cosmos provisioning state | ARG `Resources`, Microsoft.DocumentDB/databaseaccounts | 22 — Resource Inventory | `tostring(properties.provisioningState)` | Present → Present | None |
| Cosmos throughput limit | Same ARG query | 22 | `tostring(properties.capacity.totalThroughputLimit)` | Present → Present | Account throughput limit, not consumed RU, utilization or verified provisioned RU/s |
| Cosmos consumed RU — original Workbook mean/peak and provisioned summary | LA `TotalRequestUnits` / `ProvisionedThroughput` | **34 — Consumed RU, Workbook Summary** | Original `avg(iff(...,Total,0))`, `max(iff(...,Total,0))`, `avg(iff(...,Average,0))`; round 1; 7d | Missing panel → Partially implemented | Restore original query verbatim. Audit returns zero rows; display No data, not zero. Original mixed-metric zero terms are preserved, not silently changed. |
| Cosmos actual consumed RU — additional source coverage | Metrics API `TotalRequestUnits` | **35 — Actual Consumed RU per Hour** | `Total`, `PT1H` buckets, fixed 7d | Missing → Present | Existing API returns real samples. This is RU per bucket, not the original exported-summary mean/peak, RU/s, normalized RU or capacity. |
| Cosmos latency Average | LA `ServerSideLatency` | 25 — Server-Side Latency, 7d | `avg(Average)`, round 2 | Present → Present | Database-side latency only; not end-to-end incident or worker latency |
| Cosmos latency P95 | Same | 25 | `percentile(Maximum,95)`, round 2 | Present → Present | Preserve bucket-maximum percentile; not request-level P95 |
| Cosmos latency Maximum | Same | 25 | `max(Maximum)`, round 2 | Present → Present | Preserve; milliseconds |
| Cosmos normalized RU Average | LA `NormalizedRUConsumption` | 30 — RU Utilization, 7d | `avg(Average)`, round 2 | Present → Present | Percentage, not request units |
| Cosmos normalized RU Maximum | Same | 30 | `max(Maximum)`, round 2 | Present → Present | Preserve; percentage |
| Cosmos normalized RU P95 | Same | 30 | `percentile(Maximum,95)`, round 2 | Present → Present | Preserve; percentile of bucket maxima |
| Cosmos data usage / document quota | LA `DataUsage`, `DocumentQuota` | 31 — Storage, 7d | `max(Maximum)/1024/1024/1024`, round 3, grouped by Resource/MetricName | Present → Present | Seven-day peak GiB, not latest instantaneous usage. Tiny values can legitimately round to zero; missing data is not zero. |

## Minimal corrections

1. Panel 28 retains Yixin's original query but displays `CurrentReplicas` as **Peak replicas (7 days)**. The query's original misleading column name is not rewritten.
2. Panel 33 adds a separate latest-exported sample for the three requested apps, timestamp and freshness. It always shows those app identifiers; missing samples stay null. A 15-minute freshness guard is an explicit presentation policy for this new panel, not a modification to Yixin's original seven-day calculation.
3. Panel 34 restores the original consumed/provisioned-RU summary. No new diagnostic export is enabled to fill its empty result.
4. Panel 35 reads actual consumed RUs from the existing Metrics API because they are not present in the original exported-summary result. It has a deliberately distinct hourly aggregation and title, avoiding any claim that it reproduces the Workbook mean/peak.

All other original Workbook queries, rounding, percentile expressions and time windows are unchanged. Existing queue panels, datasource identity/authentication, manual refresh, default six-hour range for other panels, and the completed SOC are unchanged. Layout is extended without overlap.

## Runtime evidence and remaining gaps

Read-only Azure validation on this audit run:

- Original replica query: 3 rows, peaks dashboard/ingestion/worker = **2/10/10**.
- Latest-reported query: 3 rows, dashboard/ingestion/worker = **1/0/1**, timestamp **2026-10-21 16:26 UTC / 22 October 00:26 SGT**. These were recent at query execution, not permanently current values.
- CPU/memory: 6 rows (three apps × two metrics); latency: 1 row; normalized RU: 1 row; storage: 2 rows. No KQL errors.
- Original consumed/provisioned-RU summary: **0 rows**. It remains an honest telemetry gap.
- Direct `TotalRequestUnits` API: **168 populated hourly buckets**, no API error.
- Grafana managed-identity access/rendered panels still require authenticated live verification. Operator queries prove source/query validity, not Grafana identity permissions. No Azure deployment is performed.

## Cost and deployment readiness

Validation results: **193 passed, 2 skipped** across the complete SOC dashboard and observability suites, including **10 Grafana configuration/navigation/mutation tests**. The two skips require the optional common-event-model package. Offline provisioning/query-fidelity and whitespace checks passed. A local authenticated **Grafana 12.2.0** container became healthy and provisioned **31 panels** (including section rows and notes) with the unchanged `azure-monitor` datasource definition. The smoke helper waits for provisioning after HTTP health readiness; its initial early dashboard lookup returned 404, and the corrected wait passed. This is not a live MSI connectivity test.

Git comparison confirms **no changes** to `business-dashboard/`, infrastructure, workflows, the query catalogue, or datasource provisioning relative to `47eb721a`. Only the Grafana dashboard definition, validators/tests and coverage documentation differ. Existing branches/worktrees are not overwritten.

No new resources, diagnostics, ingestion, retention, credentials or permission grants. Two added Log Analytics queries use the existing Analytics table, and one added metric API query uses the existing Cosmos metric endpoint. Manual refresh is retained. Existing API allowances/charges and active Grafana hosting still apply; no zero-cost guarantee is made.

Local validation must pass before review. Publishing these panels requires an approved update to the **existing Grafana image/app**, not a Streamlit-only deployment. The Streamlit image and its previously verified staging candidate are unchanged. Do not push, merge or deploy this branch without review and explicit approval. Keep the existing Grafana image/revision for rollback, and preserve the Workbook and all backend resources.
