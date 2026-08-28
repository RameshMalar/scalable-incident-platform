# Incident Ingestion Service API

Base URL (local): `http://localhost:8000`

The full, always-current schema and interactive request builder is at
[`/docs`](http://localhost:8000/docs) (Swagger UI) once the service is
running, generated from the same code this document describes. Use `/docs`
as the source of truth for field types and constraints; use this document
for the behavior that a schema can't express.

## `POST /api/v1/incidents`

The real ingestion endpoint. Accepts one incident, validates it against the
Common Event Model, and publishes it for downstream processing.

**This endpoint does not store incidents.** A `202` response means the event
was handed off to the publish target (Azure Service Bus in production) --
not that it has been processed, or even that anything is listening on the
other end yet. Ingestion's responsibility ends at a successful publish;
storage, alerting, and processing are downstream consumers' job.

### Request body

An `IncidentEvent` (Common Event Model) -- see [`app/models.py`](app/models.py)
or `/docs` for the full field list and constraints. Example:

```json
{
  "event_id": "EVT-A1B2C3D4",
  "tenant_id": "TENANT-01",
  "site_id": "SITE-01",
  "source_id": "CAM-004",
  "source_name": "North Gate Camera",
  "source_type": "video_analytics",
  "analytics_type": "intrusion_detection",
  "event_type": "intrusion_detected",
  "severity": "high",
  "timestamp": "2026-08-28T09:15:00",
  "confidence": 0.94,
  "description": "Person detected inside restricted area",
  "evidence_uri": "/evidence/intrusion_sample.jpg"
}
```

### Responses

| Status | Meaning | What to do |
| --- | --- | --- |
| `202 Accepted` | Validated and published successfully. | Nothing -- the event is in flight. |
| `422 Unprocessable Entity` | Request body failed Common Event Model validation (missing required field, out-of-range value, wrong type). | Fix the payload. Retrying without changes will fail the same way. |
| `502 Bad Gateway` | The event was valid but publishing failed -- a Service Bus error or a publish timeout. | Safe to retry; the failure is in the transport, not the payload. |

A `202` body looks like:

```json
{
  "status": "accepted",
  "event_id": "EVT-A1B2C3D4",
  "tenant_id": "TENANT-01",
  "site_id": "SITE-01",
  "analytics_type": "intrusion_detection",
  "event_type": "intrusion_detected",
  "message": "Incident received successfully"
}
```

A `502` body looks like:

```json
{
  "status": "error",
  "event_id": "EVT-A1B2C3D4",
  "message": "Failed to publish incident: timed out publishing event EVT-A1B2C3D4 after 5.0s"
}
```

```bash
curl -X POST http://localhost:8000/api/v1/incidents \
  -H "Content-Type: application/json" \
  -d '{"event_id":"EVT-A1B2C3D4","tenant_id":"TENANT-01","site_id":"SITE-01","source_id":"CAM-004","source_name":"North Gate Camera","source_type":"video_analytics","analytics_type":"intrusion_detection","event_type":"intrusion_detected","severity":"high","timestamp":"2026-08-28T09:15:00","confidence":0.94,"description":"Person detected inside restricted area","evidence_uri":"/evidence/intrusion_sample.jpg"}'
```

## `POST /api/v1/simulate/{analytics_type}`

**Demo/development convenience only -- not part of the real ingestion
contract.** Generates a realistic sample `IncidentEvent` for `analytics_type`
one of `intrusion`, `ppe`, or `crowd`, publishes it the same way
`/api/v1/incidents` would, and also records it in the in-memory list backing
the `/` dashboard. Use it when you want to see an event flow through the
system without a real analytics producer.

```bash
curl -X POST http://localhost:8000/api/v1/simulate/intrusion
```

Returns `{"status": "error", "message": "Unsupported analytics type"}` for
any `analytics_type` outside the three supported samples.

## `GET /`

Demo dashboard. Lists incidents generated via `/api/v1/simulate/{analytics_type}`
in an in-memory list, for local visualization. It does **not** reflect
incidents submitted through `/api/v1/incidents` -- that path publishes only
and never touches this list. The list resets whenever the process restarts
and is not shared across replicas.

## `GET /health`

Health check used by the deploy pipeline and Container Apps.

```json
{ "status": "healthy", "service": "incident-ingestion-service" }
```
