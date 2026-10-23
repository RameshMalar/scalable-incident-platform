# Telegram subscription lifecycle: backend integration report

Date: 19 October 2026. Checkout: `scalable-incident-platform-grafana`, branch
`feature/grafana-observability`. All changes are uncommitted. Nothing was
deployed or built, no Azure setting or permission was changed, and no live
incident or Telegram message was produced.

This report supersedes the *Telegram lifecycle* and *backend integration
contract* sections of `CONSOLIDATED_IMPLEMENTATION_REPORT.md` for the
Subscription API, Worker and Notification Service. That report's dashboard,
deployment and rollback sections still apply.

**Status.** The central-access and Telegram self-service backend is installed
in the shared services. It is covered by automated integration tests and is
**off by default**. It is **not yet deployable as a live feature**: see
[Remaining blockers](#remaining-blockers).

## What changed

| Service | Change |
|---|---|
| Subscription API | New delegated-user token verifier (`user_auth.py`), separate from the client `TENANT_MAP`. Central access registry (`access_registry.py`), Telegram self-service (`telegram_self_service.py`) and routes (`account_routes.py`), implementing the contract the dashboard already calls. All routes return 404 unless `ENABLE_CENTRAL_ACCESS` / `ENABLE_TELEGRAM_SELF_SERVICE` are set. |
| Subscription API: storage | Access grants, audits, identity receipts, Telegram challenges and bindings are kept in a reserved `__platform_access__` partition of the **existing** subscriptions container. That container's identity already has write access, so no new container or role is needed. `TENANT_MAP` now refuses reserved partitions. |
| Subscription API: legacy contract | Unchanged responses. The new `owner_object_id` is hidden from legacy routes. Legacy clients cannot PATCH a person's subscription (403) but can still delete it. |
| Worker | `subscription_router` skips an account-owned subscription unless the owner's grant is active, still assigns the tenant, and the verified binding matches the chat (`owner_eligibility.py`). Lookup failures raise into existing retries. Legacy subscriptions are untouched. |
| Notification Service | Optional pre-send recheck (`delivery_eligibility.py`, `NOTIFICATION_ELIGIBILITY=enforce`, default `off`). Re-reads the subscription and owner state before each routed send, including retries. Ineligible messages complete unsent; outages abandon for retry, then go to the dead-letter queue. Adds `azure-cosmos==4.16.4`. |
| Shared role model | `subscription-service/app/access_roles.py` is a byte-identical copy of the dashboard's. A test fails if they diverge. `/access/me` returns server-computed `permissions`. |

**Unchanged:** the ingestion service (simulator authorization patch **not
applied**; see blockers), the dashboard, Grafana, infra/Bicep, CI/CD workflows,
and `access-policy.json` (SHA-256 `7bf104a7…09b`, verified).

## Lifecycle integration test

`tests/local_integration/test_telegram_lifecycle.py` drives the real
Subscription API (HTTP), the Worker persist-then-route handler, and the
Notification processor together. Entra tokens are real RS256 JWTs checked by
the production verifier against a test key. Telegram delivery is `MockNotifier`,
and the bot webhook is called as Telegram would call it. A socket guard fails
any non-loopback connection or DNS lookup. All identities are synthetic, except
the already-approved bootstrap Admin ID, which the registry logic requires.

| Required step | How it is tested |
|---|---|
| Authorized user signs in through Entra | Admin resolves via `/access/me`. An unapproved but signed-in user gets 403 everywhere. A token without the delegated scope is refused. Alice is approved only after her own identity receipt. |
| User opts in | Opting in before ownership is verified → 403, and no subscription is written. |
| Telegram ownership via bot linking | One-time deep link. A group chat, a missing secret header and replay from another account are rejected. A private `/start` binds the account. Linking creates no subscription and sends nothing. |
| Activation for approved tenants | Rejected until the operator approves the destination, then enabled. Another tenant → 403. No chat ID is ever returned. |
| Eligible test incident through the pipeline | An `e2e_test` incident is persisted, routed, enveloped and sent through the real code. Unselected event types and `load_test` are not routed. |
| Only the verified, active, authorized subscriber | Delivered to Alice plus the pre-existing legacy route only. Not to Bob (verified and opted in, then **disabled**), nor to Carol (other tenant). |
| Unsubscription stops notifications | A message queued **before** the unsubscribe is not sent. No new routes. Unsubscribing is idempotent and dashboard access is unaffected. |
| Revoking user or tenant access | User revoked: the queued message is dropped, routing stops, and account routes return 403 although her token is still valid. Tenant removed from Carol: her queued TENANT-02 message is dropped and no new routes are created. |
| Previously queued messages during an outage | Registry unreadable: the Worker raises (retry). The consumer **abandons** the queued message without sending. Once recovered, the redelivery completes and sends. |

**Mutation check.** Tests were re-run with each safeguard disabled through a
throwaway plugin; source files were not changed. Disabling the Worker owner
check delivers to the disabled user. Disabling the notifier recheck delivers a
message queued before unsubscribing. Both runs fail, so the tests do exercise
the safeguards.

## Test results

Python 3.12.10 in a fresh virtualenv installed exactly from each service's
pinned `requirements-dev.txt`, as CI does (PyJWT 2.15.0). The dashboard used its
existing pinned Python 3.12 environment. Pytest was run with the cache disabled.

| Suite | Before | Now | New tests |
|---|---:|---:|---:|
| Ingestion | 71 | 71 | 0 |
| Subscription API | 98 | 126 | 28 (`test_account_routes.py`) |
| Notification Service | 80 | 92 | 12 (`test_delivery_eligibility.py`) |
| Worker | 166 | 179 | 13 (`test_owner_eligibility.py`) |
| Load simulator | 86 | 86 | 0 |
| Common event model | 22 | 22 | 0 |
| Producer SDK | 54 | 54 | 0 |
| Dashboard and domain | 78 | 78 | 0 |
| **Service and dashboard total** | **655** | **708** | **53** |
| Local integration (`tests/local_integration`) | 74 | 89 | 15 (`test_telegram_lifecycle.py`) |
| Workflow security (`tests/workflow_security`) | 196 | 196 | 0 |
| **Everything** | **925** | **993** | **68** |

All pass; there are no failures or skips. The one warning is the existing
FastAPI/Starlette `httpx` deprecation.

**Note on the earlier 655.** It reproduces only with the pinned dependencies.
The shared `.venv` in the sibling checkout lacks `common_event_model`, so 4
suites fail to collect. It also has PyJWT 2.14.0, which fails the
deep-nested-JWT test (CVE-2026-101918). Use a CI-equivalent environment for
evidence. The separate 90-test simulator-patch suite was not re-run, because
the patch was not applied.

These tests do **not** establish: real Entra sign-in and consent, Cosmos
transactional batches against a live account, Telegram webhook delivery,
Linux image builds, or behaviour under concurrent load.

## Remaining blockers

1. **Notification send-time recheck needs a permission.** The Notification
   identity has no Cosmos access. `enforce` needs Cosmos DB Built-in Data
   Reader on the subscriptions container. This is a role assignment requiring
   approval. Until then, the Worker check is the only gate, and messages
   already queued are not rechecked.
2. **Simulator authorization (requirement 7) is not applied.** Production
   `/api/v1/simulate` is still anonymous and pages Telegram. The reviewed patch
   still applies cleanly. It would also disable the buttons on ingestion's
   built-in demo page. Because merges to `main` deploy automatically, this needs
   explicit approval. The patch should be adapted to check `permissions` from
   `/access/me` rather than role names.
3. **Entra configuration.** This needs a dashboard login client, a delegated
   scope (`user_impersonation` or the configured name) on the existing API, and
   consent. `USER_AUTH_CLIENT_IDS` must be set.
4. **Telegram configuration.** The bot username, webhook secret (as a Container
   Apps secret) and a manual `setWebhook` call by the bot owner are required.
   Webhook rate limiting is not implemented; ingress limits or a
   per-sender cap are advisable.
5. **Operator destination approval is still required.** Even after ownership
   is verified, a user cannot enable alerts until an operator adds their chat
   to `approved-destinations`. This is deliberate, because it preserves the
   existing policy. An alternative is to accept bot-verified ownership as
   approval, which is a product decision.
6. **In-flight sends.** A pre-send check cannot recall a message whose send
   has already started when the unsubscribe or revoke lands.

## Deployment readiness

**Safe to merge as-is:** every new route returns 404, the Worker check only
affects owner-marked subscriptions (none exist), and the notifier recheck is
off. Behaviour stays identical to today. The Notification image gains
`azure-cosmos`, and CI builds will verify that.

**To activate**, after approvals and in this order:
1. Configure Entra (blocker 3) and the Subscription API settings documented in
   `subscription-service/API.md`.
2. Enable central access, then the dashboard's `ENABLE_CENTRAL_ACCESS`.
3. Grant the Notification reader role and set `NOTIFICATION_ELIGIBILITY=enforce`.
4. Configure Telegram (blocker 4) and enable self-service and the dashboard's
   `ENABLE_TELEGRAM_ONBOARDING`.
5. Run one approved, controlled end-to-end test.

Rollback: unset the flags. Access and Telegram records remain in the reserved
partition and are inert.
