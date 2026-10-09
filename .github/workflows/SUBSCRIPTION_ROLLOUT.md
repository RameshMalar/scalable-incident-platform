# Phase 3B-2 — local CI/CD preparation

Prepared on 2026-10-09. **PHASE 3B-2 READY FOR REVIEW**.

This verdict concerns reviewable local workflow changes. It does not establish
GitHub protection settings, successful remote workflow execution, Azure deployment
readiness, Docker builds, live authorization, queue processing or Telegram delivery.
No remote workflow, Azure CLI/cloud operation, credentials, Git staging, commit,
push, merge or PR creation was used during this phase. No local software was installed.

## Files changed

Workflow files modified: `.github/workflows/ci.yml` and
`.github/workflows/cd.yml`. `evidence.yml` was read and preserved;
`evidence-batch.yml` is also unchanged.

New files:

- `.github/workflows/SUBSCRIPTION_ROLLOUT.md` — this report and gate/configuration plan.
- `scripts/validate-rollout-config.py` — offline, fail-closed input/configuration checks.
- `scripts/verify-notification-compatibility.py` — real Notification processor with
  MockNotifier, accepting flat and routed messages and rejecting invalid envelopes.
- `scripts/verify-container-revision.py` — offline validation of supplied revision
  JSON, expected image, active/healthy/running state and required configuration.
- `scripts/verify-subscription-api.sh` — future CD-only protected API/readiness checks;
  not executed locally and never creates subscriptions or posts incidents.
- `tests/local_integration/validate_phase3b2.py` — YAML, job dependencies, order,
  module/output contracts, retained CI gates and Bash syntax validation.
- `tests/local_integration/test_workflow_safety.py` — 38 synthetic negative/positive
  approval, configuration and revision gate tests.

Also modified `tests/local_integration/.gitignore` to exclude generated
`*-phase3b2.json` compiler artifacts. No production application Python, Common
Event Model, Bicep source, dashboard, simulator, evidence images or secret was
changed in this phase. Helper Python scripts are verification tooling, not
application modules. Synthetic test values are isolated from deployment configuration.

## CI changes and preserved checks

Subscription Service now joins the existing Python 3.12 unit-test matrix,
image build/smoke/Trivy matrix and Bandit/pip-audit matrix. Its image is
`incident-subscription:ci`, built from `./subscription-service` for linux/amd64.
Its Dockerfile's existing UID 10001/non-root configuration remains unchanged.
The proposed smoke check asserts non-root user, Python 3.12 and importability
with networking disabled, then checks local health/readiness in explicit
development authentication with an in-memory store. No cloud variables or
credentials are passed to that container.

The new integration job installs all three services' test dependencies plus the
already locally available PyYAML 6.0.3 parser, runs the existing 21 shared-state
integration cases and 38 new rollout guard cases, and performs workflow/static
validation. External delivery is mocked; the 21 integration tests retain their
external-socket guard. A new Bicep job compiles every infra template without an
Azure login. The existing PR-only live core what-if job is preserved unchanged;
it was not run locally and is skipped when CI is called by manual CD.

CI also checks the actual Notification image using the new compatibility script
mounted read-only in a `--network none` container. This is distinct from runtime
readiness and does not send a live legacy alert.

All original Ingestion, Worker, Notification, simulator and dashboard image/smoke
checks, Common Event Model comparisons, source/dependency scans and HIGH/CRITICAL
Trivy failure gates remain. Image builds now explicitly target linux/amd64.
The aggregate `Required CI gate` runs with `always()` and requires contract,
Bicep, unit tests, integration, builds and security jobs all to succeed. A failed
or skipped prerequisite does not become a successful gate. CI retains its
feature-branch push, PR and manual triggers and adds `workflow_call`.

CD calls the same local CI workflow at the selected commit. It does not infer
that an earlier PR run covered the selected deployment SHA. Its preparation and
activation jobs both depend on that reusable CI completion. Ordinary CI jobs
have contents-read permission and no Azure credentials; only the preserved PR
preview job requests its existing OIDC permission. Repository branch protection
must separately select required CI checks if merge blocking is desired; no
branch-protection settings were read or changed.

