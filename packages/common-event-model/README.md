# Common Event Model

The contract between analytics producers and incident consumers. One incident
shape for every analytics type the platform carries.

```python
from common_event_model import IncidentEvent

event = IncidentEvent(
    event_id="EVT-0001",
    tenant_id="TENANT-01",
    site_id="SITE-01",
    source_id="CAM-07",
    source_name="Loading bay camera CAM-07",
    source_type="video_analytics",
    analytics_type="intrusion_detection",
    event_type="intrusion_detected",
    severity="high",
    timestamp="2026-10-02T02:01:20Z",
)
```

## Why most fields are open strings

`analytics_type`, `event_type`, `source_type` and `severity` accept any value.
Constraining them would mean editing this library, releasing it, and redeploying
every service that installs it, in order to accept a kind of analytics that
changes nothing about how an incident is stored, routed or delivered.

`event_purpose` is the one exception and is a `Literal`, because the pipeline
genuinely branches on it: `load_test` events are persisted without paging
anyone. The rule the model follows is that a value nothing acts on should not be
enumerated, and a value something acts on should not be free.

The cost is that nothing normalises spelling, so `PPE_detection` and
`ppe_detection` can both exist. If that needs fixing it belongs in the producer,
where the value originates, not as a check each consumer repeats.

## Installing

Not published to an index. Services install it from the repository, which is why
their Docker builds use the repository root as context:

```bash
pip install ./packages/common-event-model
```

`pydantic` is declared as a range rather than a pin. A library that pins an
exact version makes itself unresolvable against a service that needs another
one; the services keep their own `pydantic==2.13.5`.
