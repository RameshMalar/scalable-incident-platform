#!/usr/bin/env bash
# Authorization rejection only; never authenticated or E2E publishing.
set -euo pipefail
: "${INGESTION_URL:?}"
anon=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
  -X POST "${INGESTION_URL}/api/v1/incidents" \
  -H 'content-type: application/json' \
  -d '{"event_id":"EVT-ANON","tenant_id":"T","site_id":"S","source_id":"C","source_name":"n","source_type":"video_analytics","analytics_type":"intrusion_detection","event_type":"intrusion_detected","severity":"low","timestamp":"2026-01-01T00:00:00"}')
if [ "$anon" != "401" ]; then
  echo "::error::An unauthenticated POST returned ${anon}, expected 401." >&2
  exit 1
fi
