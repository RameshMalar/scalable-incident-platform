# Phase 3B-1 — Subscription API infrastructure review

## Current implementation and historical scope

This branch now includes the Subscription API, subscription-based Worker routing,
compatible Notification envelopes, infrastructure and CI/CD integration. Automatic
PR CI compiles Bicep without Azure authentication. Azure preview is manual-only
from main with protected-environment approval; production rollout is manual-only
and prepares Notification, storage and the API before optional Worker activation.
See [SUBSCRIPTION_ROLLOUT.md](../.github/workflows/SUBSCRIPTION_ROLLOUT.md) and
[AZURE_PREVIEW.md](../.github/workflows/AZURE_PREVIEW.md) for current workflow gates.

The Phase 3B-1 findings, commands and results below record the earlier
infrastructure-only stage. Statements that workflows were unchanged describe
that stage, not the current branch. Live Azure validation remains a prerequisite.


Prepared locally on 2026-10-09. **PHASE 3B-1 READY FOR REVIEW**.
This is infrastructure preparation, not Azure deployment readiness or cloud
validation. No Azure CLI, login, cloud query, deployment, what-if, credentials,
workflow execution, Git staging, commit, push, merge or PR creation was performed.

## Existing infrastructure findings

- Templates are resource-group scoped. The resource group is supplied through
  `AZURE_RESOURCE_GROUP` in CD; scripts/README use `rg-incident-platform` as a
  convention, not proof of the live configured value. No resource group is created.
- Resource location defaults to `malaysiawest`. Service templates must receive
  the same location/prefix as core. `namePrefix` defaults to `incident` and core
  bounds it to 3–11 characters. Global names use `uniqueString(resourceGroup().id)`.
- Existing shared resources are `${namePrefix}-env`, Basic ACR
  `${namePrefix}acr${suffix}`, and Log Analytics `${namePrefix}-logs` with 30-day
  retention. ACR admin credentials are disabled; workloads use managed identities.
- Cosmos account `${namePrefix}-cosmos-${suffix}` uses Session consistency,
  disabled local authentication, free tier, a 1,000 RU/s account throughput limit,
  and the `incidents` database provisioned at **1,000 RU/s shared throughput**.
  Its existing `incidents` container is partitioned on `/tenant_id`; consistent
  indexing includes `/*` and excludes evidence URI and description. These settings
  remain unchanged. No parameter files or parent modules exist under `infra/`;
  CD directly deploys standalone resource-group templates with core outputs.
- Current identities include ingestion, Worker, Notification, dashboard and two
  queue scalers. Worker has Cosmos Built-in Data Contributor at account scope;
  dashboard has account-scoped Data Reader. Developer contributors also retain
  their existing account scope. Notification has queue receive, ACR pull, Key
  Vault secret read and evidence Blob read permissions; none are changed here.
- Worker configuration already selects Cosmos persistence and Service Bus
  notification publication. It has `SERVICE_BUS_FQDN`, `SERVICE_BUS_QUEUE`,
  `NOTIFICATION_PUBLISHER=servicebus`, `NOTIFICATION_QUEUE`, `AZURE_CLIENT_ID`,
  `SERVICE_BUS_TRANSPORT=amqp`, `INCIDENT_STORE=cosmos`, `COSMOS_ENDPOINT`,
  `COSMOS_DATABASE` and `COSMOS_CONTAINER`. Two identities are attached; the
  client id explicitly selects the application identity instead of the scaler.