## CD trigger and dependency graph

The **automatic push-to-main deployment trigger was removed**. CD can only be
manually dispatched, and its offline preflight rejects refs other than main.
No ordinary merge deploys production through this workflow. This intentionally
also removes the old path filters: deployment now has no push trigger to filter.

```text
CI checks at selected commit ---------+
                                     +--> protected production preparation
input + environment-protection gate -+      |
                                            +--> exact-image scans and mocked compatibility
                                            +--> Notification deployment + runtime/startup checks
                                            +--> additive core storage/identity/RBAC
                                            +--> Subscription API deployment + protected-read checks
                                            +--> existing ingestion/dashboard/simulator/workbook steps
                                            |
                         optional explicit activation + second production approval
                                            |
                                            +--> recheck API/enabled subscription and Notification
                                            +--> routing-enabled Worker LAST
                                            +--> expected revision/configuration/startup checks
```

The Notification-first step reads the outputs of an explicitly approved
**existing successful core deployment**. This supplies the existing environment,
ACR, notification identities, queues and evidence configuration before the new
subscriptions resources are provisioned. It does not guess resource ids or
bootstrap a fresh environment. The baseline/core deployment must use the current
workflow's existing default `incident` prefix and `malaysiawest` location; custom
prefix/location support or new-environment bootstrap requires another review.
Before any workload update, the baseline environment id is checked against the
local core prefix default and approved subscription/resource group, and its actual
region is checked against the local core location default. A mismatch fails closed.
The existing `incident-worker` name is reused only for rollback capture, matching
the prior workflow and default Bicep contract.

Preparation does not change Worker. It can complete with `activate_routing=false`.
The API can then be used, under a separately authorized plan, to prepare approved
subscriptions. For an activation dispatch, the optional activation job waits on
all preparation and another approval of the same existing `production` environment.
It rechecks the API and an enabled subscription for the verifier tenant, rechecks
Notification readiness, then deploys Worker with explicit
`cosmosSubscriptionsContainer` from core. `INCIDENT_STORE=cosmos`, notification
publishing, Cosmos connection/identity and queue contracts remain intact.

## Manual approvals and fail-closed behavior

Only the already existing `production` environment is named. Before any deployment
job is scheduled, preflight reads its GitHub protection rules and requires at
least one required reviewer with `prevent_self_review=true`. The read uses the
built-in GitHub token with contents/actions read permissions; no PAT or new secret
was introduced. An absent environment, missing protection, unsupported feature
or denied API read fails closed. Actual GitHub settings/token API permissions are
**unverified** locally. The workflow does not create or modify the environment.
Administrators should restrict its deployment branches to the approved main
branch and review bypass permissions before executing a rollout.

The inputs `subscription_configuration_approved`, `activate_routing` and
`routing_configuration_approved` all default to false. Preparation requires
explicit Subscription API configuration review. Activation additionally requires
the routing approval boolean, a non-empty review evidence reference and an
approved pre-routing Worker image pinned by digest. Typed booleans and valid
mapping/UUID/configuration inputs are checked offline; missing or unsafe values
fail before cloud authentication. Required variables are checked again after
entering production so existing environment-scoped variables are supported.

Before any service update on an activation dispatch, the approved rollback digest
must match the currently deployed Worker's resolved image in the existing ACR.
Matching a digest does **not** automatically prove that the image is pre-routing:
the operator/reviewer must establish that provenance and record the review
reference. No inferred rollback image, app role or subscriber is invented.

Job-level production reviews are the human authorization boundary. Boolean
attestations are supplementary configuration assertions, not a substitute for
reviewers. The activation reviewer must verify intended tenants, approved demo
subscriptions and Notification legacy/routed compatibility. API list checks prove
only the deploy verifier's tenant has an enabled Telegram subscription, not that
every tenant has been migrated or a specific business event is subscribed.

