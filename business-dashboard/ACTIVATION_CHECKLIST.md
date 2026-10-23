# Activation checklist: sign-in, central access, Telegram self-service, simulator

Prepared 19 October 2026 on branch `feature/grafana-observability` (uncommitted).
Audience: the platform owner and the project team. No secret value appears
in this document. Nothing here has been deployed, granted, registered or enabled.

Related: [TELEGRAM_INTEGRATION_REPORT.md](TELEGRAM_INTEGRATION_REPORT.md)
(lifecycle tests), [subscription-service/API.md](../subscription-service/API.md)
(route contract).

## 1. Current decision

| Decision | Status | Becomes GO when |
|---|---|---|
| Merge the prepared code | **NO-GO until reviewed.** It is now behaviour-neutral by design (§6), but needs pull-request review. | Split PRs reviewed and approved by a second team member (two-person rule enforced by CD); §6 checks pass in CI. |
| Initial 0%-traffic sign-in test | **On hold** until Liu Yiting confirms portal access to `incident-platform-api` and approves §3.1 | Liu has applied the four §3.1 items; applying `grantDashboardSigninTestSecret` is approved (§5); the PRs are merged with `dashboard_release: keep`. |
| Dashboard sign-in in production | **NO-GO (deferred)** | The test passes (§4) with recorded evidence, **and** the deferred items in §3.1 are separately approved. |
| Central access, Telegram, simulator | **NO-GO** | Each step's dependency in §7 is met, in order, with the listed approval. |

## 2. What is prepared

| Area | Files | Default effect |
|---|---|---|
| Lockout guard | `.github/workflows/cd.yml`, `infra/cd-production.json`, `scripts/validate-rollout-config.py` | `dashboard_release: keep`: CD builds and scans the dashboard image but **does not deploy it**. |
| Staged sign-in | `infra/dashboard.bicep` | `release: standard` renders the existing template. A read-only `what-if` against production shows the same deltas as the committed template. |
| Startup safety | `business-dashboard/auth_preflight.py`, `Dockerfile`, CI smoke test | A revision with missing or mismatched sign-in configuration never starts, so it cannot take traffic. |
| Least-privilege grants | `infra/activation-grants.bicep` | **Never deployed by CD.** Every grant is off; the `what-if` with defaults shows no change. |
| Webhook rate limits | `subscription-service/app/rate_limit.py` and tests | Inert until Telegram self-service is enabled. |
| Destination review | Subscription API `GET /api/v1/admin/telegram/pending-destinations`; dashboard Admin page | Read-only and audited. **The API still cannot write `approved-destinations`.** |
| Simulator authorization | `business-dashboard/simulation-authorization.v2.proposed.patch` | **Not applied.** It supersedes the v1 patch file. |

Test evidence (Python 3.12, pinned requirements, no network):

| Suite | Passed |
|---|---:|
| Ingestion | 71 |
| Subscription API | 134 |
| Notification Service | 92 |
| Worker | 179 |
| Load simulator | 86 |
| Common event model | 22 |
| Producer SDK | 54 |
| Business Dashboard | 92 |
| Local integration (includes the Telegram lifecycle) | 89 |
| Workflow security (includes the activation-grant guarantees) | 213 |
| **Total in the repository** | **1,032** |
| Simulator v2 patch, separately, in a scratch copy | 94 (the 71 ingestion tests unchanged, plus 23 new) |

The CI/CD freeze tests prove that the only workflow changes are the dashboard
release guard and the dashboard smoke test. All `infra/*.bicep` files compile.
`access-policy.json` is unchanged (SHA-256 `7bf104a7…`).

## 3. Entra configuration plan

Directory `5ba5ef5e-3109-4e77-85bd-cfeb0d347e82` (NUS). Bootstrap Platform Admin
object ID `ded95a62-f8b6-4589-b49a-2041a35d10d7`.

### 3.0 Status: portal registration blocked (19 October 2026)

Both portals returned 401 "You do not have access" for the platform owner.
Read-only CLI checks show **the directory policy is not the cause**:

