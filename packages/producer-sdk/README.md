# Producer SDK

Builds, validates, authenticates and delivers incidents to the platform.
The library a new analytics pipeline is supposed to install, rather than
re-implementing the contract, the token, the retry policy and the evidence URI.

```python
from incident_producer import AzureCliToken, Deployment, IncidentProducer, build

deployment = Deployment.discover()
producer = IncidentProducer(
    deployment.endpoint, token_source=AzureCliToken(deployment.audience)
)
producer.wake()

receipt = producer.send(
    build(
        tenant_id="TENANT-01",
        site_id="SITE-01",
        source_id="CAM-07",
        source_name="Loading bay camera CAM-07",
        analytics_type="intrusion_detection",
        event_type="intrusion_detected",
        severity="high",
    )
)
```

`Deployment.discover()` is for our own scripts. A third-party producer is given
a URL and an audience and does not read our deployment. `token_source` is any
callable returning a string, so a managed identity is a one-liner:

```python
credential = DefaultAzureCredential()
producer = IncidentProducer(
    endpoint,
    token_source=lambda: credential.get_token(f"{audience}/.default").token,
)
```

## What it does, and what it does not

The proposal called for a reusable library covering event construction,
validation, authentication, evidence upload and reliable delivery. Those are
the five modules here. What it deliberately does not do:

- It does not talk to Cosmos, Service Bus, or Telegram. A producer writes
  incidents through the API and never reads them back.
- It does not enumerate `analytics_type`. A new pipeline must not need a
  release of this SDK any more than it needs a release of the Common Event
  Model. That claim is tested.
- Delivery is `urllib` from the standard library. A producer often runs on a
  camera gateway, and every dependency is something they have to accept, audit
  and keep patched. The Azure SDK arrives only with the optional `evidence`
  extra, which a producer that sends no images never installs.

Retrying a delivery is safe because the store keys documents on the producer's
`event_id`. `build()` assigns that id once. A timeout is retried on purpose:
the API may have accepted the incident before the socket gave up, and a second
attempt with the same id is a duplicate rather than a second document. A 4xx
that is not 429 is not retried.

## Evidence

```bash
pip install ./packages/producer-sdk[evidence]
```

```python
from incident_producer import EvidenceUploader

uri = EvidenceUploader(blob_endpoint).upload("intrusion.jpg", blob_name="SITE-01/CAM-07/evt.jpg")
producer.send(build(..., evidence_uri=uri))
```

This does not work from a laptop. A tenant Conditional Access policy refuses
user accounts a token for `storage.azure.com`, so only a workload identity
(the Evidence workflow, or a producer running in Azure) can write. The
returned URI is a plain `https` blob reference, not a SAS.

## Installing

Not published to an index. From the repository root:

```bash
pip install ./packages/common-event-model ./packages/producer-sdk
```

The Common Event Model is not on an index, so it has to be installed from this
repository in the same command (or first). Python 3.12 or newer, same
interpreter the services run.
