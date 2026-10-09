# Routed notification migration

Phase 2B-1 supports flat IncidentEvent messages and version-1 envelopes. Worker
publishing remains unchanged. No subscription matching occurs in this service.

NotificationProcessor defaults to `accept_legacy=True` to preserve the running
pipeline. Flat messages use the configured global TELEGRAM_CHAT_ID. Setting
`accept_legacy=False` rejects flat messages when migration is complete; remove
the legacy branch and optional notifier destination after all old messages drain.

Any envelope marker (`schema_version`, `subscription_id`, `channel`,
`telegram_chat_id`, or `incident`) selects envelope validation. Invalid/incomplete
envelopes are InvalidIncident errors, which the existing consumer dead-letters.
They are never reinterpreted as flat messages or sent to the global destination.

Valid envelopes explicitly pass their chat ID to the notifier. Text, photo,
caption delivery, photo-to-text fallback, and retries retain that destination in
local call state; shared `self.chat_id` is never changed. Existing Blob evidence
validation, managed identity access, and text degradation remain unchanged.

The current constructor still requires the global chat for legacy compatibility.
The bot token remains in existing environment/Key Vault configuration. Do not
enable routed production publishing until each destination is operator-approved
for its incident tenant. This phase does not verify chat ownership or reachability.

Load-test suppression and legacy EVT-LOAD policy apply to both formats. Explicit
e2e_test events remain eligible. Worker integration, route message IDs, runtime
activation, Azure provisioning and workflow changes remain later-phase work.
