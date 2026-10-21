# Consolidated platform observability

Dashboard: https://incident-grafana.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/d/incident-platform-ops/incident-platform-7c-operations

This extends the existing dashboard UID `incident-platform-ops` and datasource UID `azure-monitor`. LML SENTINEL links directly to Grafana; it does not host infrastructure charts. Grafana authentication remains required. The existing Streamlit local login does not establish a Grafana session; no credentials, auth proxy or anonymous access are added.

## Source inventory and supported measurements

Read-only discovery on 21 October 2026 inspected the deployed `incident-resource-health` Workbook (`5d11b703-4adc-5157-b11e-d27c7512dc02`), the `incident-logs` workspace, metric definitions, original Workbook query results, the existing Grafana identity and datasource configuration.

`workbook/queries.json` preserves all nine Workbook queries, including the consumed/provisioned RU query which returned no rows and is intentionally not graphed. The source Workbook and diagnostic settings are not changed.

| Section | Available data and interpretation |
| --- | --- |
| A. Platform Health | Original Resource Graph inventories for six apps, their environment and Cosmos; existing system-log event counts. Inventory state is not an uptime SLO. |
| B. Ingestion and Throughput | Existing incoming/outgoing Service Bus totals and time series, separately by queue. Actual ingestion HTTP Requests split by status category from Azure Monitor, and ResponseTime from AzureMetrics. HTTP metrics include all ingress routes, not just incident submissions. |
| C. Processing Latency | Original Cosmos ServerSideLatency averages, P95 and maximum. **Not end-to-end or worker duration.** P95 is percentile(Maximum,95), a percentile of bucket maxima rather than individual requests. |
| D. Service Bus and Queue Monitoring | Existing ActiveMessages and DeadletteredMessages Average time series and last-reported-depth cards. These are point-in-time depths, not new failures or successful processing counts. |
| E. Notification Performance | Explicit coverage note directs users to the existing notification-events queue series without duplicating them. Notification/Telegram duration and success/failure rates are unavailable. |
| F. Container Scaling and Resource Utilization | Original Workbook replica maxima, CPU/memory summaries, normalized Cosmos RU and storage. The Workbook's CurrentReplicas column is max(Maximum) across seven days, so the panel is correctly titled Peak Replicas. |

Seven-day validation found 168 hourly points per observed status category (10,695 2xx, 37 4xx, 1 3xx; no 5xx series returned) and 10 nonempty AzureMetrics ingestion buckets, actual replica/CPU/memory/Cosmos records, and successful Service Bus queries. AppRequests and AppDependencies had zero records; no console records matched the existing environment filter. This does not prove telemetry is absent forever or in every possible scope. No application duration or failure-rate panel is fabricated from system logs, event timestamps, queue counts or empty tables.

Workbook summary panels explicitly retain their fixed last-seven-days filters and panel override. Other panels respect the dashboard range (default six hours). Times are displayed in Singapore time; underlying timestamps remain UTC. Null observations remain null. Original queue targets, aggregations, dimensions, reductions and missing-value behavior are contract-tested. Redundant queue total bars/share donut are omitted rather than multiplying identical views.

## Cost and access assessment

No Azure resources, diagnostics, ingestion, retention, hosting limits, alerting or permissions are added or changed. Existing Grafana uses 0.5 CPU/1 GiB and scales 0â€“1; its activity can still incur existing hosting charges. Existing workspace pricing is PerGB2018, with 30-day workspace retention. Queried AzureMetrics and Container App log tables use the Analytics plan with 30-day retention. No Basic/Auxiliary table, archive search or restore is used.

Normal Analytics queries within interactive retention have no separate query charge. Platform metric API calls can contribute to the existing subscription allowance/charges, and opening Grafana can keep its container active. Exact incremental cost depends on query count, concurrent viewers, active runtime and subscription usage; zero cost is not guaranteed. Manual dashboard refresh prevents unattended query multiplication. No ACR build/push or deployment is included in this change.

Pricing references: https://azure.microsoft.com/en-us/pricing/details/monitor/ and https://learn.microsoft.com/en-us/azure/azure-monitor/logs/cost-logs . Recheck pricing and subscription usage before rollout.

The existing Grafana identity has Monitoring Reader at resource-group scope and AcrPull at registry scope; resource-context log access is enabled. Azure queries succeeded using the operator CLI credential. **Grafana MSI datasource health, rendered queries and permissions have not yet been verified through an authenticated Grafana session.** Do not grant more access automatically if runtime checks fail.

## Validation and rollout gate

```text
python grafana/validate.py
python -m pytest grafana/tests -q
python -m py_compile business-dashboard/app.py
```

The dedicated CI workflow has contents-read permission and no Azure login or deployment step. Existing CI/CD workflows are unchanged. Do not merge or deploy automatically. This PR is based on current origin/main to exclude eight unmerged teammate commits; it adds the existing Grafana source plus the consolidation, and only a direct-link insertion in main's dashboard. The separate dirty SOC worktree retains its pending UI changes; it must not be replaced with main's older dashboard during a staging release.

Before approval, review the dashboard JSON, unavailable telemetry, query volume and cost assessment. Before rollout, verify authenticated Grafana datasource health and representative panels through MSI. Build/deploy only the existing Grafana image and the approved current SOC dashboard source, after explicit Azure approval. Preserve all backend services, the Workbook, secrets and permissions. Keep Streamlit production `0000032` at 100%, evidence-fix `0000035`, and common sentinel URL unchanged. Retain previous images/revisions for rollback. A query error must not be hidden by zero-fill or an authentication bypass.
