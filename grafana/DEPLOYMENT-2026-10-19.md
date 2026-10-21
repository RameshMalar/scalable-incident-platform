# Grafana deployment — 2026-10-19

Approved scope: existing Grafana image only. Subscription `Azure for Students` (`a3bb9202-635d-4ab5-aaa4-5458f296b64e`), resource group `rg-incident-platform`, app `incident-grafana`.

- Previous/rollback image: `incidentacr4pqaac2c4e4hs.azurecr.io/incident-grafana:demo-v1`
- Previous ready revision: `incident-grafana--f0dhs2u`
- Deployed image: `incidentacr4pqaac2c4e4hs.azurecr.io/incident-grafana:ops-20261019000127`
- Pushed digest: `sha256:131f770d1365bdf6745579b17841ffd8b23e9b96fc76044e9422f381a9c42267`
- New ready revision: `incident-grafana--0000001`; Healthy / Provisioned; app Running / Succeeded.
- Health endpoint: HTTP 200, database `ok`, Grafana 12.2.0.
- Old/new revision templates compare equal after excluding image and revision suffix.

The README ACR remote build failed before deployment: listBuildSourceUploadUrl is unsupported in malaysiawest. Used the existing local Docker engine with the same grafana/Dockerfile, pushed to the existing ACR, then executed the documented image-only containerapp update. No resources or infrastructure settings were created or changed. No rollback was needed.

Dashboard: https://incident-grafana.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/d/incident-platform-ops

Dashboard API returned HTTP 200 with all six panels and targets matching the local dashboard. Authenticated datasource query checks used Grafana managed identity. Raw legacy Service Bus targets initially returned MissingSubscription when sent directly to the backend; applying Grafana 12.2.0 migrateResourceUriToResourceObj (as normal browser queries do) resolved this. This was a verification-client issue, not a datasource permission failure. No dashboard changes were needed.

| Panel | Last 6 hours | Result |
| --- | --- | --- |
| Incoming Messages | Both queues: all 72 returned samples zero | HTTP 200, valid zero activity |
| Outgoing Messages | Both queues: all 72 returned samples zero | HTTP 200, valid zero activity |
| Active Messages | Both queues: all 72 returned samples zero | HTTP 200, valid zero depth |
| Dead-Lettered Messages | incident-events: zero; notification-events: flat 7,704 (72 samples each) | HTTP 200, valid retained depth |
| Container Apps System Events | No frames over 6h or 24h | HTTP 200, no data; no permission/query error |

No system-event data does not establish health or uptime. Dashboard/API and query results were verified; visual browser rendering was not inspected. No synthetic load, production-data mutation, queue purge, other-service update, GitHub push or workflow change occurred. Existing unrelated worktree edits remain untouched.

Rollback reference (not executed; use only if needed and authorized):

```powershell
az containerapp update --subscription a3bb9202-635d-4ab5-aaa4-5458f296b64e -g rg-incident-platform -n incident-grafana --container-name grafana --image incidentacr4pqaac2c4e4hs.azurecr.io/incident-grafana:demo-v1 --output none
```
