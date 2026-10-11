# Phase 3 local integration verification — 2026-10-09

## Historical report and current branch

The results and stopping point below describe the 2026-10-09 Phase 3 exercise.
Since then this branch has integrated subscription-based Worker routing,
Notification compatibility, infrastructure and CI/CD changes. Automatic PR CI
is cloud-free; Azure preview is manual-only from main with protected-environment
approval. CD is manual-only and gates optional Worker activation separately.
The readiness review verified successful CI at `f68e068`, including Docker checks;
Docker unavailability below describes the earlier local run. Historical counts
are not results for subsequent edits, and live Azure/Telegram behavior remains
unverified by this report.


Verdict: **LOCAL INTEGRATION VERIFIED**

PR status: **READY FOR PR PREPARATION** (local application verification only).

## Architecture and scope

The real Subscription API runs through FastAPI TestClient with an injected local
token verifier for TENANT-A and TENANT-B. Its real InMemorySubscriptionStore is
the sole subscription state. A test-only SharedReader reads that same store on
every Worker lookup and applies the Worker's production subscription validation
and matching functions. API PATCH changes are therefore immediately visible.

Synthetic JSON is parsed by the real Worker IncidentProcessor, then processed by
PersistAndNotifyHandler and SubscriptionRouter. Incident persistence and message
publication are injected local boundaries. The publisher serializes the real
version-1 envelope and uses the production route_message_id function. The real
NotificationProcessor validates the serialized envelope and invokes the real
TelegramNotifier with a mocked HTTP session and mocked evidence fetcher.

The assertions verify validation before persistence, persistence before lookup
and publication, and suppression before lookup. Azure persistence, broker
settlement and duplicate detection, and actual Blob/Telegram delivery are outside
this local test's scope. No exactly-once guarantee is claimed.

External socket connections are blocked. Literal loopback sockets are allowed
because Windows asyncio uses them internally. No live Azure or Telegram was
accessed. No credentials, infrastructure, workflows, Common Event Model,
dashboard, or evidence files were modified. No containers were deployed and no
Git staging, commit, push, PR creation, or merge was performed.

## Executed scenario results

| Scenario | Result | Evidence |
|---|---|---|
| A: subscribed TENANT-A | PASS | Actual API POST/GET; persisted incident; one envelope; subscription, destination and complete validated incident retained; mocked text delivery to explicit destination |
| B: unsubscribed TENANT-B | PASS | Persisted; lookup completed; zero routes and delivery |
| C: disabled subscription | PASS | Enabled route first established; actual API PATCH/GET; new event persisted with zero routes after disable |
| D: tenant isolation | PASS | TENANT-B GET/PATCH/DELETE returned 404; list empty; TENANT-A record unchanged |
| E: evidence | PASS | URI retained in persistence and envelope; passed to evidence fetch; photo success and rejected-photo text fallback both used selected destination |
| F: suppression | PASS | load_test and historical EVT-LOAD-* without purpose persisted without lookup; e2e_test matched and delivered |
| G: failure/retry | PASS | Persistence, lookup and publish failures propagated; duplicate handoff retried; partial fan-out repeated successful route; route IDs stable and distinct; invalid envelopes rejected without global fallback |
| Legacy compatibility | PASS | Flat event delivered to synthetic global chat; routed messages delivered to explicit chat |
| Local API health/readiness | PASS | Memory store healthy and ready with injected local test authentication |
| Docker checks | SKIPPED | Docker CLI installed; Linux engine unavailable; no image builds or container checks executed |

## Python and complete suite results

Every final suite used **Python 3.12.10**, pytest 9.1.1 and pytest-asyncio 1.4.0.
Python 3.14.2 is also installed but was not used for these results.

| Suite | Passed | Failed/errors | Skipped | Warnings |
|---|---:|---:|---:|---:|
| Existing Worker | 139 | 0 | 0 | 0 |
| Existing Notification Service | 80 | 0 | 0 | 0 |
| Existing Subscription Service | 67 | 0 | 0 | 2 |
| New local cross-service integration | 21 | 0 | 0 | 1 |
| Total, without double-counting | **307** | **0** | **0** | **3** |

The integration file has 21 executed pytest cases after parametrization. Existing
Worker subscription integration tests remain part of the 139 and are not counted
again as new cross-service tests. These counts are fresh executions, not the
previous Phase 2 results.

Warnings: Starlette deprecated its httpx TestClient integration (one warning each
in Subscription and new integration runs). Subscription pytest could not write
its pre-existing cache nodeids file due to permissions. Git also reported LF to
CRLF normalization warnings for pre-existing modified tracked files. No test
dependencies remained missing after installation. Pip's update notice was not
acted upon. Unpinned dependency resolutions are recorded in the local
dependency-versions.txt artifact.

