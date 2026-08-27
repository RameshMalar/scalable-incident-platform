# Scalable Incident Platform

A reusable, cloud-native platform that receives standardized incident events
from AI video analytics producers, processes them, and delivers them to
downstream consumers. Intrusion Detection is the seed producer used to validate
the architecture.

Sprint 1 delivers the Common Event Model and the Incident Ingestion Service.

## Repository layout

| Path | Contents |
| --- | --- |
| `ingestion-service/` | Incident Ingestion Service (FastAPI) and its tests |
| `infra/` | Azure infrastructure as Bicep: `core.bicep` (shared) and `ingestion.bicep` (per service) |
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
at `/docs`, health at `/health`.

Run the tests:

```bash
cd ingestion-service
python -m pytest tests -v
```

Generate a sample incident without a real camera:

```bash
curl -X POST http://localhost:8000/api/v1/simulate/intrusion
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
  --template-file infra/main.bicep
```

The template is idempotent — re-running it reconciles drift rather than
creating duplicates. It provisions a container registry, a Log Analytics
workspace, a Container Apps environment, and the ingestion service scaled from
zero to ten replicas on HTTP concurrency.

On the very first deployment the app starts on a placeholder image; the CD
pipeline replaces it with the real one.

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

A message the worker abandons five times, or one that outlives its time-to-live,
moves to the queue's built-in **dead-letter subqueue** rather than blocking the
queue or vanishing. The dead-letter path is not a separate resource — it is
`incident-events/$deadletterqueue`, read by opening a receiver on that subqueue.

The processing worker itself deploys today as a skeleton on a public placeholder
image (`infra/worker.bicep`): it takes no ingress and does not yet consume, but
its environment, identity and receive rights are provisioned and wired. Replacing
the placeholder with the real image is a one-parameter change once that code
lands.

### Verifying the queue without the producer or worker

`scripts/verify-servicebus.py` exercises both paths this milestone owns —
*reaches the queue* and *reaches the dead-letter subqueue* — without either
service. It publishes a message, receives it back, deliberately dead-letters it,
and confirms it in the DLQ.

```bash
az login
pip install -r scripts/requirements.txt
export SERVICE_BUS_FQDN=$(az deployment group show \
  -g "$RESOURCE_GROUP" -n core-<sha> \
  --query properties.outputs.serviceBusFqdn.value -o tsv)
python scripts/verify-servicebus.py
```

It runs as your `az login` user, so that account also needs the Data Sender and
Data Receiver roles on the namespace (or Owner on the resource group).

## Pipelines

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | Every push and pull request | Runs unit tests; builds the image and smoke tests the running container; scans the image (Trivy), the source (Bandit) and the dependencies (pip-audit) |
| `cd.yml` | Push to `main` | Deploys infrastructure (registry, logging, Container Apps environment, Service Bus namespace and queue, identities), builds and pushes a SHA-tagged image, rolls out the ingestion revision, deploys the processing-worker skeleton, verifies `/health` |

Dependabot raises weekly pull requests for Python, Docker and Actions
dependencies.

The smoke test exists because unit tests cannot catch a broken Dockerfile or a
missing runtime dependency. The health verification exists so a deployment
cannot report success while being unreachable.

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
