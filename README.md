# Scalable Incident Platform

A reusable, cloud-native platform that receives standardized incident events
from AI video analytics producers, processes them, and delivers them to
downstream consumers. Intrusion Detection is the seed producer used to validate
the architecture.

Sprint 1 delivers the Common Event Model and the Incident Ingestion Service.
Sprint 2 adds the Processing Worker and scales it on queue depth. Sprint 3 adds
authentication and tenant isolation, the business dashboard, and the load
simulator the scalability claims are measured with.

## Repository layout

| Path | Contents |
| --- | --- |
| `packages/common-event-model/` | The Common Event Model as an installable library, and its tests. Every service that reads or writes an incident installs it |
| `packages/producer-sdk/` | The Producer SDK: builds, validates, authenticates and delivers incidents. What a new analytics pipeline installs |
| `ingestion-service/` | Incident Ingestion Service (FastAPI) and its tests |
| `worker-service/` | Processing Worker: consumes `incident-events`, and its tests |
| `business-dashboard/` | Streamlit dashboard reading incidents straight out of Cosmos DB |
| `load-simulator/` | Load generator for the scalability run, and its tests |
| `infra/` | Azure infrastructure as Bicep: `core.bicep` (shared), then `ingestion.bicep`, `worker.bicep`, `dashboard.bicep`, `simulator.bicep` per component, and `workbook.bicep` for resource health |
| `scripts/` | One-off setup scripts |
| `.github/workflows/` | CI, CD and static analysis pipelines |

## Local development