The first harness run had 21 setup errors: its network guard blocked asyncio's
Windows loopback socketpair. The test-only guard was corrected to permit literal
loopback addresses; the final run passed all 21 with no setup errors. No
production defect was discovered or fixed.

## Exact commands

Run from the repository root in PowerShell:

```powershell
py -0p
py -3.12 --version
py -3.12 -m pytest --version
.venv/Scripts/python.exe --version
docker version
py -3.12 -m venv .venv-phase3
.venv-phase3/Scripts/python.exe -m pip install -r worker-service/requirements-dev.txt -r notification-service/requirements-dev.txt -r subscription-service/requirements-dev.txt
.venv-phase3/Scripts/python.exe -m pytest tests/local_integration/test_phase3.py -q -o asyncio_mode=auto
.venv-phase3/Scripts/python.exe tests/local_integration/run_suite.py worker-service
.venv-phase3/Scripts/python.exe tests/local_integration/run_suite.py notification-service
.venv-phase3/Scripts/python.exe tests/local_integration/run_suite.py subscription-service
.venv-phase3/Scripts/python.exe -m pip freeze
git status --short
git diff --stat
git diff --name-status
git ls-files --others --exclude-standard
```

The suite runner changes into the selected service, runs its entire tests
directory using its pytest configuration, clears cloud/auth/Telegram environment
configuration in that child process, blocks external connections, and writes a
local JUnit XML artifact. No system-wide software was installed.

Python 3.12 verification: **PASS** for all three complete suites and new tests.
Docker verification: **NOT VERIFIED**. Docker CLI 29.7.2 could not connect to
dockerDesktopLinuxEngine. Builds for incident-worker:local,
incident-notification:local and incident-subscription:local were skipped, as were
container Python/import/non-root/API health/readiness checks. No Dockerfiles
were changed and Docker Desktop was not started.

## Exact created files and Git review

Created reviewable files:

- tests/local_integration/test_phase3.py — shared-state harness and scenario tests
- tests/local_integration/run_suite.py — isolated existing-suite runner
- tests/local_integration/REPORT.md — this report
- tests/local_integration/.gitignore — excludes generated local verification artifacts

Created local artifacts:

- tests/local_integration/worker-service-results.xml
- tests/local_integration/notification-service-results.xml
- tests/local_integration/subscription-service-results.xml
- tests/local_integration/dependency-versions.txt
- .venv-phase3/ — isolated Python environment, generated packages/scripts/configuration
- .venv-phase3/.gitignore — excludes the local environment from Git

Existing application/source/test files were not edited by this exercise.

The existing tracked diff remains **8 files, 171 insertions, 63 deletions**:

- Notification: app/notifier.py, app/processor.py, app/telegram_notifier.py
- Worker: app/main.py, app/notification_handler.py, app/notification_publisher.py,
  tests/test_main.py, tests/test_notification_handler.py

Pre-existing untracked implementation groups:

- Entire subscription-service directory (API, storage, authentication, models,
  tests, requirements, Dockerfile, .dockerignore, pytest.ini and API.md)
- Worker ROUTING.md; app/notification_models.py, app/subscription_router.py,
  app/subscription_store.py; tests/test_notification_models.py,
  tests/test_notification_publisher.py, tests/test_subscription_integration.py,
  tests/test_subscription_router.py and tests/test_subscription_store.py
- Notification ROUTING.md, app/notification_models.py and
  tests/test_routed_notifications.py

Unrelated pre-existing files to exclude from a proposed PR:
cd-conflict.txt, cd-main.yml, cd-ours.yml, ci-conflict.txt, ci-main.yml, ci-ours.yml,
core-conflict.txt, core-main.bicep, core-ours.bicep, merge-conflict-files.zip and
project_structure.txt. These were left untouched. Generated local artifacts and
the virtual environment should also be excluded. Git diff does not include
untracked implementation or harness files; they must be reviewed separately
during any subsequently authorized PR preparation.

## Remaining limitations and stopping point

Docker verification remains blocked by the unavailable engine. The cache warning
and TestClient deprecation warning remain unresolved. This exercise verifies
application integration through local boundaries; it does not establish Azure
deployment or infrastructure readiness.

**LOCAL INTEGRATION VERIFIED**

**READY FOR PR PREPARATION**

At the end of this historical exercise, Phase 3B/3C and PR preparation had not
begun. The current branch includes those implementation stages and has undergone
a PR readiness review; no PR creation or live rollout is claimed here.