| Check | Result |
|---|---|
| Directory policy `allowedToCreateApps` | `true` |
| Platform owner's directory roles | none |
| Platform owner already owns | `incident-subscription-demo-manager`, `incident-grafana` |
| User consent | Allowed for low-impact permissions; `openid`, `profile`, `email`, `offline_access` are classified low |

This pattern matches NUS restricting the **admin portal UI** to administrators.
Creating applications by CLI or Graph instead is **not** used: the restriction
signals NUS intent, and the project owner ruled it out.

**Decision (19 October 2026).** The platform owner approved using the existing
`incident-platform-api` registration as the sign-in client for the initial
0%-traffic test **only**. This is subject to Liu Yiting, its owner, confirming
portal access and approving the changes. Nothing proceeds until she confirms.

**Read-only verification behind the decision:**
- The code accepts one registration as both client and API. The ID token
  audience equals the client ID; the user-token verifier accepts v1/v2
  issuers, `api://` and bare audiences, and `azp` or `appid`.
- Pre-authorization satisfies the admin-consent `user_impersonation` scope **for
  a different client**. A delegated Azure CLI token was issued for it with no
  consent grant recorded.
- Whether pre-authorizing the app **on itself** does the same is *unverified*
  and can only be shown by a sign-in. The initial test therefore requests only
  `openid profile email`, which users may consent to themselves.
- 4 users and 7 service principals are assigned to the API enterprise app.
  Assignment required is already on, so only assigned people can sign in.
- The API app holds no application permissions.
- **Never add `fbbe63e1-…` to any `TENANT_MAP`**: the platform owner's tokens
  from it already carry `Subscriptions.Write` and `Incidents.Write`.

**Approved approach: reuse the existing `incident-platform-api`
registration as the dashboard's sign-in client.**
- **Owner:** Liu Yiting owns both the registration and its enterprise app.
- **Already in place:** the enterprise app has *assignment required = Yes*.
- **Key Vault:** Liu holds Key Vault Secrets Officer, so the client secret can go straight into the vault.
- **Admin consent:** no NUS administrator and no new registration are needed.

The dashboard code already supports this:
- set the client ID to `fbbe63e1-1f8f-4d4d-b695-0f9982842081` (`dashboard_auth_client_id`, `DASHBOARD_AUTH_CLIENT_ID`, `USER_AUTH_CLIENT_IDS`, and later `SIMULATION_CLIENT_IDS`);
- Liu's configuration list is §3.1; §3a/§3b are kept only as the Option C fallback.

Trade-off: the API registration gains a client secret, and one registration
is both the client and the resource. The services still refuse that
registration's app-only tokens: it is not in any `TENANT_MAP`, and the
user-token routes require delegated `scp`. Keep the secret short-lived and
only in Key Vault, and move to a dedicated `incident-dashboard-login`
registration if an NUS administrator later creates one (Option C, §3.2).

### 3.1 What Liu Yiting configures on `incident-platform-api`

**Prerequisite: Liu confirms she can open this app's *Authentication* page at
entra.microsoft.com and approves the changes.** If she also receives 401, stop:
the remaining route is an NUS Entra administrator (§3.2).

**Initial scope, approved for the 0%-traffic test.** All four are additive,
and owner rights suffice.

| # | Where | Change | Undo |
|---|---|---|---|
| 1 | Authentication → Add a platform → **Web** | **Test redirect URI only:** `https://incident-dashboard---signin-test.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback`. Leave implicit grant (ID and access tokens) unticked. | Remove the URI |
| 2 | Certificates & secrets → New client secret | Name `incident-dashboard`, expiry 6 months or less; record the expiry date. Liu writes it **only** into Key Vault secret `dashboard-auth-signin-test` (TOML in §3c). The value is never sent to anyone, including the platform owner. | Delete the client secret; disable the Key Vault secret |
| 3 | App roles → Create app role | Display name and value `Dashboard.Read`; allowed member types *Users/Groups*; enabled. Existing roles are unchanged. | Disable, then delete, the role |
| 4 | Enterprise applications → `incident-platform-api` → Users and groups | Assign `ramesh.malar_21@u.nus.edu` (`ded95a62-…`, the approved bootstrap Admin) the `Dashboard.Read` role. Existing assignments are unchanged. | Remove that assignment |

