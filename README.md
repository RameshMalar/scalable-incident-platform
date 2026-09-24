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

Run the service directly:

```bash
cd ingestion-service
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Or through Compose:

```bash
docker compose up --build
```

Either way the service is at http://localhost:8000 — dashboard at `/`, Swagger
at `/docs`, liveness at `/health`, readiness at `/ready`.

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
Telegram. Load-test incidents have ids beginning with `EVT-LOAD-`; they are
still persisted for scalability measurements, but the worker does not publish
them to the human notification channel. The Notification Service repeats that
check so a deployment can safely drain load events that were queued before the
worker-side filter existed.

Telegram delivery is intentionally limited to one Notification Service replica
and one message every 3.1 seconds. If Telegram returns HTTP 429, the notifier
honours the response's `retry_after` value and retries once before handing the
message back to Service Bus. The notification queue uses a five-minute message
lock so that wait and both HTTP attempts can finish before the broker redelivers
the event. This avoids the immediate redelivery loop that can otherwise turn
one rate limit into five failed deliveries and a dead-lettered message.

If the Notification Service has been manually stopped, deploy this filtering
and rate-limiting behaviour before starting it. Do not purge the queue merely
to make the count reach zero: active `EVT-LOAD-*` messages can now be completed
without contacting Telegram, while the dead-letter subqueue remains available
for investigation. Check the counts, start the app, and watch them drain with:

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

Your `az login` user needs **Cosmos DB Built-in Data Reader** on the account —
not the worker's Data Contributor role, which also grants write. Cosmos data-plane
RBAC is not an Azure role and is not granted with `az role assignment create`;
it has its own command:

```bash
COSMOS_ACCOUNT="$(az cosmosdb list -g rg-incident-platform \
  --query "[?starts_with(name, 'incident-cosmos')].name | [0]" -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv)"
az cosmosdb sql role assignment create \
  --account-name "$COSMOS_ACCOUNT" --resource-group rg-incident-platform \
  --scope "/" --principal-id "$ME" \
  --role-definition-id 00000000-0000-0000-0000-000000000001
```

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

python -m app.main --stage 5:30 --stage 20:60 --producer alpha:TENANT-01
```

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
| `ci.yml` | Every push and pull request | Checks the Common Event Model is identical between the two Cosmos-facing services; runs every test suite; builds all four images and smoke tests each; scans the images (Trivy), the source (Bandit) and the dependencies (pip-audit) |
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
