#!/usr/bin/env bash
# Future CD read-only verification; no subscriptions or incidents are created.
set -euo pipefail

: "${SUBSCRIPTION_URL:?}" "${SUBSCRIPTION_AUTH_AUDIENCE:?}" "${SUBSCRIPTION_TENANT_MAP:?}" "${AZURE_CLIENT_ID:?}" "${RUNNER_TEMP:?}"
if [[ ! "$SUBSCRIPTION_URL" =~ ^https://[A-Za-z0-9.-]+/?$ ]]; then
  echo '::error::Subscription API URL must be an approved HTTPS endpoint' >&2
  exit 1
fi

ready=no
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 20 "${SUBSCRIPTION_URL}/health" > "${RUNNER_TEMP}/subscription-health.json" \
    && jq -e '.status == "healthy" and .service == "subscription-service" and .store == "cosmos" and .auth == "entra"' "${RUNNER_TEMP}/subscription-health.json" >/dev/null \
    && curl -fsS --max-time 20 "${SUBSCRIPTION_URL}/ready" > "${RUNNER_TEMP}/subscription-ready.json" \
    && jq -e '.status == "ready" and .store == "cosmos"' "${RUNNER_TEMP}/subscription-ready.json" >/dev/null; then
    ready=yes
    break
  fi
  sleep 10
done
test "$ready" = yes

anonymous=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "${SUBSCRIPTION_URL}/api/v1/subscriptions")
invalid=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer local-invalid-verification-token' "${SUBSCRIPTION_URL}/api/v1/subscriptions")
if [ "$anonymous" != 401 ] || [ "$invalid" != 401 ]; then
  echo '::error::Subscription API did not reject missing and malformed bearer tokens' >&2
  exit 1
fi

# The existing deploy identity requires an explicitly approved Subscriptions.Read
# (or Write) role/scope and its own tenant mapping. No role is granted here.
token=$(az account get-access-token --resource "$SUBSCRIPTION_AUTH_AUDIENCE" --query accessToken -o tsv)
test -n "$token"
echo "::add-mask::$token"
trap 'unset token' EXIT
expected_tenant=$(python3 - <<'PY'
import os
mapping = dict(tuple(part.strip() for part in entry.split(':', 1)) for entry in os.environ['SUBSCRIPTION_TENANT_MAP'].split(',') if entry.strip())
print(mapping[os.environ['AZURE_CLIENT_ID']])
PY
)

offset=0
while true; do
  curl -fsS --max-time 20 -H "Authorization: Bearer ${token}" \
    "${SUBSCRIPTION_URL}/api/v1/subscriptions?limit=100&offset=${offset}" > "${RUNNER_TEMP}/subscription-page.json"
  jq -e --arg tenant "$expected_tenant" \
    '(.items | type == "array") and all(.items[]; .tenant_id == $tenant)' "${RUNNER_TEMP}/subscription-page.json" >/dev/null
  if [ "${REQUIRE_ENABLED_SUBSCRIPTION:-false}" != true ]; then
    break
  fi
  if jq -e 'any(.items[]; .enabled == true and .channel == "telegram")' "${RUNNER_TEMP}/subscription-page.json" >/dev/null; then
    break
  fi
  next=$(jq -r '.next_offset // empty' "${RUNNER_TEMP}/subscription-page.json")
  if [[ ! "$next" =~ ^[0-9]+$ ]] || [ "$next" -le "$offset" ] || [ "$next" -gt 10000 ]; then
    echo '::error::No enabled Telegram subscription found for the approved verifier tenant; routing activation refused' >&2
    exit 1
  fi
  offset=$next
done

echo 'Cosmos readiness, anonymous/malformed-token rejection and authenticated tenant-scoped read verified; no records written'