**Deferred: each needs a separate approval after the test passes.**

| Item | Why deferred |
|---|---|
| Production redirect URI `https://incident-dashboard.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback` and secret `dashboard-auth-production` | Needed only to promote (§4 step 6) |
| `Incidents.Simulate` app role and its assignment | Only reveals a simulator page that stays disabled |
| Self-pre-authorization (Expose an API → Authorized client applications → `fbbe63e1-…` for `user_impersonation`) | Unverified for the same client. Test it alone on the 0% revision; if users then see "Need admin approval", fall back to a dedicated client (§3.2), whose pre-authorization is proven. |
| All feature activation (§7 steps 8–16) | Depends on the above |

Liu should not change: the identifier URI, the `user_impersonation`
definition, the existing app roles and assignments, `signInAudience`, the
existing Azure CLI pre-authorization, or "assignment required".

### 3.2 Option C: an NUS Entra administrator

If the team prefers a dedicated registration, an Application Administrator
(or a Cloud Application Administrator) creates `incident-dashboard-login` as
in §3a, with owners Ramesh Malar and Liu Yiting. That administrator can also
pre-authorize it on the API, or Liu can (§3b). Granting a team member the
Application Developer role would also allow it, but is broader than needed.

### 3a. Reference only (Option C): a dedicated `incident-dashboard-login`

Not used for the initial test. Kept as the fallback configuration.

| Setting | Value |
|---|---|
| Supported accounts | This organizational directory only (single tenant) |
| Platform | **Web** (confidential client; Streamlit exchanges the code server-side) |
| Redirect URI 1 (test) | `https://incident-dashboard---signin-test.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback` |
| Redirect URI 2 (production, add before promote) | `https://incident-dashboard.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback` |
| Front-channel logout, implicit grant | None; leave ID/access-token implicit grant **off** |
| Client secret | One secret, at most 6 months; record its expiry date. The value goes only into Key Vault (3c). |
| App roles (bootstrap mode) | `Dashboard.Read` and `Incidents.Simulate`, allowed member type Users. The packaged policy grants only roles the ID token also carries. |
| Enterprise application | **Assignment required = Yes.** Assign `ded95a62-…` both roles. Unassigned users then cannot obtain a token at all. |
| API permissions | Microsoft Graph `openid`, `profile`, `email` (delegated). Later, after 3b: `incident-platform-api` → `user_impersonation`. |

No Azure RBAC role, Cosmos role or subscription access is needed by any
dashboard user. Team members are added later through Admin Access Management.

### 3b. Reference only (Option C): pre-authorizing a dedicated client

`user_impersonation` is an **Admin-consent** scope in NUS. Pre-authorizing the
login app lets signed-in users obtain it with no NUS administrator, as the Azure
CLI already is. This is needed only for central access, Telegram and the
simulator, **not** for the first bootstrap sign-in test. Request to send:

> Hi Yiting, for the Business Dashboard sign-in we need one change on
> `incident-platform-api` (`fbbe63e1-1f8f-4d4d-b695-0f9982842081`):
> Expose an API → Authorized client applications → Add a client application →
> client ID `<incident-dashboard-login client ID>` → tick
> `api://fbbe63e1-1f8f-4d4d-b695-0f9982842081/user_impersonation`.
> No other change: the scope, app roles and the existing Azure CLI
> pre-authorization stay exactly as they are. Could you confirm when done?

### 3c. Key Vault secret (written by Liu Yiting; value never shared)

Vault `incidentkv4pqaac2c4e4hs` (RBAC; Liu holds Key Vault Secrets Officer).
**Initial test: one secret only, `dashboard-auth-signin-test`.** Its value is a
complete Streamlit TOML file. Liu generates `cookie_secret` locally (64+
random characters) and pastes the client secret straight from the portal.
`dashboard-auth-production` is deferred to promotion.

