# Scalable Incident Platform

A reusable, cloud-native platform that receives standardized incident events
from AI video analytics producers, processes them, and delivers them to
downstream consumers. Intrusion Detection is the seed producer used to validate
the architecture.

Sprint 1 delivers the Common Event Model and the Incident Ingestion Service.
Sprint 2 adds the Processing Worker and scales it on queue depth.

## Repository layout

| Path | Contents |
| --- | --- |
| `ingestion-service/` | Incident Ingestion Service (FastAPI) and its tests |
| `worker-service/` | Processing Worker: consumes `incident-events`, and its tests |
| `infra/` | Azure infrastructure as Bicep: `core.bicep` (shared), `ingestion.bicep` and `worker.bicep` (per service) |
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
Bus namespace and queue, and the identities. `ingestion.bicep` and
`worker.bicep` each deploy one service on top of it and both require an `image`
parameter, so in practice they are deployed by CD, which has just built one.

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

The ingestion service opens its queue sender once, at startup, and reuses it for
the life of the process. Opening a client per message costs a TLS and AMQP
handshake on every request: harmless in a demo, and enough to dominate the
throughput and latency figures the platform is assessed on.

Transport is a switch rather than a constant. Native AMQP needs port 5671, which
campus and corporate firewalls routinely block, so the default everywhere is
AMQP over WebSocket on 443 and local development just works. The deployed
service sets `SERVICE_BUS_TRANSPORT=amqp` to take the faster native port, where
the egress is ours to control.

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

## Pipelines

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | Every push and pull request | Checks the Common Event Model is identical in both services; runs both test suites; builds both images and smoke tests each; scans the images (Trivy), the source (Bandit) and the dependencies (pip-audit) |
| `cd.yml` | Push to `main` | Deploys infrastructure (registry, logging, Container Apps environment, Service Bus namespace and queue, identities), builds and pushes SHA-tagged images for both services, rolls out the ingestion and worker revisions, verifies `/health` reports both healthy **and** publishing to Service Bus, then publishes a real incident and waits for the worker to drain the queue |

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
