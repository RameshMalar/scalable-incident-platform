# Verified LML SENTINEL source consolidation

This integration preserves the working demo without deploying anything. Base:
`ee1b323f8770d4cac13f2a3fca2de53b059094f5`; dashboard/backend source:
`12444baae3d8657610bfdaad7dace3ab8ceeafb3`; verified Grafana source:
`3aac3b8e04d78fa01758e16bb9024a22319af0d5`.

## Deployment boundary

Source pushes and PRs run CI, including security gates. Following explicit
rollout approval, deployable merges to `main` now dispatch production CD through
`.github/workflows/cd-on-merge.yml`. The dispatcher has no Azure credential and
reads the reviewed inputs from `infra/cd-production.json`; it also pins the
triggering merge SHA, which `cd.yml` verifies before its reviewed-merge,
configuration, baseline and queue gates. Evidence upload, Azure
preview and DAST remain manual. Feature flags, permission grants, label moves
and revision promotion remain separately gated by the reviewed configuration.

## Preserved implementations

- Complete Business Dashboard: existing branding/logo, 3rem header clearance,
  overview/charts, evidence, simulation, subscriptions and login/logout. App
  source matches the approved dashboard; Streamlit 1.66.1 and Pillow 13.0.0
  retain the validated dependency-security correction.
- Exact Platform Observability destination:
  `https://incident-grafana--sys-events-3aac3b8e.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/d/incident-platform-ops/platform-observability-7c-health-and-performance?orgId=1&from=now-6h&to=now&timezone=Asia%2FSingapore`.
- Verified **35-panel** Grafana provisioning, seven Workbook monitoring areas,
  System Events correction, original queue panels and explicit missing-data
  semantics. Manual refresh, MSI datasource and Grafana authentication retained.
- Main's existing fire/smoke and people-count ingestion contributions retained.
- Existing evidence, common-event-model/event-purpose/tenant-isolation contracts,
  legacy/routed notification compatibility and tested subscription/ownership
  support. Flag-gated account/self-service/eligibility code is preserved as
  tested source, **not activated or claimed live** by this consolidation.

## Exclusions and deferred activation

Unapplied simulator-authorization patches, obsolete 28-panel generators and
duplicate generated Grafana JSON/text templates are excluded. Two modules under
`business-dashboard/review-proposals/` remain because existing regression tests
exercise them; Docker does not package those offline reference modules.

Dirty worktrees, unverified ingestion instrumentation, pending dependency PRs
and other divergent/experimental branches are preserved separately. No branch
was blindly merged just because it existed. Existing main features and already
integrated evidence/subscription/security fixes form the baseline.

Entra promotion, central-access activation, Telegram self-service/webhook setup,
new permissions, eligibility enforcement, simulator authorization and any
production traffic changes require separate review and authorization. No SSO
is implemented. A fresh Grafana browser session requires Grafana login.

## Validation and limitations

Local Linux/Python 3.12 full validation: **1,187 passed, 1 skipped**, with eight
additional pytest subtests passed. The optional-import skip occurred in a mixed
suite; all nine subscriber-tool tests passed when rerun separately. Existing
common-event-model, producer SDK/evidence, dashboard, Grafana, backend and
cross-service suites ran. Six Bandit Medium/High gates and six dependency audits
passed. The repository-local common-event-model has no PyPI advisory entry;
its source/tests were checked rather than claiming a PyPI vulnerability audit.

All Bicep templates compiled locally. CI/CD graph/contracts and 57 shell blocks
passed syntax-only validation. Dashboard and Grafana images built locally;
35-panel provisioning, Admin/Viewer controls, anonymous rejection, logout and
header-spoof protection passed. Dashboard login/session/logout passed with a
temporary synthetic local account. All 25 query-bearing panels passed read-only
checks against the existing verified Grafana candidate; the original consumed
RU summary still has no data. No monitoring values were fabricated.

No live authenticated browser visual recheck was available in this integration
run; the user had already accepted the unchanged dashboard application. No
Azure what-if/deployment, registry push, end-to-end incident publication or
Telegram test delivery was performed. Full remote CI and independent teammate
review are required before merge.

Demo URLs remain:

- `https://incident-dashboard--header-link-f21eea32.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/`
- The exact Grafana candidate URL above.
- Existing sentinel label URL:
  `https://incident-dashboard---sentinel.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/`.

Historical readiness reports describe their stated dates, not current rollout
approval. The existing demo image predates the dependency update; no rebuilt
integration image should replace it without a separate staged release.