```toml
[auth]
redirect_uri = "https://incident-dashboard---signin-test.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback"
cookie_secret = "<generated locally, 64+ characters>"
expose_tokens = ["id", "access"]

[auth.microsoft]
client_id = "fbbe63e1-1f8f-4d4d-b695-0f9982842081"
client_secret = "<the incident-dashboard client secret value>"
server_metadata_url = "https://login.microsoftonline.com/5ba5ef5e-3109-4e77-85bd-cfeb0d347e82/v2.0/.well-known/openid-configuration"
# Initial test: OpenID scopes only (users may consent themselves). The API
# scope api://fbbe63e1-1f8f-4d4d-b695-0f9982842081/user_impersonation is added
# only after self-pre-authorization is separately approved and tested.
client_kwargs = { scope = "openid profile email", prompt = "select_account" }
```

The dashboard identity can read this one secret only after the
`grantDashboardSigninTestSecret` grant (§5). `auth_preflight.py` checks the
structure at startup and prints only the names of failed checks.

## 4. Lockout prevention and the sign-in test

1. **Merge keeps the dashboard.** `dashboard_release: keep` in
   `infra/cd-production.json` means CD never replaces the running revision
   (`incident-dashboard--0000032`, public, no sign-in) on merge.
2. **Test revision.** A manual CD run with `dashboard_release=signin-test` and
   `dashboard_auth_client_id=fbbe63e1-1f8f-4d4d-b695-0f9982842081` deploys the new image as a new
   revision at **0% traffic** with label `signin-test`. Production keeps 100%
   (labelled `stable`).
3. **Automatic check.** CD waits for `/_stcore/health` on the label URL. Healthy
   also means `auth_preflight.py` accepted the mounted configuration.
4. **Human test, in a private browser window:**

   | Action | Expected result |
   |---|---|
   | Open the label URL | Platform page with **Sign in with Microsoft** |
   | Sign in as `ramesh.malar_21@u.nus.edu`; consent to the basic sign-in permissions if prompted | Overview, All incidents, Evidence and the Grafana link for TENANT-01/02/03. Account Setup and Admin Access Management show "disabled" notices. **No simulator page** (`Incidents.Simulate` is deferred). |
   | Sign in with an account that is not assigned to the app | Microsoft error AADSTS50105 (not assigned); no dashboard |
   | Optional, with a volunteer: a team member who is assigned other API roles but not approved for the dashboard | Microsoft sign-in succeeds; the dashboard still denies access |
   | Open the production URL | The existing public dashboard, unchanged |

   If Microsoft shows "Need admin approval", stop: the test requested more
   than `openid profile email`. Check the `dashboard-auth-signin-test` secret.

5. **Evidence.** Record the date, the account used and the results as
   `dashboard_signin_reference`.
6. **Promote (deferred)**, with a separate approval: Liu adds the production redirect URI and writes `dashboard-auth-production`, then run CD with
   `dashboard_release=promote` and the reference. Production then requires
   sign-in; the old revision stays at 0% as `previous`.
7. **Rollback**, about one minute with no rebuild:
   ```
   az containerapp ingress traffic set -g rg-incident-platform -n incident-dashboard --label-weight previous=100 production=0
   ```
   This restores the old **public** revision, so use it only briefly, or
   restrict ingress at the same time.

## 5. Least-privilege grants (prepared, not granted)

All grants live in `infra/activation-grants.bicep`, are off by default, and
are applied manually, one flag at a time, after approval. Run the `what-if`
first; it must show exactly one `Create` per flag.

```
az deployment group what-if -g rg-incident-platform -f infra/activation-grants.bicep \
  -p cosmosAccountName=incident-cosmos-4pqaac2c4e4hs keyVaultName=incidentkv4pqaac2c4e4hs <flag>=true
```