Docker Compose is for local development only. The cloud runtime is Azure
Container Apps — see [Deploying to Azure](#deploying-to-azure).

Run the service directly. **Install from the repository root**, because
`requirements.txt` installs the Common Event Model by path and pip resolves a
path requirement against the working directory, not against the file it was read
from:

```bash
pip install -r ingestion-service/requirements-dev.txt

cd ingestion-service
uvicorn app.main:app --reload
```

Or through Compose:

```bash
docker compose up --build
```

Either way the service is at http://localhost:8000 — dashboard at `/`, Swagger
at `/docs`, liveness at `/health`, readiness at `/ready`.

If your machine needs Compose settings nobody else does — a pinned subnet,
because the default bridge pool collides with an existing route, is the one that
has come up — put them in `docker-compose.override.yml`. Compose merges that file
automatically, so no command changes, and it is gitignored, so a host-specific
workaround cannot break a teammate whose network differs.

The two probes answer different questions on purpose. `/health` asks whether the
process is serving and never touches Service Bus, because failing it restarts
the container and a broker blip should not do that. `/ready` asks whether this
replica can actually publish, by having the credential mint a Service Bus token,
and answers 503 until it can.

By default the service publishes to an in-memory mock rather than to Azure, so
it runs with no cloud account and no credentials. `/health` reports which one is
active: `"publisher": "mock"` locally, `"publisher": "servicebus"` when it is
really publishing. Point it at the real namespace with:

```bash
az login
export EVENT_PUBLISHER=servicebus
export SERVICE_BUS_FQDN="$(az servicebus namespace list -g rg-incident-platform \
  --query "[?starts_with(name, 'incident-bus')].serviceBusEndpoint | [0]" -o tsv \
  | sed -E 's~https://~~; s~:443/?$~~')"
uvicorn app.main:app --reload
```

Your `az login` user needs **Azure Service Bus Data Sender** on the namespace —
see [Verifying the queue](#verifying-the-queue-without-the-producer-or-worker)
for why a management role is not enough.

Run the tests:

```bash
cd ingestion-service
python -m pytest tests -v
```

Generate a sample incident without a real camera:

```bash
curl -X POST http://localhost:8000/api/v1/simulate/intrusion
```

### Running the worker locally

The worker has no mock mode: a consumer with nothing to consume from is not a
consumer. It needs a real queue, so point it at the deployed namespace. Your
`az login` user needs **Azure Service Bus Data Receiver** on it.

```bash
cd worker-service
pip install -r requirements-dev.txt
export SERVICE_BUS_FQDN="$(az servicebus namespace list -g rg-incident-platform \
  --query "[?starts_with(name, 'incident-bus')].serviceBusEndpoint | [0]" -o tsv \
  | sed -E 's~https://~~; s~:443/?$~~')"
python -m app.main
```

It logs one line per incident and exits cleanly on Ctrl-C, draining whatever it
already holds. Run it alongside the ingestion service and every
`POST /api/v1/simulate/intrusion` should appear in its output within a second.

Its tests need no queue and no credentials:

```bash
cd worker-service
python -m pytest tests -v
```

## Deploying to Azure

Deployment is automatic: **every push to `main` builds, deploys and verifies
itself.** There is nothing to run by hand and no server to log into.

The pipeline authenticates with OpenID Connect against a user-assigned managed
identity, so the repository holds no Azure credentials. Images are tagged with
the commit SHA, which keeps every running revision traceable to a commit.

### One-time setup

Needed once per Azure subscription, by someone with Owner on the resource
group.

```bash
az login
RESOURCE_GROUP=<your-resource-group> \
GITHUB_REPO=<owner>/<repo> \
  ./scripts/setup-github-oidc.sh
```

The script creates the deploy identity, grants it the two roles it needs, and
registers the federated credentials that let GitHub Actions exchange a workflow
token for an Azure token. It needs the GitHub CLI on PATH: GitHub signs its
tokens with immutable subject claims, so the credentials must carry the
repository's numeric ids rather than its name. A credential written with plain
names simply never matches, and the failure appears as `AADSTS700213` at
sign-in.

One credential is registered per trusted context -- the deploy branch, pull
requests, and the `production` environment. The deploy job names an
environment, which gives it an environment-scoped subject rather than a
branch-scoped one; without that third credential the deployment fails to
authenticate even though the branch credential looks correct. It prints four values to add under
**Settings → Secrets and variables → Actions → Variables**:

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_RESOURCE_GROUP`. Optionally set `AZURE_LOCATION` to override the default
of `malaysiawest`.

These are identifiers rather than credentials, which is why they are variables
and not secrets.

### Deploying by hand

Only needed when working on the infrastructure itself:

```bash
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file infra/core.bicep
```

The templates are idempotent — re-running them reconciles drift rather than
creating duplicates. `core.bicep` provisions the shared platform: a container
registry, a Log Analytics workspace, a Container Apps environment, the Service
Bus namespace and queue, and the identities. `ingestion.bicep`, `worker.bicep`
and `dashboard.bicep` each deploy one service on top of it and all three
require an `image` parameter, so in practice they are deployed by CD, which
has just built one.

### Region

Deployed to `malaysiawest`, the closest allowed region to Singapore.

Azure for Students subscriptions carry an `Allowed resource deployment
regions` policy. Only `malaysiawest`, `indonesiacentral`, `eastasia`,
`japaneast` and `koreacentral` are permitted -- `southeastasia` is rejected
with `RequestDisallowedByAzure`, which reads like a quota problem but is not.

Every resource, including the load simulator used for scalability testing,
must live in the same region. A simulator in one region driving a platform in
another adds inter-region latency to every measurement and makes the
throughput and latency results meaningless. The resource group's own location
is metadata only and does not constrain this.

One known gap: ACR Tasks (`az acr build`) is unavailable in `malaysiawest`, so
images are built on the runner and pushed, rather than built by the registry.

### Tearing down and rebuilding

An idle deployment costs about five dollars a month, all of it the container
registry -- the service scales to zero and Log Analytics bills by ingestion.
To stop that entirely between milestones:

```bash
./scripts/teardown.sh
```

Rebuilding is one pipeline run (`gh workflow run cd.yml`). That the whole
environment can be destroyed and reproduced from source is the point of
keeping it in Bicep, and is worth demonstrating rather than just claiming.

### Developer access

Everyone working on the platform holds `Contributor` on the resource group,
which covers the whole management plane. It does not cover the data planes:
`Contributor` lists no `dataActions`, and Cosmos runs a separate RBAC system of
its own. That is why a role such as Owner still cannot read a queue, a secret, a
document, or a blob.

The data-plane roles people need are declared in `core.bicep` and applied by CD,
from the comma-separated object ids in the `DEVELOPER_PRINCIPAL_IDS` repository
variable: **Cosmos DB Built-in Data Contributor** to read and write incidents,
**Key Vault Secrets Officer** because the Telegram bot token is put in by hand
rather than by CD, and **Storage Blob Data Contributor** on the evidence
container. Granting them by hand works too, but the declared form is reviewable
in a diff, needs nobody to hold `Role Based Access Control Administrator`
personally, and survives `teardown.sh` -- which removes resources by name
prefix, taking hand-made assignments with them.

Two things are not Azure roles and so are not in that list. Calling the incident
API needs an `Incidents.Write` app role in Entra, which
[the operator setup](#letting-a-person-call-the-api) grants from the same
variable. And the storage role, though assigned, cannot currently be used at all
— see [uploading evidence images](#uploading-evidence-images).

Withholding these roles would not be a safety boundary in any case, since
`Contributor` already allows deleting the accounts they apply to. The boundary
that does mean something is `Role Based Access Control Administrator`, the one
permission `Contributor` explicitly excludes and the only one that lets a
principal grant itself anything further. The deploy identity holds it so that no
person has to.

## The Common Event Model

One incident shape for every analytics type, in
[`packages/common-event-model/`](packages/common-event-model). Each service that
reads or writes an incident installs it:

```
./packages/common-event-model
```

It was three byte-identical copies of `app/models.py` until the pipeline could
install a library, kept in step by a CI job that diffed them. The copies were not
an oversight — a Docker build can only copy from inside its own context, and each
service built from its own directory, so a shared package meant changing the
build before the model. That trade is now made the other way: the three services
that speak the model build from the **repository root**, with
`-f <service>/Dockerfile`, and the CI job that policed drift runs the library's
own tests instead.

`business-dashboard/` and `load-simulator/` keep the smaller context, because
neither installs the model. The dashboard reads stored incidents as documents.
The load simulator is the interesting one: `app/contract.py` implements the
contract **independently, on purpose**. It stands in for a third-party producer,
and a producer that built payloads with the same module the API validates them
with could never produce the mistakes a real external producer makes.

## The Producer SDK

[`packages/producer-sdk/`](packages/producer-sdk) is what a new analytics
pipeline installs. It builds a validated incident, obtains a token, delivers it
to `/api/v1/incidents` with a retry policy that is safe because the store keys
on the producer's `event_id`, and — with the optional `evidence` extra —
uploads an image and returns the `evidence_uri` to put on the incident.

```bash
pip install ./packages/common-event-model ./packages/producer-sdk
```

A third-party producer is given a URL and an audience. `Deployment.discover()`
is for our own scripts and is how
`scripts/demo-new-analytics-type.py` finds the deployed API rather than
hard-coding it. The SDK does not talk to Cosmos, Service Bus or Telegram: a
producer writes incidents through the API and never reads them back.

`analytics_type` is not enumerated here either. A new pipeline must not need a
release of this library any more than it needs a release of the Common Event
Model.

Two consequences worth knowing before working on this:

- **Install from the repository root.** `pip install -r <service>/requirements.txt`
  works; `cd <service> && pip install -r requirements.txt` fails, because pip
  resolves a path requirement against the working directory rather than against
  the file it was read from. CI installs from the root for the same reason, and
  so does `pip-audit`.
- **`.dockerignore` is load-bearing now, not tidiness.** With the root as context
  the whole tree is sent to the Docker daemon, and Docker does not read
  `.gitignore` — a local `.venv` alone is around 96 MB of the repository's 109 MB.

The library declares `pydantic` as a **range**; the services keep their exact
pins. A library that pins an exact version makes itself unresolvable against a
service that needs a different one.

## Messaging

Incidents move between services over an Azure Service Bus queue,
`incident-events`, on a Standard namespace. The ingestion service publishes to
it; the processing worker consumes from it. Neither holds a connection string:
`core.bicep` grants the ingestion identity **Service Bus Data Sender** and the
worker identity **Service Bus Data Receiver**, and local auth on the namespace
is switched off, so the only way in is a managed identity with an explicit role.

The ingestion service opens its queue senders at startup and reuses them for the
life of the process. Opening a client per message costs a TLS and AMQP handshake
on every request: harmless in a demo, and enough to dominate the throughput and
latency figures the platform is assessed on.

It keeps a small **pool** of them rather than one, because a sender is not safe
to use from two coroutines at once. An AMQP link is a state machine, and
concurrent sends interleave frames on it: the SDK detaches the link mid-send,
shuts the handler down, and every request after that fails against a
half-dismantled object. One sender is therefore held by one request at a time,
a request that finds them all busy waits — which is back pressure, and the
autoscaler answers sustained concurrency with another replica — and a sender
whose send failed or was cancelled is discarded rather than handed to the next
caller, since a link that was part-way onto the wire is one nobody can reason
about.

This was not designed in. The service ran for weeks with a single shared sender
and every check passed, because every check published one incident at a time:
CD's queue-drain verification posts one, `verify-servicebus.py` posts one, and
the unit tests use a double. The first load run that offered five incidents a
second failed 94 of 100 requests, and the logs showed the link detaching and the
SDK failing inside itself with `'NoneType' object has no attribute
'create_sender_link'`. The concurrency tests in
`ingestion-service/tests/test_publisher.py` exist to keep it from coming back.

Transport is a switch rather than a constant. Native AMQP needs port 5671, which
campus and corporate firewalls routinely block, so the default everywhere is
AMQP over WebSocket on 443 and local development just works. The deployed
service sets `SERVICE_BUS_TRANSPORT=amqp` to take the faster native port, where
the egress is ours to control.

### Telegram notifications

After the worker persists an incident, it publishes the event to
`notification-events`. The Notification Service consumes that queue and calls
Telegram. Notification policy is explicit in the Common Event Model rather
than inferred from an id:

- `incident` is the backwards-compatible default and reaches Telegram.
- `load_test` exercises ingestion, Service Bus, and Cosmos without paging
  people.
- `e2e_test` follows the complete notification path at low volume and is
  labelled as a test in the Telegram message.

The worker applies the policy before publishing new notification messages. The
Notification Service repeats it as a defence in depth and, only for messages
created before `event_purpose` existed, recognises the old `EVT-LOAD-*`
convention so the current backlog can drain safely.

`evidence_uri` remains part of the event through ingestion, persistence, and
the notification queue; notification filtering never strips or rewrites it.
When the event carries evidence, the Notification Service downloads the blob
with its managed identity, which holds `Storage Blob Data Reader` on the
`evidence` container, and uploads the bytes through Telegram `sendPhoto`. The
container blocks public access and the account disables shared-key access, so
neither public containers nor long-lived SAS URLs are involved. Both `incident`
and `e2e_test` events exercise the same image-delivery path.

`evidence_uri` is producer-supplied and therefore untrusted. It is never
fetched as given: its host and container must match the configured account, and
only the remaining blob path is used to build the request, so a producer cannot
aim the service at a URL of its own choosing or at another container. Any query
string is dropped, because access is decided by the service's identity rather
than by a caller-supplied SAS token.

Evidence is best-effort. A missing blob, an oversized one, a non-image content
type, a reference that fails the checks above, or a `sendPhoto` rejection all
degrade to the text alert rather than dropping the notification. Captions are
truncated to Telegram's 1024-character limit, so the text-only path is the one
that carries a long description in full. Set `EVIDENCE_BLOB_ENDPOINT` to enable
image delivery; leaving it empty keeps the service on the text-only path.

#### Uploading evidence images

`allowSharedKeyAccess` is off on the account, so there is no connection string
or account key to upload with, and — as with Service Bus and Cosmos — a
management role such as Owner does not grant blob data access.

Use the **Evidence** workflow (`gh workflow run evidence.yml`), which uploads as
the deploy identity and then verifies that Telegram received the image rather
than the text fallback. It is the supported path, and not only for convenience:
a tenant Conditional Access policy refuses user accounts a token for
`storage.azure.com` regardless of the Azure roles they hold, so a person's
`Storage Blob Data Contributor` currently cannot be used at all. Conditional
Access governs authentication and does not apply to managed identities, so the
pipeline is unaffected. The role is still assigned to the developers listed in
`DEVELOPER_PRINCIPAL_IDS`, so uploading directly becomes possible if that policy
is ever relaxed or the work happens from a managed device:

```bash
ACCOUNT="$(az storage account list -g rg-incident-platform \
  --query "[?starts_with(name, 'incidentevi')].name | [0]" -o tsv)"

az storage blob upload --auth-mode login --account-name "$ACCOUNT" \
  --container-name evidence --name EVT-001.jpg --file ./EVT-001.jpg
```

The resulting `evidence_uri` is
`https://<account>.blob.core.windows.net/evidence/EVT-001.jpg`. A URI with any
other host or container is refused by the checks above, so evidence has to live
in this container to be delivered.

#### Announcing uploaded evidence to the platform

An image in the container is invisible until an incident references it. Nothing
scans the container: an incident reaches Cosmos — and therefore the dashboard's
Evidence page and the Telegram alerts — only by being posted to the ingestion
API. `scripts/upload-evidence-batch.py` does the first half, and
`scripts/publish-evidence-events.py` does the second, from the same manifest, so
the events and the blobs they name come from one description rather than two.

It refuses to publish anything unless every `evidence_uri` in the manifest
matches the blob endpoint and container the deployment is actually serving,
because an incident whose evidence fails that check is stored and alerted with an
image that can never load and nothing downstream says why. It reports rather than
sends unless `--publish` is given:

```bash
python scripts/publish-evidence-events.py                       # report only
python scripts/publish-evidence-events.py --limit 3 --publish   # send three
```

`--limit` is worth using, because **the store is idempotent and notifications are
not**. The worker keys each document on the producer's `event_id`, so
re-publishing creates no duplicate incident — but `should_notify` only holds back
`load_test`, and `PersistAndNotifyHandler` deliberately notifies on a duplicate
as well as a new incident so that a failed delivery gets another chance. Every
event carrying an `evidence_uri` therefore becomes a `sendPhoto`, and a second
run sends a second round of them. Publishing forty events means forty
photographs in the bot's chat, and nothing takes them back.

Authentication is the operator path below: the Azure CLI is pre-authorized on
the API and the caller holds `Incidents.Write` individually. `AADSTS65001` means
`scripts/grant-operator-access.sh` has not been run for that account.

Telegram delivery is intentionally limited to one Notification Service replica
and one message every 3.1 seconds. If Telegram returns HTTP 429, the notifier
honours the response's `retry_after` value and retries once before handing the
message back to Service Bus. The notification queue uses a five-minute message
lock so that wait and both HTTP attempts can finish before the broker redelivers
the event. This avoids the immediate redelivery loop that can otherwise turn
one rate limit into five failed deliveries and a dead-lettered message.

If the Notification Service has been manually stopped, deploy this filtering
and rate-limiting behaviour before starting it. Do not purge the queue merely
to make the count reach zero: explicitly classified load events and legacy
`EVT-LOAD-*` messages can now be completed without contacting Telegram, while
the dead-letter subqueue remains available for investigation. Check the counts,
start the app, and watch them drain with:

```bash
NS="$(az servicebus namespace list -g "$RESOURCE_GROUP" \
  --query '[0].name' -o tsv)"

az servicebus queue show -g "$RESOURCE_GROUP" --namespace-name "$NS" \
  -n notification-events --query countDetails -o json

az containerapp start -g "$RESOURCE_GROUP" -n incident-notification
```

A fresh simulated intrusion should be used for the delivery check only after
the active backlog is near zero. Historical dead-letter messages are not
replayed or deleted automatically.

A message the worker abandons five times, or one that outlives its time-to-live,
moves to the queue's built-in **dead-letter subqueue** rather than blocking the
queue or vanishing. The dead-letter path is not a separate resource — it is
`incident-events/$deadletterqueue`, read by opening a receiver on that subqueue.

### The consumer side

The worker revalidates every message against the Common Event Model rather than
trusting the producer, and settles each one in exactly one of three ways:

| Outcome | Settlement | Why |
| --- | --- | --- |
| Processed, or recognised as already processed | complete | The incident is done with |
| Malformed — not JSON, or not an incident | dead-letter immediately | No number of retries will make it valid, and retrying it occupies a delivery slot the rest of the queue could use |
| Anything else, such as an unavailable downstream | abandon | Transient; the broker redelivers, and dead-letters it itself after `maxDeliveryCount` |

Because delivery is at-least-once, the worker also dedupes on `event_id` — the
same id the publisher puts in the message's `message_id`. Today that memory is
per-replica and bounded, so it suppresses a redelivery to the same replica and
nothing more; a shared store makes it exact, and the natural place for it is the
incident store, where an idempotent upsert makes the dedupe check and the write
the same operation.

The dedupe entry is written *after* the handler succeeds, not before. Claiming
first would turn a crash midway through processing into a silently dropped
incident, because the redelivery would be mistaken for a duplicate. This way a
crash costs a reprocess, which is the cheaper of the two mistakes.

On SIGTERM — a scale-in, a new revision, a node move — the worker finishes
settling the batch it already holds before exiting. Messages dropped with their
locks held are not lost, but nothing else can have them until the lock expires.

### Scaling the worker

The worker takes no HTTP, so there is nothing to scale it on but the queue.
`worker.bicep` gives it a KEDA `azure-servicebus` rule: one replica per 20
messages of backlog, from zero up to ten. A queue that is empty costs nothing,
and a burst of incidents wakes replicas without anyone being paged.

That rule needs to read the queue's depth, and **Data Receiver does not carry
that right** — it can take messages off a queue but not ask how many are on it.
The only built-in role that can is **Service Bus Data Owner**, which also grants
send. Handing that to the worker identity would quietly give the worker's own
code the ability to publish incidents, so it goes to a third identity,
`incident-scaler-id`, scoped to the `incident-events` queue rather than to the
namespace. The autoscaler can manage exactly one queue; the worker still only
receives.

### Verifying the queue without the producer or worker

`scripts/verify-servicebus.py` exercises both paths this milestone owns —
*reaches the queue* and *reaches the dead-letter subqueue* — without either
service. It publishes a message, receives it back, deliberately dead-letters it,
and confirms it in the DLQ.

```bash
az login
pip install -r scripts/requirements.txt

# The resource group the platform is deployed in.
export RESOURCE_GROUP=rg-incident-platform

# The namespace host, read straight from whatever is deployed -- no deployment
# name to look up.
export SERVICE_BUS_FQDN="$(az servicebus namespace list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'incident-bus')].serviceBusEndpoint | [0]" -o tsv \
  | sed -E 's~https://~~; s~:443/?$~~')"

python scripts/verify-servicebus.py
```

If `SERVICE_BUS_FQDN` comes back empty the namespace is not deployed yet. This
branch deploys it via CD once merged to `main`; to try it before then, deploy
core by hand:

```bash
az deployment group create -g "$RESOURCE_GROUP" --template-file infra/core.bicep
```

The script authenticates as your `az login` user. Local auth is switched off on
the namespace, so that user needs the **data-plane** roles explicitly —
management roles such as Owner do not grant message send or receive:

```bash
NS="$(az servicebus namespace list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'incident-bus')].id | [0]" -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv)"
az role assignment create --assignee "$ME" --scope "$NS" --role "Azure Service Bus Data Sender"
az role assignment create --assignee "$ME" --scope "$NS" --role "Azure Service Bus Data Receiver"
```

## Authentication and tenant isolation

The ingress is public, so the API requires an Entra access token carrying
`Incidents.Write`. Producers are services and authenticate with client
credentials against their own app registration; the role is granted as an
application role on the platform's API registration.

Authentication is only half of it. `tenant_id` is an ordinary field of the
Common Event Model, so an authenticated producer for one customer could
otherwise submit incidents attributed to another. The token's client id
identifies the producer, `TENANT_MAP` gives the one tenant that producer may
speak for, and a mismatch is refused with 403. That map is the tenant registry
the Subscription API takes over; the shape is the same either way.

Local development runs with `AUTH_MODE` unset and authentication switched off,
so the service needs no directory and no token. The deployed service must never
do that, and two things stop it: `/health` reports the active mode and CD fails
on anything but `entra`, and CD then posts an unauthenticated incident and
requires a 401 rather than trusting the field.

### One-time Entra setup

Needed once per directory, by someone who can register applications.

| Registration | Purpose |
| --- | --- |
| `incident-platform-api` | The API. Exposes `api://<appId>` as its Application ID URI and defines the `Incidents.Write` application role |
| One per producer | Each analytics producer. Granted `Incidents.Write`, and mapped to its tenant in `TENANT_MAP` |

Two repository variables carry the result, both identifiers rather than secrets:
`AUTH_AUDIENCE` (the `api://<appId>` URI) and `TENANT_MAP`
(`<producer appId>:TENANT-01,...`). `AUTH_TENANT_ID` defaults to the
subscription's own tenant.

The deploy identity needs `Incidents.Write` and an entry in `TENANT_MAP` too:
CD's queue-drain check posts a real incident, and it now goes through the
authenticated path like any other producer rather than round a back door.

#### Letting a person call the API

`Incidents.Write` was defined for applications, so for a while no human could
obtain a token carrying it — no amount of Azure RBAC helps, because Azure roles
govern resources rather than this API. `scripts/grant-operator-access.sh` sets up
the four things that change that, and is safe to re-run:

```bash
./scripts/grant-operator-access.sh
```

It exposes a `user_impersonation` delegated scope and pre-authorizes the Azure
CLI, which is only what lets a token be issued at all; makes `Incidents.Write`
assignable to users and assigns it to everyone in `DEVELOPER_PRINCIPAL_IDS`,
which is what the API actually checks; and turns on `appRoleAssignmentRequired`
so Entra issues tokens only to assigned principals.

That last part matters. `TENANT_MAP` normally refuses any client it does not
know, but this works by registering the Azure CLI — a client every account in
the directory shares — so that particular refusal no longer applies. Two things
replace it: a token needs the app role, and Entra will not issue one to an
unassigned account at all.

Because operators share the CLI's client id they also share one tenant,
`TENANT-DEV`, which keeps hand-posted incidents out of the tenants the isolation
demonstration uses. Submitting any other `tenant_id` is refused with 403, which
is the isolation rule working rather than a fault.

## The incident store

Incidents are persisted in Cosmos DB, NoSQL API, partitioned on `tenant_id` --
which is how they are read back, since a customer looks at their own sites. The
choice is recorded in AD-07: the free tier carries 1,000 RU/s and 25 GB at no
charge and does not expire, where the alternative, a PostgreSQL Flexible Server
B1ms, costs about $21 a month and, being always on, would be the only component
here that cannot scale to zero.

The account is provisioned with `enableFreeTier: true` and a
`totalThroughputLimit` of 1,000 RU/s, so provisioned throughput cannot be raised
past the free grant by accident. An overrun throttles rather than bills, which
matters on a subscription whose spending limit disables everything once the
credit is gone. `disableLocalAuth: true` matches the Service Bus posture: no
keys, so the only way in is a managed identity holding a data-plane role. The
worker holds **Data Contributor** (read and write); the business dashboard
holds **Data Reader** on an identity of its own, so a bug in the dashboard can
never corrupt the store. Ingestion has neither -- it publishes to the queue and
never touches the store.

**A measured number for planning the scalability run:** one incident write costs
**9.9 RU**, so the free grant sustains roughly **101 writes per second**. A
rejected duplicate costs 2.48 RU. Ten worker replicas can exceed 101 writes per
second, so the load profile has to be planned against that ceiling rather than
discovered at it.

### Deduplication is the write

The document id is the producer's `event_id`, so uniqueness is the store's own
constraint and the worker does not have to remember anything. A redelivery --
after a lock expiry, a crash, or a scale-in -- collides no matter which replica
handles it. The in-memory deduplicator it replaces only ever caught redeliveries
that came back to the same replica, and said so.

It creates rather than upserts, deliberately. An upsert would succeed on a
repeat and overwrite the stored incident, so duplicates would be invisible and
could never be reported. Creating and catching the conflict makes the duplicate
an observable fact. Incidents are immutable events, so there is nothing an
update would legitimately change.

The logging handler, which keeps the in-memory deduplicator, remains the local
development path: the worker runs with no Azure account and no credentials, and
stores nothing.

### The deployed dashboard

CD deploys `business-dashboard/` alongside the other two services, via
`infra/dashboard.bicep`, on its own reader-only identity (see above). It runs
as a Container App with external ingress and **no authentication in front of
it** -- that is a deliberate choice, not an oversight, made because adding
login was out of scope for now. Anyone with the URL can see every tenant's
incident data. `cd.yml`'s deploy job prints the URL; it is also visible with:

```bash
az containerapp show -g rg-incident-platform -n incident-dashboard \
  --query properties.configuration.ingress.fqdn -o tsv
```

If this ever needs to be locked down, Container Apps' built-in Entra
authentication (an `authConfig` sub-resource, no application code change) is
the natural next step -- the same directory already used for the ingestion
API's producer tokens can gate this too.

### Running the business dashboard locally

`business-dashboard/` is a read-only Streamlit view over the same Cosmos DB
account the worker writes to. `disableLocalAuth: true` applies here too, so
there is no connection string to hand it — it authenticates the same way the
worker does, with `DefaultAzureCredential`, just against a role that can only
read.

```bash
az login
cd business-dashboard
pip install -r requirements.txt
export COSMOS_ENDPOINT="$(az cosmosdb list -g rg-incident-platform \
  --query "[?starts_with(name, 'incident-cosmos')].documentEndpoint | [0]" -o tsv)"
streamlit run app.py
```

Your `az login` user needs a Cosmos data-plane role on the account. Developers
listed in `DEVELOPER_PRINCIPAL_IDS` already have **Built-in Data Contributor**
from `core.bicep`, which reads and also writes — useful for clearing out
load-test documents before a demo, and deliberately not what the dashboard's own
identity holds.

Cosmos data-plane RBAC is not an Azure role and is not granted with
`az role assignment create`; it has its own command, should you need to add
someone outside that list:

```bash
COSMOS_ACCOUNT="$(az cosmosdb list -g rg-incident-platform \
  --query "[?starts_with(name, 'incident-cosmos')].name | [0]" -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv)"
az cosmosdb sql role assignment create \
  --account-name "$COSMOS_ACCOUNT" --resource-group rg-incident-platform \
  --scope "/" --principal-id "$ME" \
  --role-definition-id 00000000-0000-0000-0000-000000000001
```

#### Evidence on the dashboard

The **Evidence** page shows the image an incident's `evidence_uri` points at.
The container is private and the account has `allowSharedKeyAccess: false`, so
there is no URL a browser can fetch: `business-dashboard/evidence.py` reads the
bytes with the dashboard's managed identity — **Storage Blob Data Reader** on
the container, from `core.bicep` — and serves them inline.

The identity holds `blobs/read` and nothing more. Handing the browser a
user-delegation SAS instead would need `Storage Blob Delegator`, which is
deliberately not granted: a SAS is a bearer token in a URL, passed on by whoever
receives it and revocable only by rotating the key behind every SAS at once.

`evidence_uri` arrives from a producer, so it is untrusted. Only references whose
host and container match the configured account are fetched, and only the
remaining path is used — the same rules `notification-service/app/evidence.py`
applies, for the same reason. Two consequences worth knowing:

- Incidents created through `/api/v1/simulate` carry placeholder paths such as
  `/evidence/ppe_sample.jpg`, which name no real blob. They are listed as
  incidents and skipped on the Evidence page rather than shown as broken images.
- **Running locally, the page stays empty**, and that is not a
  misconfiguration. `DefaultAzureCredential` falls back to your `az login` user
  outside Azure, and a tenant Conditional Access policy refuses user accounts a
  token for `storage.azure.com` whatever Azure roles they hold. Deployed, the
  managed identity is not subject to that policy. Build against the 43 images
  committed under `evidence_images/` and let the container app fetch the real
  ones.

## Adding a new analytics type

A new kind of AI analytics needs no change to this platform, and
`scripts/demo-new-analytics-type.py` proves it rather than asserting it: it
builds the incident with the Producer SDK, ingests a type the platform has
never seen, and follows it through every layer, with nothing rebuilt,
redeployed or reconfigured in between.

```bash
pip install -r scripts/requirements.txt
az login

# Confirm the type really is unknown, and ingest nothing.
python scripts/demo-new-analytics-type.py --check

# Ingest one event of it and trace it through.
python scripts/demo-new-analytics-type.py --analytics-type loitering_detection \
                                          --event-type person_loitering
```

Pick a type nobody has used yet — step 1 reports if the source already mentions
it, and step 2 reports if the store already holds one, either of which makes the
demonstration weaker. The run prints seven numbered steps, ending with the log
lines each of the four services wrote about the event. A verified run:

```
4. Ingesting EVT-NEWTYPE-1790906479 through the real API
   202 Accepted. The API applied no validation specific to the type, because there is none to apply.
5. Waiting for the worker to persist it
   Stored after 6s or less.
     analytics_type = loitering_detection
6. Types the store knows now (6):
     loitering_detection  <- new
7. What each service logged about this event
     incident-ingestion     02:01:20,153 app.publisher publishing event_id=EVT-NEWTYPE-1790906479 to queue=incident-events
     incident-worker        02:01:20,507 app.store stored incident ... analytics_type=loitering_detection
     incident-notification  02:01:21,430 app.telegram_notifier Telegram notification delivered ... method=sendMessage
```

**Why it works.** `analytics_type` is a plain `str` in the Common Event Model —
no `Literal`, no `Enum`, no pattern — and so are `event_type`, `source_type` and
`severity`. Only `event_purpose` is constrained, because the pipeline genuinely
branches on it. Downstream nothing maps the value to behaviour: the worker logs
it, the notifier interpolates it into message text, and the dashboard derives its
filter options from whatever the store contains. Service Bus carries queues
rather than filtered topic subscriptions, so there is no routing rule to add, and
Cosmos partitions on `/tenant_id` and enforces no schema, so there is no
migration and no new partition.

Two things the demo is careful about, both worth knowing:

- `/api/v1/simulate/{analytics_type}` **does** hold a hardcoded list of three
  canned samples and rejects anything else. It is a demo shortcut, not the
  ingestion path, and the script deliberately posts to `/api/v1/incidents` so
  the difference is not glossed over.
- The event is labelled `event_purpose: "e2e_test"`, so the Telegram alert it
  produces is headed `Incident Alert [E2E TEST]`. Each run sends one real
  message to the shared chat. That is the point — the alert naming a type nobody
  configured is the last link in the chain — but it is not free, so run it
  deliberately.

The freedom has a cost the store already shows: `PPE_detection` and
`ppe_detection` both exist as distinct types, because nothing normalises the
string. Open types mean no spelling is wrong. If that matters later it belongs in
the producer, where the value originates, not in a check each consumer repeats.

## Measuring scalability

`load-simulator/` drives synthetic incidents at the deployed ingestion service
so the platform's scalability is demonstrated rather than asserted. It exists
because the two things worth showing cannot be produced by hand: the ingestion
service scaling on HTTP concurrency, and the worker scaling on queue depth from
zero.

The generator is **open loop**. A request is issued when its slot comes due,
not when the previous one returns. A closed-loop generator cannot exceed
`concurrency / latency` requests per second, so as the platform slows the
generator quietly slows with it: the queue never builds, the autoscaler never
sees a backlog, and the run reports a system that coped. Backlog is the thing
being measured, so the schedule has to be independent of the responses.

It also measures itself. Every request records how late it left against its
schedule, and the summary reports `generator_kept_up`. When that is false the
run describes the simulator rather than the platform, and every other figure
in it is void — a runner short of CPU or sockets otherwise reports the platform
as slow.

The load is spread across several producers, each with its own identity and its
own tenant, so incidents for different tenants are in flight at once. That is
the condition tenant isolation has to hold under, and it cannot be shown by
sending one at a time. A configurable fraction of incidents is re-sent under an
`event_id` already used, which turns deduplication into an observed outcome:
the store rejects the repeat rather than overwriting the original.

### Plan the profile against the store, not against the workers

One incident write costs a measured **9.9 RU** against a 1,000 RU/s free-tier
grant, so the store sustains roughly **101 writes per second**. Ten worker
replicas can exceed that. The default profile therefore steps up to the ceiling
and then crosses it deliberately: throttling that was planned for is a result,
throttling that was stumbled into is noise.

### Running it locally

Against the deployed service, with a token for your own user rather than a
producer's. This cannot demonstrate tenant isolation, because every request
then arrives with the same client id — it is for checking that the thing works
before spending a real run on it.

```bash
cd load-simulator
pip install -r requirements-dev.txt

export TARGET_URL="https://<the ingestion service URL>"
export STATIC_TOKEN="$(az account get-access-token \
  --resource "$AUTH_AUDIENCE" --query accessToken -o tsv)"

python -m app.main --stage 5:30 --stage 20:60 --producer local:TENANT-DEV
```

The tenant has to be `TENANT-DEV`, because that is what `TENANT_MAP` maps the
Azure CLI to and the API refuses a producer writing for anyone else. This needs
[the operator setup](#letting-a-person-call-the-api) to have been run once; until
it has, the token request itself fails with `AADSTS65001`, and a token obtained
some other way is refused with 403 for carrying no `Incidents.Write`.

Every option has an environment variable, because the same image runs as a job
in the cloud where there is no command line to type. Client secrets are read
from the environment only, never from an argument: a secret on the command line
is recorded in shell history and is visible in the process list.

Its tests need no network and no credentials:

```bash
cd load-simulator
python -m pytest tests -v
```

### Running the real measurement

`infra/simulator.bicep` deploys the simulator as a **Container Apps job** in the
same region as the platform. A job rather than an app because a load run starts,
drives a profile and ends. Same region because a generator in one region driving
a platform in another adds inter-region latency to every sample, and the figures
would be measuring the distance between two Azure regions.

Each simulated producer is a user-assigned managed identity, so the run holds no
client secret anywhere — not in the repository, not in a GitHub secret, not in
the job's environment. Two grants remain, both in the directory rather than in
ARM:

1. `Incidents.Write` on the API registration, for each producer identity
2. an entry per identity in the `TENANT_MAP` repository variable

Until both are done the job authenticates and is refused with 403, which is the
same answer any unregistered producer gets.

`scripts/grant-producer-roles.sh` does the first and prints the second. It reads
the identities and their tenants back from the deployment that created them, so
it cannot disagree with what was actually deployed, and it skips any identity
that already holds the role. Assigning application roles needs an Application
Administrator or an owner of the API registration; anyone else gets 403 from
Microsoft Graph, which is what that restriction is for.

`TENANT_MAP` reaches the ingestion service as an environment variable set at
deploy time, so it takes effect on the next CD run rather than when the variable
is saved.

CD deploys the job definition on every merge but never starts it. A profile that
ran itself would drive the platform, and the bill, on each merge and would
collide with whatever measurement was already in progress. Start a run by hand:

```bash
az containerapp job start -g rg-incident-platform -n incident-load-simulator
```

The summary is written to `OUT_DIR` and printed to stdout as one line of JSON
behind a `LOAD_SUMMARY_JSON` marker, because a job has no volume to read
afterwards and an exit code cannot carry a result.

Read it back from Log Analytics rather than from the job. `az containerapp job
logs show` streams from the replica, and a finished execution has none — it
answers `No replicas found for execution`, which is about the replica, not
about the run. A job's console logs also arrive with `ContainerAppName_s`
empty, so they are found by the replica name in `ContainerGroupName_s`:

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query "
  ContainerAppConsoleLogs_CL
  | where TimeGenerated > ago(1h)
  | where ContainerGroupName_s startswith 'incident-load-simulator'
  | where Log_s has 'LOAD_SUMMARY_JSON'
  | project TimeGenerated, Log_s
  | order by TimeGenerated desc"
```

### Reading the result honestly

Check `generator_kept_up` before anything else. When it is false the run
describes the simulator and every other number in it is void.

The usual cause is `MAX_INFLIGHT`. It caps how many requests may be in flight
at once, and reaching it turns an open-loop run closed: the schedule stalls
behind the platform's latency, and the achieved rate becomes that ceiling
divided by the latency. A first run asked for 500 incidents a second, hit the
default ceiling of 200, and reported 112 — which is arithmetic about the
simulator, not a measurement of the platform. It is set to 2,000 for the
deployed job for that reason.

Achieved rate is also not a ceiling unless the platform refused something.
A run where every request returned 202 found no limit; it just did not ask
for enough.

## Pipelines

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | Every push and pull request | Tests the Common Event Model and the Producer SDK; runs every service test suite; builds all four images and smoke tests each; scans the images (Trivy), the source (Bandit) and the dependencies (pip-audit); on pull requests, previews the infrastructure change against the live subscription |
| `cd.yml` | Push to `main` | Deploys infrastructure (registry, logging, Container Apps environment, Service Bus namespace and queue, identities), builds and pushes SHA-tagged images for all four components, rolls out the ingestion, worker and dashboard revisions, verifies `/health` reports the ingestion service healthy **and** publishing to Service Bus and that the dashboard is serving, publishes a real incident and waits for the worker to drain the queue, then deploys the load simulator's job definition without starting it |

Dependabot raises weekly pull requests for Python, Docker and Actions
dependencies.

The smoke test exists because unit tests cannot catch a broken Dockerfile or a
missing runtime dependency. The health verification exists so a deployment
cannot report success while being unreachable — and, because `/health` names the
active publisher, so it cannot report success while quietly dropping every
incident it accepts.

The worker has no endpoint to probe, so its verification is behavioural: CD
posts one incident through the deployed ingestion service and fails unless the
queue's backlog returns to zero, or if the message dead-letters instead. That
single assertion covers the whole path — ingress, publish, queue, the autoscaler
waking a replica from zero, receive, complete — and it is the only check that
would notice the worker's receive rights or its scale rule being wrong.

The model-drift check exists because both services carry their own copy of the
Common Event Model, so that each can build from its own directory. Drift between
the copies is a protocol break that neither service's unit tests can see: each
would still agree with itself.

The infrastructure preview runs `what-if` against the live subscription, which
catches what `az bicep build` cannot — a stale API version, an `existing`
reference to something absent, a change to a property that cannot be changed. It
runs as the deploy identity, using a federated credential for `pull_request` that
already existed and went unused, and that is the point of it: previewing role
assignments needs Role Based Access Control Administrator, which the pipeline
holds and no person does, so this is what puts a preview within everyone's reach.

Its output is filtered, which is unusual for a check and deliberate here. Raw
`what-if` reports around forty property changes against this template, of which
one is real: every value taken from another resource is reported as changing,
because a `reference()` cannot be evaluated before deployment and the expression
itself is printed as the new value. Each known class of false positive is counted
instead of listed, so that a genuine change is not the fortieth line of a summary
nobody reads.

Image builds are retried three times with increasing backoff. Both pipelines
pull the base image from Docker Hub and push to a registry, neither of which
we control; a 502 from either has already failed a deployment whose code was
correct. A retry distinguishes an outage from a defect instead of reporting
both as the same red check.

### A note on code scanning

GitHub's own code scanning — CodeQL and SARIF upload — needs GitHub Advanced
Security, which is a paid add-on on private repositories. Bandit, pip-audit and
Trivy give equivalent coverage at no cost and are enforced by failing the build
rather than by uploading to the Security tab. Trivy ignores unfixed
vulnerabilities: a CVE with no available patch in the base image is not
something a build can act on.

Dependabot alerts and version updates are free on private repositories and are
enabled separately.
