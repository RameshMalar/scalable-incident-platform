# Operations dashboard

## Scope and verified configuration

Only Grafana-owned files are changed. Existing MSI datasource `azure-monitor`, dashboard UID, active/dead-lettered panel IDs, namespace resource URI, queue filters and Average aggregation are preserved. Provisioning and Bicep need no changes.

Resource: `/subscriptions/a3bb9202-635d-4ab5-aaa4-5458f296b64e/resourceGroups/rg-incident-platform/providers/Microsoft.ServiceBus/namespaces/incident-bus-4pqaac2c4e4hs`.

| Metric API name | Namespace | Aggregation used | Dimension | Unit |
| --- | --- | --- | --- | --- |
| IncomingMessages | Microsoft.ServiceBus/namespaces | Total | EntityName | Count per bucket |
| OutgoingMessages | Microsoft.ServiceBus/namespaces | Total | EntityName | Count per bucket |
| ActiveMessages | Microsoft.ServiceBus/namespaces | Average | EntityName | Queue depth |
| DeadletteredMessages | Microsoft.ServiceBus/namespaces | Average | EntityName | Queue depth |

Both queues remain `incident-events` and `notification-events`. Azure metric definitions were checked read-only on 2026-10-18 against this exact namespace; all four metrics, EntityName and PT1M minimum grain exist. Auto grain scales with the time range. Total is the API aggregation name for Sum.

References: [Microsoft supported metrics](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-metrics/microsoft-servicebus-namespaces-metrics), [Service Bus monitoring semantics](https://learn.microsoft.com/en-us/azure/service-bus-messaging/monitor-service-bus-reference).

Six-hour default range and five-minute refresh reduce repeated queries. Flat zero depth can indicate idle queues or fast consumers between samples; stable nonzero DLQ depth can be retained messages. Neither measures traffic. Missing data is left missing, never replaced with zero. Traffic counts are totals per bucket, not rates.

## Container Apps visibility and limitations

Existing workspace `incident-logs` resolves to the datasource workspace ID `c424063b-39b1-4ffc-bdf6-1d8e9b55c932`. Grafana identity `incident-grafana-id` has Monitoring Reader at resource-group scope and AcrPull at registry scope. The workspace contains ContainerAppSystemLogs_CL and ContainerAppConsoleLogs_CL. System-table columns ContainerAppName_s, EnvironmentName_s and Type_s were confirmed read-only.

The system-event panel counts records by app and severity in `incident-env`; it excludes raw log text to avoid exposing application payloads or secrets. Event volume is not failure count, readiness, replica health, or uptime. No console-log content panel, new diagnostics, resources, permission grants, or alert rules are added. A Grafana managed-identity query has not been executed locally: the operator CLI credential does not prove MSI access. If it fails after deployment, inspect the query error and workspace access; do not interpret it as zero or grant broader permissions automatically.

## Local validation

Run from this worktree:

```powershell
python grafana/validate.py
az bicep build --file infra/grafana.bicep --stdout > $env:TEMP/grafana-identity-validation.json
az bicep build --file infra/grafana-app.bicep --stdout > $env:TEMP/grafana-app-validation.json
git diff --check -- grafana infra/grafana.bicep infra/grafana-app.bicep
```

The validator requires existing PyYAML. Bicep commands compile only; they do not validate live Azure deployments. Generated infra/grafana*.json files are left untouched. Files were already untracked, so ordinary git diff does not show them until staged; inspect their full contents as part of review.

## Validation results (2026-10-18)

- Dashboard/provisioning validator: PASS.
- Both Grafana Bicep files compile successfully with installed Bicep 0.47.16; only upgrade notices. No source or generated infrastructure files changed.
- Whitespace checks passed, including explicit no-index checks for the three untracked changed files.
- Query fields verified against Grafana v12.2.0 AzureLogsQuery and AzureMetricQuery source definitions.
- Read-only workspace query using the panel table/environment/app filter succeeded with zero records over six hours under the operator credential. MSI runtime access and rendered panel behavior remain unverified.
- No Docker build, Azure deployment, push, merge, queue mutation, synthetic incident, or workflow change performed.

## Deployment commands — approval required

No deployment has been performed. After explicit approval, run from this worktree in PowerShell. This updates only the existing Grafana app and builds an image in the existing registry; ACR build has a small build/storage cost. Existing app configuration, secrets, identity and scale settings are retained. No Bicep deployment is required for this dashboard-only change.

```powershell
$subscription = 'a3bb9202-635d-4ab5-aaa4-5458f296b64e'
$resourceGroup = 'rg-incident-platform'
$previousImage = az containerapp show --subscription $subscription -g $resourceGroup -n incident-grafana --query properties.template.containers[0].image -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Cannot read current Grafana image' }
$tag = 'ops-' + (Get-Date -Format 'yyyyMMddHHmmss')
$image = "incidentacr4pqaac2c4e4hs.azurecr.io/incident-grafana:$tag"
az acr build --subscription $subscription --registry incidentacr4pqaac2c4e4hs --image "incident-grafana:$tag" --file grafana/Dockerfile grafana
if ($LASTEXITCODE -ne 0) { throw 'Image build failed; stop' }
az containerapp update --subscription $subscription -g $resourceGroup -n incident-grafana --container-name grafana --image $image --output none
if ($LASTEXITCODE -ne 0) { throw 'Grafana update failed' }
```

Open Grafana and verify all six panels, existing two queue series, legends, gaps, traffic buckets and system-event query through MSI. Expand to 24h for quiet workloads. Do not generate incidents or purge queues. If the log query errors, capture only a redacted error. If rollback is required and approved, reuse the saved prior image:

```powershell
az containerapp update --subscription $subscription -g $resourceGroup -n incident-grafana --container-name grafana --image $previousImage --output none
```