| Flag | Identity (principal ID) | Role | Exact scope | Needed for |
|---|---|---|---|---|
| `grantDashboardSigninTestSecret` | `incident-dashboard-id` (`ececbe0e-9f93-47a6-8e4b-a56432b7c31d`) | Key Vault Secrets User `4633458b-…` | secret `dashboard-auth-signin-test` **only** | Sign-in test (§4) |
| `grantNotificationSubscriptionsReader` | `incident-notification-id` (`57b6aafd-8ad9-45b4-ad05-df00428f4843`) | Cosmos DB Built-in Data Reader `…0001` | `/dbs/incidents/colls/subscriptions` | Rechecking already-queued messages (`NOTIFICATION_ELIGIBILITY=enforce`) |
| `grantSubscriptionWebhookSecret` | `incident-subscription-id` (`fadc85b4-…`) | Key Vault Secrets User | secret `telegram-webhook-secret` | Webhook verification |

A Key Vault grant must be deployed **after** its secret exists. Applying
`grantDashboardSigninTestSecret` before Liu creates `dashboard-auth-signin-test`
fails, which enforces that order. Removing a grant means deleting that role
assignment; incremental CD never removes it.

### 5a. Decision: dashboard test-secret grant (Option a, 19 October 2026)

The platform owner approved preparing a test-only grant. The earlier
`grantDashboardAuthSecrets` flag, which covered both dashboard secrets, has
been **replaced** by `grantDashboardSigninTestSecret`.

**Prepared and validated locally. Not applied.**

| Property | Value |
|---|---|
| Identity | `incident-dashboard-id` only |
| Role | Key Vault Secrets User (read secret values; no list, write or vault-level rights) |
| Scope | `…/vaults/incidentkv4pqaac2c4e4hs/secrets/dashboard-auth-signin-test` |
| Assignment name (deterministic) | `4a6f9e7f-301b-5c67-a35a-107e753d7e73` |
| Production secret | Neither referenced, granted nor required. Its grant is a separate change at promotion. |
| Existing permissions | Unchanged: the `what-if` with defaults shows no change; with this flag, exactly one `Create` |

**Validation (19 October 2026):**
- `az bicep build` passes for every `infra/*.bicep` file.
- Read-only `what-if`: defaults give no change; with the flag set, a single
  `Create` with the identity, role and scope above.
- `tests/workflow_security/test_activation_grants.py` (4 tests) pins that no
  workflow deploys the template, that every flag is off, and that this grant is
  the one test secret for the dashboard identity. It also pins that no grant is
  scoped to a whole vault or account. In-memory mutations (vault-wide scope,
  production secret, default on, wrong identity) were each caught.

**Apply, later, after §7 steps 0–3 and a separate approval.** Run the `what-if`
first and confirm the single `Create`, then:

```
az deployment group create -g rg-incident-platform -f infra/activation-grants.bicep \
  -p cosmosAccountName=incident-cosmos-4pqaac2c4e4hs keyVaultName=incidentkv4pqaac2c4e4hs grantDashboardSigninTestSecret=true
```

**Roll back.** This removes only this assignment:

```
az role assignment delete --assignee ececbe0e-9f93-47a6-8e4b-a56432b7c31d \
  --role "Key Vault Secrets User" \
  --scope "$(az keyvault show -n incidentkv4pqaac2c4e4hs --query id -o tsv)/secrets/dashboard-auth-signin-test"
```

Verify with `az role assignment list --scope <same scope>`: it should return
no assignment for that identity. After a rollback:
- the `signin-test` revision can no longer read its configuration and stops
  becoming healthy;
- production is unaffected, because no production traffic goes to that
  revision and the live dashboard reads no secret;
- no other permission changes.

Separately recommended: the dashboard identity currently holds account-wide
Cosmos read, which includes the access partition. Narrowing it to the
`incidents` container is a team decision (it touches `core.bicep`).

## 6. Behaviour on merge (verified locally)