## Required configuration and unresolved decisions

Existing variables remain required, with no values invented or changed:

- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`.
- `AUTH_AUDIENCE` and `TENANT_MAP` for the existing Ingestion/simulator contract.
- `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN_SECRET_URI` for Notification.
- Existing optional `DEVELOPER_PRINCIPAL_IDS` retains its existing core behavior.

New manually supplied **input names**, rather than assumed new repository variables:

| Input | Requirement |
|---|---|
| `core_deployment_name` | Approved existing successful core deployment; required |
| `subscription_auth_tenant_id` | Reviewed Entra issuer directory; required |
| `subscription_auth_audience` | Reviewed Subscription API token audience; required |
| `subscription_tenant_map` | Reviewed subscription-manager mapping; required |
| `subscription_configuration_approved` | Explicit roles/scopes/consent/configuration review |
| `activate_routing` | Optional, default false |
| `routing_configuration_approved` | Required true for activation |
| `routing_approval_reference` | Required non-empty for activation; review evidence/reference |
| `rollback_worker_image` | Required pre-routing digest in existing ACR for activation |

No audience, directory/client/resource identifier, mapping, subscription id or
real Telegram destination is hardcoded. API settings are passed as quoted
environment values into existing Bicep parameters. Subscription authentication
is separate from producer authorization; the existing producer mapping is not
blindly reused for subscription management.

The existing deploy identity needs an explicitly approved `Subscriptions.Read`
role/scope (Write also permits reads) on the approved Subscription API audience,
plus an entry in the subscription-manager TENANT_MAP, to perform the read-only
authenticated deployment check. Role definition/assignment, consent, issuer,
audience choice and cross-tenant subscription migration remain external decisions.
No Entra roles, consent or mappings are automatically provisioned by this workflow.
The required-role check is an actual authenticated API read, not a health claim;
if token acquisition/authorization fails, Worker is not activated.

Unresolved execution prerequisites: production reviewer protection and feature
support; GitHub-token permission to inspect it; approved subscription issuer,
audience, manager mapping and roles; retained pre-routing image provenance;
approved tenant/demo subscription migration; actual regional quota and Docker/image
scan results. These intentionally block deployment until verified/approved.

## Images, scanning and runtime verification

CD keeps the existing ACR OIDC login and three-attempt build/push convention,
adds `./subscription-service`, and builds all six images for linux/amd64.
Commit-SHA tags are still published for traceability; **latest is no longer updated**.
Buildx metadata supplies content digests, and every workload receives an immutable
`registry/repository@sha256:...` image reference. The exact rebuilt deployment
digests are scanned again with the existing Trivy action/severity/failure rules
before any Container App update, since a CD rebuild can differ from a CI build
when base images or unpinned dependencies change.

Notification's exact image must accept flat legacy and v1 routed envelopes and
reject invalid routes in an isolated, mocked check. Its deployed revision must
be active, provisioned, healthy and running on that expected digest, with the
expected provider/queue/identity settings and its `notification service ready
notifier=telegram` startup log. Worker must meet analogous runtime/image/config
checks, preserve Cosmos persistence and log `worker ready ... store=cosmos+notification`.
These services have no HTTP readiness endpoint; runtime status alone is not treated
as evidence that all processing dependencies initialized.

The rollout explicitly requests **one warm minimum replica** for Notification
and, on activation, Worker so scaled-zero revisions cannot falsely satisfy startup
readiness. Notification remains capped at one; Worker's existing maximum remains
unchanged. This increases idle compute relative to the old minimum-zero deployment
and keeps consumers running. Approval of this rollout must include that cost/quota
and consumer behavior. No automated scale-down is performed afterward; restoring
scale-to-zero requires a separately reviewed change. A new Notification deployment
can process queued real messages; absence of a new test POST does not imply absence
of real service delivery during a future authorized rollout.

The Subscription API check verifies Cosmos-backed health/readiness, rejection of
anonymous and malformed bearer tokens (401), and an authenticated tenant-scoped
GET with the approved deploy identity. Before Worker activation, pagination seeks
an enabled Telegram subscription for that identity's mapped tenant. It does not
create subscriptions or log tokens/returned tenant records. Tokens are masked.

The authenticated Worker end-to-end POST/queue-drain test and the anonymous
incident POST are removed from CD. Ingestion verification uses only GET health
and readiness checks, confirming Service Bus readiness and the reported Entra
configuration. These checks do not prove authentication enforcement. Live
incident authorization/E2E testing requires its separately approved phase.
Dashboard health checks and the Subscription API's non-mutating bearer/tenant
read checks remain.

The local workflow validator rejects literal `/api/v1/incidents` endpoints in
run blocks, workflow/job/step environment values and directly referenced local
Python/Bash scripts in CD and environment-bearing workflows. It conservatively
rejects every method, catching explicit POST, curl's implicit POST data options,
option reordering, continuations, simple quote concatenation and percent-encoded
paths. Regression tests exercise these forms. This is not a general static
security guarantee: dynamically assembled URLs, repeated/other encodings, nested
helpers, generated scripts and third-party actions require manual review. Literal
endpoint mentions in inspected comments can also cause conservative rejection.
Manual gates, Notification-first preparation, Worker-last activation and immutable
rollback approval requirements are preserved.

Simulator deployment still provisions its job without running it; workbook deployment
retains its existing continue-on-error behavior. Evidence workflows remain unchanged
and still require separate authorization; nothing here triggers them.

## Local validation results and exact commands

**PASS** on Python 3.12.10: final combined run **59 passed, zero failed/skipped**
(21 existing integration cases rerun + 38 new guard tests). The independent earlier
38-case run is not counted again. One existing Starlette/httpx TestClient deprecation
warning remains. The previous 286 service unit tests/307 total belong to Phase 3;
they were not rerun or claimed as new executions in this phase.

**PASS**: compatibility script executed against local Notification code with
MockNotifier and network connections blocked. This is a script check, not another
pytest case and not a Docker result.

**PASS**: all nine Bicep templates compiled with existing standalone Bicep
0.47.16 in offline `--no-restore` mode: core, dashboard, ingestion, notification,
simulator-job, simulator, subscription, workbook and Worker. No az CLI was used.

**PASS**: YAML parsing (including YAML 1.2 handling of `on` and duplicate-key
rejection), dependency/reference/order checks, default-off/manual gates, preservation
of original CI checks, exact-image scans, module required parameters/core outputs,
and Bash syntax of 45 CI/CD run blocks and the API checker. Bash was used with `-n`
only; none of the cloud/deployment shell blocks were executed.

**PASS**: `git diff --check`. Existing Python files emit Git's LF-to-CRLF warnings.
Evidence workflow diffs are empty. No actionlint or shellcheck binary was found;
their checks are **NOT VERIFIED**. The local custom workflow/graph/Bash checks do
not replace GitHub's full expression/action-schema validation.

**NOT VERIFIED / not executed**: Docker builds/smokes/image compatibility in a
container, actual Trivy/Bandit/pip-audit runs in the new CI jobs, GitHub environment
settings and remote execution, Azure runtime/revision/log/API checks, token flows,
RBAC propagation, actual subscriptions/queues/evidence and Telegram delivery.

Commands from repository root unless noted:

```powershell
.venv-phase3/Scripts/python.exe -m pytest tests/local_integration/test_phase3.py tests/local_integration/test_workflow_safety.py -q -o asyncio_mode=auto
.venv-phase3/Scripts/python.exe tests/local_integration/validate_phase3b2.py
$bicepToolPath = 'C:\Users\rames\.azure\bin\bicep.exe'
foreach ($template in Get-ChildItem -LiteralPath infra -Filter '*.bicep') {
  & $bicepToolPath build $template.FullName --no-restore --outfile "tests/local_integration/$($template.BaseName)-phase3b2.json"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
# Compatibility check executed with working-directory notification-service:
../.venv-phase3/Scripts/python.exe ../scripts/verify-notification-compatibility.py
git diff --check
git status --short
git diff --stat
git diff --name-status
git ls-files --others --exclude-standard
git diff -- .github/workflows/evidence.yml .github/workflows/evidence-batch.yml
```

## Rollback and Git separation

Rollback remains a separately authorized deployment operation, not an automatic
workflow reaction. Keep backward-compatible Notification deployed, restore the
approved immutable pre-routing Worker image and its known working configuration,
preserve Cosmos incidents/subscriptions, and do not change INCIDENT_STORE as a
routing toggle. The old Worker may resume flat/global delivery; review that
destination. Do not replay dead letters automatically. Startup and digest checks
do not establish exactly-once delivery or successful per-incident persistence.

Phase-start changes already included ten tracked Bicep/Worker/Notification files
(243 insertions, 65 deletions), untracked Subscription Service/routing sources,
Phase 3 integration files and Phase 3B-1 docs/template/validator. This phase adds
only the two tracked workflow diffs plus the new tooling/report/guard tests and
the harness ignore-pattern update. The pre-existing application and infrastructure
changes remain preserved; they must be reviewed separately for any future PR.
`git diff --stat` omits untracked additions, including this report and helper scripts.
Generated compiler JSON and the local Python environment are ignored.

Unrelated pre-existing cd/ci/core conflict/main/ours files,
`merge-conflict-files.zip` and `project_structure.txt` remain untouched and must
be excluded from any future PR. No files were staged, committed, pushed, merged
or used to create a PR. No remote workflow was triggered. This phase stops at
local review preparation.

**PHASE 3B-2 READY FOR REVIEW**

Deployment remains blocked pending the explicit configuration/protection and
execution prerequisites above. Stop here and wait for approval before further changes.


## Phase 3C-2 integration on verified main

The isolated `feature/subscription-api-rollout` worktree is based on
`a5089c4761577367f2d6136d5c86f459feb23b7f`. Earlier phase statements above are
historical; this integration preserves main's shared `common_event_model`
package, producer SDK checks, root-context service images, dashboard import and
evidence checks, Cosmos/Worker diagnostics, workspace parameters and alerting.
Worker and Notification routing models and tests import the shared package;
no Common Event Model source changes were required. API event types remain open
strings, and envelopes retain evidence URI and event purpose.

CI and CD build ingestion, Worker and Notification from the repository root
with explicit service Dockerfiles. Subscription builds from its own directory:
it stores subscription configuration and does not import the incident model.
The aggregate CI gate also requires the producer SDK job. Subscription pins
PyJWT 2.15.0, matching main's security requirement, with a regression asserting
that a deeply nested untrusted token returns 401 before any key-fetch request.

Final offline Python 3.12.10 checks: Worker 139; Notification 80; Subscription
68; ingestion 66; simulator 86; shared-model/producer SDK 76; integration/safety
73. Total: 588 passed. External sockets were blocked and cloud/auth configuration
cleared. Local shared package sources were on PYTHONPATH; cached PyJWT 2.15.0 and
locally available Jinja2/MarkupSafe sources supplied missing test dependencies.
The first ingestion collection attempt lacked Jinja2; it was resolved offline,
and the final suite includes main's deep-JWT regression. No tests were disabled.
The mocked Notification compatibility script passed. All ten Bicep templates
compile offline; compiled baseline checks preserve every existing core resource,
output and parameter and Worker diagnostics. YAML, workflow gates, Docker context
contracts and Bash syntax checks passed.

Docker CLI is installed but its Linux daemon is unavailable; no image build or
image-level runtime verification is claimed. GitHub reviewer settings, remote CI,
Azure RBAC and live deployment/E2E behavior remain unverified. No staging, commit,
push, PR, Azure request or deployment occurred. Review and revalidate the isolated
change set before authorizing Git operations.