- Subscription API Dockerfile uses Python 3.12, user `appuser` (UID 10001), and
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`. The API defaults to Cosmos
  storage and Entra authentication. Its local development bypass is explicit
  and is not used in the new production template.
- Current CD order is core, image builds, ingestion, Worker, Notification,
  dashboard, health/end-to-end checks, simulator and workbook. Subscription
  Service is absent from the build/deploy path. CI's test/build/security matrices
  also omit it; CI's infrastructure job accesses Azure for a core-only what-if.
  Both workflow files were inspected and left unchanged.
- The prior local verification report records 307 passing tests on Python
  3.12.10 and an unavailable Docker engine. Those suites were not rerun in this
  infrastructure-only phase; no new application or container validation is claimed.

## Exact changes and contracts

Modified:

1. `infra/core.bicep`: adds four resources (subscriptions container, dedicated
   identity, ACR pull assignment and Cosmos data contributor assignment), and
   three additive outputs. No existing resource or output was changed semantically.
2. `infra/worker.bicep`: adds defaulted `cosmosSubscriptionsContainer` parameter
   and `COSMOS_SUBSCRIPTIONS_CONTAINER` environment entry. The file now also ends
   in a newline. Incident persistence, identities, messaging and scaling are intact.
3. `tests/local_integration/.gitignore`: excludes generated `*-phase3b.json` files.

Created:

1. `infra/subscription.bicep`: new Subscription API Container App template.
2. `infra/SUBSCRIPTION_DEPLOYMENT.md`: this report and future deployment plan.
3. `tests/local_integration/validate_phase3b.py`: offline compiled-template checks.

Generated local artifacts (ignored): `tests/local_integration/core-phase3b.json`,
`worker-phase3b.json` and `subscription-phase3b.json`. Baseline compiler input
files were created only in a temporary directory and removed when validation ended.

New core outputs are `subscriptionIdentityId`, `subscriptionIdentityClientId`
and `cosmosSubscriptionsContainerName`. All prior outputs remain intact.

| Subscription template parameter | Future source |
|---|---|
| `location`, `namePrefix` | Same approved values as core; defaults malaysiawest/incident |
| `environmentId` | core `environmentId` |
| `identityId`, `identityClientId` | core `subscriptionIdentityId`, `subscriptionIdentityClientId` |
| `cosmosEndpoint` | core `cosmosEndpoint` |
| `cosmosDatabase` | core `cosmosDatabaseName` |
| `cosmosSubscriptionsContainer` | core `cosmosSubscriptionsContainerName` |
| `image` | Immutable `incident-subscription` image in the existing ACR |
| `authTenantId` | Approved issuer directory; default `subscription().tenantId` |
| `authAudience` | Required reviewed API audience; no guessed value or default |
| `tenantMap` | Required reviewed client-to-platform-tenant mapping; no default |
| `minReplicas`, `maxReplicas` | Default 0/1; minimum allowed 0 or 1, maximum fixed to 1 |

Outputs follow existing service conventions: `appName`, `url`,
`latestRevisionName`. Templates remain standalone; no deployment orchestrator
or workflow was added. Passing an image from a different registry will fail unless
the selected identity has access there: future CD must use the existing core ACR.
Custom database/container values must match core resources and the identity scope.

## Cosmos and identity permissions

`subscriptions` is a child of the existing database, partitioned on `/tenant_id`
with Hash partitioning. Indexing is automatic and consistent, with all paths
included to support tenant/event/enabled/channel lookup and API ordering by id.
No TTL or dedicated throughput options are declared; incidents and subscriptions
share the existing 1,000 RU/s. No second account or database is introduced.

`${namePrefix}-subscription-id` receives:

- **AcrPull**, scoped to the existing registry, for image pulls only.
- **Cosmos DB Built-in Data Contributor**, scoped to
  `${cosmos.id}/dbs/incidents/colls/subscriptions` using the actual database and
  container resource names. This supports subscription CRUD and query/metadata
  operations without incident data access or Azure management-plane privileges.

Assignments have deterministic GUID names based on resource/identity/role ids.
The Cosmos assignment references the container, establishing provisioning
dependency. Repeated deployments use the same names and desired configuration.
This is template-level idempotence; no live redeployment was tested.

The existing Worker's account contributor assignment already allows subscription
reads, so no extra Worker assignment is needed. A separate reviewed least-privilege
migration could replace it with an incidents-container contributor and a
subscriptions-container reader. That migration is intentionally deferred to
avoid permission removal or propagation risks during this preparation. Existing
dashboard/developer account-wide permissions likewise reach the new container;
their access should be considered in a later permission review. API isolation
does not narrow the privileges of these existing identities.

## Container App and authentication

`${namePrefix}-subscription` reuses the existing environment and registry,
attaches only its dedicated identity, and selects it using `AZURE_CLIENT_ID`.
Resources are 0.5 vCPU / 1 GiB, minimum zero and maximum one replica, with HTTP
concurrency target 10 and Single active revision. Cold starts are expected.
No startup command override changes the Dockerfile contract.

Environment variables use the actual application configuration names:
`SUBSCRIPTION_STORE=cosmos`, `COSMOS_ENDPOINT`, `COSMOS_DATABASE`,
`COSMOS_SUBSCRIPTIONS_CONTAINER`, `AZURE_CLIENT_ID`, `AUTH_MODE=entra`,
`AUTH_TENANT_ID`, `AUTH_AUDIENCE`, `TENANT_MAP`. No credentials, destinations,
tokens, passwords, secrets or development authentication settings are supplied.
Audience and tenant mapping are required parameterized non-secret configuration,
following the ingestion template's existing approach. Their precise approved
values and future GitHub variable names must be decided in Phase 3B-2; no Entra
registration or roles have been created by this Bicep.

Ingress is **publicly reachable over HTTPS**, targeting port 8000 with
`allowInsecure=false`. No Container Apps authConfig is added: the existing API
performs bearer-token authentication and tenant authorization. Protected CRUD
requires a valid RS256 token with supported issuer, audience and expiry, a client
mapped in TENANT_MAP, and `Subscriptions.Read` or `Subscriptions.Write` for reads;
writes require `Subscriptions.Write`. Missing/invalid bearer tokens return 401;
unmapped clients or missing permissions return 403. Cross-tenant item access
returns 404. `Incidents.Write` alone does not authorize subscription management.

`/health`, `/ready`, `/docs`, `/redoc` and `/openapi.json` remain public under the
existing API contract. Health/readiness expose service/store/auth status, not
tenant records or secrets. Liveness probes `/health` without a Cosmos dependency;
readiness probes `/ready`, which checks container access and a synthetic tenant
query. Readiness timeout is 10 seconds to accommodate the store's five-second
operation timeout. Public ingress follows the existing ingestion convention;
network restrictions, API gateway controls or documentation restrictions would
require a separately reviewed design. Entra signing-key retrieval needs outbound
network access at runtime. Production health/readiness, restricted Cosmos RBAC,
token acquisition and authorization still require future Azure verification.

## Expected resource and cost impact

One new container and identity, two permission assignments and one Container App.
No new ACR, environment, database, Cosmos account, Service Bus namespace/queue,
Key Vault, storage account or evidence resource. Provisioned database throughput
and the account limit remain 1,000 RU/s. Subscription queries and CRUD consume
that same budget, so incident processing and readiness checks may compete for RUs;
monitor throttling before increasing replicas or throughput. Subscription records
and indexes increase stored data. API compute when active, image storage and
Log Analytics ingestion may add usage/cost; scale-to-zero reduces idle compute.
No live pricing, quota, free-tier eligibility, actual usage or cost estimate was
queried. Free tier is preserved in the template, not a guarantee of zero cost.

## Local validation results

Standalone **Bicep CLI 0.47.16 (3f73e1a234)** was already installed at
`C:\Users\rames\.azure\bin\bicep.exe`. All three changed/new templates compiled
successfully using `--no-restore` with no compiler diagnostics. No tools were
installed; no az command was used. Exact commands, from repository root:

```powershell
& 'C:\Users\rames\.azure\bin\bicep.exe' --version
& 'C:\Users\rames\.azure\bin\bicep.exe' build infra/core.bicep --no-restore --outfile tests/local_integration/core-phase3b.json
& 'C:\Users\rames\.azure\bin\bicep.exe' build infra/worker.bicep --no-restore --outfile tests/local_integration/worker-phase3b.json
& 'C:\Users\rames\.azure\bin\bicep.exe' build infra/subscription.bicep --no-restore --outfile tests/local_integration/subscription-phase3b.json
.venv-phase3/Scripts/python.exe tests/local_integration/validate_phase3b.py 'C:\Users\rames\.azure\bin\bicep.exe'
git diff --check
git status --short
git diff --stat
git diff --name-status
git ls-files --others --exclude-standard
git diff -- .github/workflows/ci.yml .github/workflows/cd.yml
```

The Python 3.12.10 validator compiles the tracked HEAD core/Worker templates
locally with `--no-restore --stdout`, compares compiled resources and outputs,
and checks the new module's configuration and actual Dockerfile/env names.

- **PASS:** every pre-existing core resource, output and parameter preserved.
- **PASS:** exactly four additive core resources, three additive outputs;
  correct partition key; no dedicated throughput or TTL; container data scope.
- **PASS:** Worker changed only by the new defaulted parameter and env entry.
- **PASS:** API contracts, authentication, ingress, resources, probes and startup.
- **PASS:** `git diff --check`; workflow diff empty.
- **NOT VERIFIED:** Azure resource-provider acceptance, RBAC propagation and SDK
  access with restricted identity, regional quota, deployment, API token flows,
  Docker builds, actual notification/evidence delivery and live cost.

An initial static-check assertion treated a compiled variable expression as a
literal container name; the validator was corrected and the final execution
passed all groups. No infrastructure compiler error was found. Git emits its
existing LF-to-CRLF warnings for pre-existing Python changes.

## Historical Phase 3B-2 preparation plan

The checklist below records the plan at Phase 3B-1, when workflows were unchanged.
CI/test/build integration and manual CD ordering are now implemented. The earlier
live PR preview was replaced by manual-only Azure preview, and CD no longer has a
push trigger or incident POST checks. Live CRUD, tenant isolation and delivery
verification remain separately authorized work; consult the current rollout guide.

Historical checklist:

1. Add Subscription Service to Python 3.12 test, build and source/dependency/image
   security checks. Add the cross-service local integration tests with all three
   services' dependencies in an isolated job. Preserve the Common Event Model gate.
2. Add offline Bicep compilation for core, Worker and Subscription templates.
   Review the existing live core what-if job separately; do not treat offline
   compilation as equivalent to provider validation.
3. Add `subscription-service/**` to CD's trigger paths, immutable subscription
   image build/output, extraction of the three new core outputs, Subscription API
   deployment and explicit core-to-module parameter wiring. Keep default location
   and prefix consistent, or pass reviewed overrides to every affected template.
4. Wire approved issuer/audience and subscription-manager tenant map. Do not
   blindly copy a producer-wide TENANT_MAP or assume deploy/operator identities
   have Subscriptions roles. Define/assign approved Entra app roles/delegated
   scopes and consent separately; the workload managed identity is for Cosmos/ACR,
   not a grant of API client authorization.
5. Gate deployment so compatible Notification goes before routing-enabled Worker.
   Provision storage/RBAC and API before Worker rollout, and wait for permission
   propagation/readiness and approved test-subscription preparation.
6. Add protected API CRUD, tenant isolation and subscribed/unsubscribed routing
   verification with approved low-volume e2e_test events. Verify Cosmos persistence
   separately; queue draining does not prove per-incident persistence or delivery.
   Avoid enabling load-test human notifications or assuming exactly-once delivery.

At Phase 3B-1, CD deployed Worker before Notification and lacked API deployment
and subscriber verification gates. Current manual CD deploys compatible Notification
first, then storage and the API, and optionally activates Worker last after approval.
With the routing-enabled Worker, zero matches
means no notifications; pre-existing tenants need an approved subscription
migration before cutover. Do not infer Azure readiness from the local 307 tests.

## Proposed future deployment sequence and rollback

For an existing environment, using existing resources until the additive core step:

1. Deploy backward-compatible Notification Service first with an immutable image.
2. Verify legacy flat incident notification handling under an approved test plan.
3. Provision subscriptions container, dedicated identity and RBAC using additive
   core changes; allow permission propagation. Do not remove existing resources.
4. Deploy Subscription API with approved issuer/audience/map; verify Cosmos-backed
   readiness and protected API responses before sending routing traffic.
5. Configure approved Entra roles/scopes, consent and tenant mappings, then create
   approved tenant test subscriptions through the authenticated API. Complete the
   tenant subscription migration needed to preserve intended notification coverage.
6. Deploy routing-enabled Worker **last** with the new container parameter while
   preserving `INCIDENT_STORE=cosmos` and existing queue configuration.
7. Verify subscribed tenants persist and notify; unsubscribed tenants persist with
   no notification; disabled subscriptions and tenant isolation behave correctly.
8. Verify evidence photos and failed-photo text fallback to the same destination.
9. Monitor notification queue age/depth, both dead-letter queues, application errors
   and Cosmos throttling. Pause further rollout if evidence is inconclusive.

The verification and any real Telegram sends require separate approval and an
approved destination/test plan. The literal sequence assumes the existing shared
environment/resources are already present; a new-environment bootstrap needs a
separately reviewed prerequisite plan.

Before rollout, record the immutable **pre-routing Worker image** and its working
configuration. On routing failure, retain the compatible Notification Service
and roll Worker back to that image, rather than changing INCIDENT_STORE or
inventing a routing flag. The legacy Worker may resume its previous flat/global
notification behavior; review that destination before rollback. Preserve Cosmos
incidents/subscriptions and their storage/RBAC; API rollback or access restriction
must not delete the data. Do not automatically replay dead letters. Diagnose,
authorize and deliberately replay only selected messages, accounting for retry
duplicates and partial fan-out. Immutable ids help retries but do not guarantee
exactly-once delivery.

## Git summary and stop point

At phase start, eight tracked Worker/Notification Python/test files were modified
(171 insertions, 63 deletions), alongside untracked Subscription Service, routing
implementation/docs/tests and the prior local harness. These were preserved.
This phase adds core/Worker Bicep tracked changes (72 insertions, two deletions,
including comment/newline replacement), a new untracked Subscription template,
this report and static validator, and extends the pre-existing harness ignore file.
`git diff --stat` includes tracked files only and omits untracked additions.

Unrelated pre-existing cd/ci/core conflict/main/ours files,
`merge-conflict-files.zip` and `project_structure.txt` remain untouched and must
be excluded from a future PR. Generated compiler JSON files and local environments
are ignored. No staging, commit, deletion of user files or workflow modification
occurred. No Phase 3B-2 changes have begun.

**PHASE 3B-1 READY FOR REVIEW** — local compiler/static verification succeeded;
Azure deployment readiness and cloud validation are not claimed. Stop here and
wait for user approval before further changes.