| Component | On merge | Evidence |
|---|---|---|
| Dashboard | **Not redeployed** (`keep`) | `test_dashboard_release.py`; freeze tests |
| Subscription API | Redeployed; new routes return 404, and legacy responses are identical | 134 tests |
| Notification Service | Redeployed with `azure-cosmos`; recheck `off` | 92 tests; CI image check |
| Worker | **Not redeployed** (`activate_routing: false`); its owner check reaches production only in step 7 | cd.yml |
| Ingestion | Unchanged (patch not applied) | 71 tests |
| Azure permissions | None (`activation-grants.bicep` is not referenced by any workflow) | grep and `what-if` |

## 7. Deployment sequence

"You" = the platform owner's approval or action; "Team" = coordination needed.

| # | Step | Who | Depends on | Verify | Roll back |
|---|---|---|---|---|---|
| 0 | **Liu Yiting confirms she can open `incident-platform-api` in the portal and approves §3.1's initial scope.** Nothing below starts before this. | **Team**: Liu Yiting | — | Her written confirmation | — |
| 1 | Liu applies §3.1 items 1, 3 and 4: test redirect URI only, `Dashboard.Read` role, assignment to the platform owner | **Team**: Liu Yiting | 0 | One Web redirect URI (test); role assigned | Per §3.1 "Undo" |
| 2 | Liu applies §3.1 item 2: client secret written only into `dashboard-auth-signin-test` (§3c) | **Team**: Liu Yiting (sole holder of the value) | 1 | Secret name listed (value never read) | Delete the client secret; disable the Key Vault secret |
| 3 | Review and merge the PRs (backend + guard; dashboard; Grafana as its own PR) | **Team** (second approver) | — | CD green; dashboard revision unchanged | Revert the merge |
| 4 | Apply `grantDashboardSigninTestSecret` (§5a). Option (a) is decided and the template is prepared; **applying it is a separate approval**. | **You** (permission) | 2, 3 | what-if shows exactly one Create, on the test secret | §5a roll back |
| 5 | CD run `dashboard_release=signin-test` | **You** | 4 | §4 step 3 | Nothing to undo: 0% traffic |
| 6 | Human sign-in test (§4) | **You** | 5 | §4 table | — |
| 7 | **Deferred.** Liu adds the production redirect URI and writes `dashboard-auth-production`; then CD `promote` | **You** + **Team**: Liu Yiting (separate approval) | 6 evidence | Production asks for sign-in; Admin signs in | §4 step 7 |
| 8 | **Deferred.** Self-pre-authorization (§3.1 deferred table), tested alone on the 0% revision with the API scope added to the test secret; if it fails, Option C (§3.2) | **Team**: Liu Yiting + **You** (separate approval) | 6 | Sign-in succeeds; access token has `scp=user_impersonation`, `appid=fbbe63e1-…` | Remove the authorized client; remove the scope from the secret |
| 9 | CD `activate_routing` run (Worker with owner checks) | **You** + Team (routing reference) | 3 | Worker revision ready | `rollback_worker_image` |
| 10 | Wiring PR: Subscription API and dashboard central-access settings, flags on | **You** approve | 7, 8 | `/api/v1/access/me` 200 for Admin, 403 for others | Flags off |
| 11 | Register team members (each with an identity receipt) | **You** | 10 | Their role and tenants shown | Disable or revoke in the Admin page |
| 12 | Grant `grantNotificationSubscriptionsReader`; set `NOTIFICATION_ELIGIBILITY=enforce` | **You** (permission) | 9 | Notification revision ready | `off`; delete the assignment |
| 13 | Telegram: bot username, `telegram-webhook-secret`, grant, settings, `setWebhook` | **Team**: bot owner + **You** | 10, 12 | `getWebhookInfo` shows the URL; a private `/start` verifies ownership | `deleteWebhook`; flags off |
| 14 | Destination approvals from the Admin review | **You** / operator | 13 | Status shows `destination_approved` | Disable the record |
| 15 | Apply simulator v2 patch; set `ENABLE_EVENT_SIMULATOR`, `SIMULATION_CLIENT_IDS`, `SUBSCRIPTION_API_ORIGIN` | **You** + Team (ingestion owner) | 10 | Capabilities = `entra-user-v1` | Flag off |
| 16 | One controlled end-to-end test with a real Telegram message | **You** (explicit approval) | all | Delivered only to the verified subscriber | Unsubscribe |

