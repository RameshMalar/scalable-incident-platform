# October 24 demonstration — gated walkthrough

Do not run live steps until the readiness/security gates are approved. The
current local implementation is not an already deployed unified platform.

1. Open the existing dashboard URL. An anonymous visitor receives the sign-in
   screen, with no incident data, gallery or simulator access.
2. Sign in through the approved Entra client as the approved operator. Confirm
   the tenant list contains only TENANT-01/02/03. Explain that unknown users and
   other persisted tenants are denied on the server.
3. Show executive KPIs, restored period comparisons, severity distribution and
   trend chart. Point out the tenant/date filters and actual last-read timestamp.
4. Show latest-first records and their event IDs, source/site and UTC times.
   Expand an associated evidence image; show the categorized gallery and paging.
   These are stored AI analytics evidence images, not a live video stream.
5. Use the preserved Platform Observability link to open Grafana. No Grafana
   resource/settings changes are part of the dashboard release.
6. After subscription owner approval and verified backend implementation, open
   **Account Setup**, select an authorized tenant, link Telegram through the
   one-time private-chat ownership flow, and optionally opt in. No chat ID or bot
   token is entered into the dashboard. Demonstrate opt out with mocked delivery
   until separate consent permits real bot messages.
7. Only after both authorization layers and explicit live-test consent: enable
   the controlled simulator, select one authorized tenant and a supported type,
   acknowledge one test, submit once, and show API acceptance/event ID separately
   from Cosmos confirmation. Refresh the incident feed to find the persisted ID.
   The existing API owns severity/evidence selection; do not pretend unsupported
   custom selections exist. Events retain `e2e_test` purpose and eligible routing.
8. End with denial cases using mocks or approved negative-token tests. Explain
   real-time video as a future extension and keep the rollback/security notes ready.

Without the approvals above, demonstrate only the local mocked regression
results and prepared UI; do not label mocked authentication, ownership or
notification delivery as a verified live integration.
