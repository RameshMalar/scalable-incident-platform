# Phase 4: opt-in notification publication deduplication

The existing deterministic route-v1 MessageId remains unchanged: SHA-256 of
(tenant, event, subscription, channel, destination). Delivery remains one alert
per matching subscription, including subscriptions sharing a Telegram chat.
Worker persistence duplicates must still retry routing/publication.

## Infrastructure and wiring

infra/notification-dedup.bicep is standalone and is not invoked by CD.
provisionQueue defaults to false: no queue or assignments are created.
The existing core notification-events queue and all workload defaults are unchanged.
The approved namespace must remain Standard/Premium with local authentication disabled.
The allowed new names exclude notification-events; verify the selected new name
is absent before first provisioning. Do not use this template to update an existing queue.
The proposed history window is P1D, partitioning is disabled, and queue lock,
TTL and DLQ settings match the existing notification queue.

Supply existing identity object IDs, not client IDs. Worker receives Sender,
Notification receives Receiver, and the dedicated scaler receives the existing
queue-depth Data Owner role, each scoped only to the new queue. Application
identities receive no Owner role. No SAS key or namespace-wide grant is added.

Use the same notificationQueueName output as worker.bicep notificationQueue and
notification.bicep serviceBusQueue. These existing parameters feed NOTIFICATION_QUEUE,
SERVICE_BUS_QUEUE and the Notification KEDA queueName respectively. Worker KEDA
continues to watch the incident queue; it must not watch the notification queue.
Provisioning is preparation only and never switches either workload.

## Reviewed migration sequence (not performed by this change)

1. Read deployed queue settings, backlog/DLQ and identity assignments; obtain
   explicit migration approval. Compile/preview the standalone template and
   confirm the new queue name is absent and the old queue is untouched.
2. Provision the approved new queue with provisionQueue=true and allow scoped
   RBAC to propagate. Verify duplicate detection, window and managed identity access.
3. In a maintenance window pause incident producers and stop Worker processing;
   let in-flight work settle. Preserve queued incidents. Drain the old notification
   backlog with the existing consumer. Inspect its DLQ separately; do not automatically replay it.
4. Stop the old consumer after settling in-flight delivery. Configure Notification
   consumer and scaler to the new queue; verify readiness and the configured name.
5. Configure Worker publication to the same new queue, preserving its incident
   receiver/scaler configuration. Verify wiring before resuming Worker/producers.
6. Perform only separately approved E2E tests. Monitor DLQ, publication failures,
   queue depth and delivery failures. Retain the old queue for rollback.

## Persistent CD selection and fail-closed gate

In the existing protected production environment, an operator records
NOTIFICATION_QUEUE_NAME and NOTIFICATION_QUEUE_SELECTION_APPROVED=true after
an approved maintenance cutover. Each CD dispatch selecting a new queue must
provide notification_queue_review_reference. These variables persist across runs;
there is no one-run queue override. Blank selection keeps the legacy default.

CD checks the selected queue before any workload deployment and rechecks after
Worker approval. It reads namespace tier/local-auth settings, queue deduplication,
identity object IDs, exact queue-scoped roles, all active Worker/Notification
revision queue settings and Notification scaler queue metadata. API denial fails
closed. Deleted/blank selection after cutover fails because active revisions differ;
CD never automatically switches back. It also fails on partially completed cutover.
Operators must manually quiesce, settle/drain and configure both workloads first.
The current fixed incident identity/app names match CD's core-default restriction.

### Reviewed implicit legacy image defaults

The protected production variables `LEGACY_WORKER_DEFAULT_IMAGE` and
`LEGACY_NOTIFICATION_DEFAULT_IMAGE` are optional approval evidence for historical
images that omit `NOTIFICATION_QUEUE` or `SERVICE_BUS_QUEUE`, respectively.
Each must be a reviewed immutable `repository@sha256:<64 lowercase hex characters>`
reference that exactly matches the corresponding deployed container image string.
Review that image's code to establish its implicit `notification-events` default
before recording approval. A mutable tag, an attached identity, or the variable
alone does not prove the image's behavior. These variables do not select a queue.
When queue variables are explicit, these image approvals are not required.
Implicit defaults are never accepted for a selected duplicate-detection queue.

Before approving a deployment, inspect the current template and every active
Worker and Notification revision, including overlapping historical revisions.
Record each image, effective queue, `SERVICE_BUS_FQDN`, `AZURE_CLIENT_ID`, and each
Notification revision's scaler namespace, queue and managed identity. Confirm
application client IDs match the expected attached identities and that scalers
use the dedicated Notification scaler identity. Inspect revision templates rather
than assuming the latest template represents all running consumers.