Steps 10, 12 and 13 still need a small wiring PR: Container App settings and
CD inputs for the Subscription API, Notification and dashboard flags. **That
PR is not prepared yet**; it is deliberately left until steps 1–7 succeed.

## 8. Telegram configuration (step 13)

- **Bot username.** From the bot owner (BotFather). Not secret: `TELEGRAM_BOT_USERNAME`.
- **Webhook secret.** Generate locally, 48+ characters from `A–Z a–z 0–9 _ -`.
  Store only as Key Vault secret `telegram-webhook-secret` and reference it as a
  Container Apps secret. Never put it in a plain setting, a commit or a chat.
- **Registration.** Done by the bot owner with the bot token, never in CI:
  ```
  setWebhook  url=https://incident-subscription.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/api/v1/telegram/webhook
              secret_token=<telegram-webhook-secret>  allowed_updates=["message"]  drop_pending_updates=true
  ```
  Nothing polls this bot today, so existing alert sending is unaffected.
  Undo with `deleteWebhook`.
- **Rate limits** (implemented and tested; single replica):

  | What | Limit | Over the limit |
  |---|---|---|
  | Link requests per user | 5 per 15 minutes | 429 |
  | Updates per Telegram sender | 10 per minute | Acknowledged and dropped |
  | All webhook updates | 120 per minute | 429 (Telegram retries) |
  | Unauthenticated calls | 30 per minute | 429 |

  Bodies are capped at 64 KB.

## 9. Destination approval (unchanged separation)

1. A verified team member appears under **Telegram destinations awaiting
   approval** on the Admin page. Loading the list is audited; if the audit
   cannot be written, the list is not shown.
2. The Platform Admin confirms that the person belongs to the tenant.
3. An operator writes the displayed record into `approved-destinations`
   through the existing manual process.

The Subscription API identity still has only **read** access to that
container, so the service that writes subscriptions still cannot approve its
own destinations.

## 10. Simulator patch and the ingestion HTML demo page

Patch: `simulation-authorization.v2.proposed.patch` (5 files, +306/−3, applies
cleanly). The v1 file is superseded.

| Environment | `/api/v1/simulate` | Demo page at `/` |
|---|---|---|
| Local `docker-compose` (auth off) | Open for TENANT-01, **as today** | Works as today |
| Azure, default | **404** | Buttons show "Simulation is available only to authorized operators through the Business Dashboard"; the in-memory list stays empty |
| Azure, enabled | Delegated user, approved client, fresh grant with `Incidents.Simulate` for the requested tenant | Same message (the page has no token) |

CD's checks, DAST (which already excludes this path) and the load simulator do
not depend on the anonymous endpoint.

## 11. Dependencies that block a dashboard-only release

Sign-in needs steps 1, 2, 4 and 5–7. Admin Access Management needs 8 and 10.
Account Setup (Telegram) needs 9, 10, 12 and 13. The simulator page needs 15.
Overview, incidents, evidence and Grafana need only the existing identities.

## 12. Cost

No new Azure resource is created.
- The extra 0%-traffic revision scales to zero.
- Registry reads use the existing free-tier 1000 RU/s database.
- Key Vault operations and webhook calls fall within free allowances.

Estimate: **US$0–1 per month**. The risk is Cosmos throttling under heavy load,
not cost.

## 13. Rollback summary

| What | How |
|---|---|
| Dashboard | Traffic back to label `previous` (§4) |
| Features | Flags off; routes return 404 |
| Telegram | `deleteWebhook` |
| Grants | Delete the specific role assignment |
| Worker | `rollback_worker_image` |
| Records | Access and Telegram records are additive and inert; never delete audits |