One image approval variable exists per service. If overlapping revisions omit
the queue variable but use different image references, the gate cannot approve
both implicitly: stop deployment until a separately reviewed maintenance operation
settles in-flight work and removes the incompatible overlap or configures explicit
queue values. Do not disable active consumers merely to pass the gate without
following the staged backlog and cutover procedure above.

The gate blocks missing or mismatched legacy image evidence, unknown configuration,
missing active revisions, mixed queues, incorrect namespaces/client IDs, missing
identity attachments, inconsistent scalers, missing exact queue-scoped roles,
denied API reads, unsupported namespace/authentication settings, and invalid new
queue deduplication settings. A new queue additionally requires persistent approval,
a dispatch review reference, an explicit queue value in each revision, and a
nonpartitioned one-day window (equivalent duration spellings are accepted).
Selection changes between production approvals also block deployment. Missing
legacy selection after a new-queue cutover must not silently restore the old queue.

CD verifies an already completed cutover; it does not perform one. Operators must
also block manual cutover if approval, provisioning verification, in-flight
settlement, backlog/DLQ disposition or rollback evidence is incomplete. The gate
does not itself verify that every operational migration step was performed.

Provision the standalone queue only through a separately reviewed command using
`az deployment group create --mode Incremental` with this template and reviewed
parameters. Never use Complete mode. CD does not provision queues. Confirm the
selected new queue name is absent before provisioning; the template alone cannot
verify absence. Default provisionQueue=false is not a rollback/delete operation.

Before approving cutover, verify in the real broker (separate authorization):
send two synthetic messages with the same route ID and confirm one enqueue;
exercise accepted-send/lost-ack retry and distinct subscription IDs. Do not use
Telegram for this broker check. Verify identity access and scaler readiness.
Record old backlog and DLQ counts, drain approved backlog, quarantine/review DLQ
items without automatic replay, and record cutover/rollback evidence.

Normal CD uses the gate's queue output for both workloads, independent of legacy
core outputs. Rollback requires the same maintenance process: restore all active
revisions and scaler to the old queue, then update the persistent environment
selection. Changing only the variable cannot trigger a queue switch.

## Rollback

Pause producers and Worker first; settle in-flight sends. Drain or quarantine the
new queue with explicit approval, then stop its consumer. Restore the approved
Worker image/configuration and Notification consumer/scaler to notification-events,
verify readiness, and resume processing. Leave both queues and assignments intact
until a later cleanup review. Do not bulk copy messages between queues: deduplication
history is queue-local and previously delivered routes can be sent again.

## Guarantees and remaining risks

Within one day, repeated ingress with the same route ID is suppressed, including
partial fanout retry, lost broker acknowledgement and concurrent Worker publication.
This does not prevent consumer redelivery or make Telegram delivery exactly once.
Telegram can accept a request whose response is lost; a crash or failed queue
completion after sending can cause another alert. Photo/text fallback and lock
expiry need separate delivery failure tests. Ten sequential received messages,
long photo sends and 429 waits can exceed the five-minute lock; there is no automatic
lock renewal configured. One replica does not prevent restart/revision overlap.

The one-day window does not cover the 14-day TTL or arbitrary DLQ replay. Re-sending
a DLQ item inside the window can be suppressed despite needing recovery; outside
it, it can duplicate prior delivery. Require explicit replay review. Changed routes
or subscription IDs are new identities. IDs assume event IDs are unique per tenant;
changed payloads with reused identity can be suppressed. Queue migration resets history.

A future tenant-partitioned Cosmos delivery ledger can suppress confirmed deliveries
on redelivery using atomic claims and ETag updates. It needs scoped Notification RBAC,
retention and an explicit ambiguous-send recovery policy; it cannot atomically commit
Telegram delivery and database state. Batching is optional throughput work, not a
replacement for deduplication.

## Local validation

worker-service/tests/test_notification_dedup.py uses a fake broker with atomic,
time-bounded ingress deduplication and the real Worker handler/publisher. It tests
partial publication, stable retry IDs, accepted sends with lost acknowledgements,
concurrent workers, separate same-chat subscriptions and window expiry.
Infrastructure guards verify opt-in defaults, legacy preservation and workload wiring.
Fake behavior is a contract model, not proof of deployed Azure behavior. Actual broker
and Telegram testing requires separate approval.
