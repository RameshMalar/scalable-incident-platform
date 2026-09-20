#!/usr/bin/env bash
#
# Grants the load simulator's producer identities the right to write incidents.
#
# infra/simulator.bicep creates one managed identity per simulated producer,
# but two things it cannot do live in the directory rather than in Azure:
# granting each identity the `Incidents.Write` application role, and mapping
# each to the one tenant it may speak for. This does the first and prints the
# second.
#
# The identities and their tenants are read back from the deployment that
# created them rather than typed in again here, so this cannot disagree with
# what was actually deployed.
#
# Run once, after CD has deployed the simulator job, from a shell logged in
# with `az login` as someone who can assign application roles -- an
# Application Administrator, or an owner of the API registration. Everyone
# else gets 403 from Microsoft Graph, which is the point of the restriction.
#
# Safe to re-run: an identity that already holds the role is left alone.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-incident-platform}"
GITHUB_REPO="${GITHUB_REPO:-RameshMalar/scalable-incident-platform}"
ROLE_VALUE="${ROLE_VALUE:-Incidents.Write}"

for tool in az jq; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "This script needs ${tool}." >&2
    exit 1
  }
done

# The API's Application ID URI, which is also the audience tokens are
# requested for. Taken from the repository variable so there is one source of
# it, with an override for anyone working outside the repository.
if [ -z "${AUTH_AUDIENCE:-}" ]; then
  command -v gh >/dev/null 2>&1 || {
    echo "Set AUTH_AUDIENCE, or install the GitHub CLI so it can be read from the repository." >&2
    exit 1
  }
  AUTH_AUDIENCE="$(gh variable get AUTH_AUDIENCE --repo "${GITHUB_REPO}" 2>/dev/null || true)"
fi

if [ -z "${AUTH_AUDIENCE}" ]; then
  echo "AUTH_AUDIENCE is not set and could not be read from ${GITHUB_REPO}." >&2
  exit 1
fi

api_app_id="${AUTH_AUDIENCE#api://}"

echo "==> Reading the simulator deployment in ${RESOURCE_GROUP}"

# Most recent first: re-running CD produces a new deployment each time, and an
# older one may name identities that have since been renamed away.
outputs="$(az deployment group list \
  --resource-group "${RESOURCE_GROUP}" \
  --query "sort_by([?starts_with(name, 'simulator-')], &properties.timestamp) | [-1].properties.outputs" \
  -o json)"

if [ -z "${outputs}" ] || [ "${outputs}" = "null" ]; then
  echo "No simulator deployment found in ${RESOURCE_GROUP}." >&2
  echo "CD deploys it on every push to main; let one finish first." >&2
  exit 1
fi

principal_ids="$(echo "${outputs}" | jq -r '.producerPrincipalIds.value[]')"
tenant_map_entries="$(echo "${outputs}" | jq -r '.tenantMapAdditions.value | join(",")')"

if [ -z "${principal_ids}" ]; then
  echo "The deployment reported no producer identities." >&2
  exit 1
fi

echo "==> Resolving ${ROLE_VALUE} on ${AUTH_AUDIENCE}"

api_sp_id="$(az ad sp show --id "${api_app_id}" --query id -o tsv)"
role_id="$(az ad sp show --id "${api_app_id}" \
  --query "appRoles[?value=='${ROLE_VALUE}'].id | [0]" -o tsv)"

if [ -z "${role_id}" ] || [ "${role_id}" = "null" ]; then
  echo "The API registration defines no ${ROLE_VALUE} application role." >&2
  exit 1
fi

# Already-assigned identities are skipped rather than reassigned: Graph
# answers 409 on a duplicate, and a re-run should be quiet, not fatal.
existing="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${api_sp_id}/appRoleAssignedTo" \
  --query "value[?appRoleId=='${role_id}'].principalId" -o tsv)"

granted=0
skipped=0
for principal_id in ${principal_ids}; do
  if echo "${existing}" | grep -qx "${principal_id}"; then
    echo "    already holds it: ${principal_id}"
    skipped=$((skipped + 1))
    continue
  fi

  echo "==> Granting ${ROLE_VALUE} to ${principal_id}"
  az rest --method post \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/${api_sp_id}/appRoleAssignedTo" \
    --headers 'Content-Type=application/json' \
    --body "{\"principalId\":\"${principal_id}\",\"resourceId\":\"${api_sp_id}\",\"appRoleId\":\"${role_id}\"}" \
    --output none
  granted=$((granted + 1))
done

echo
echo "Granted ${granted}, already held ${skipped}."

current="$(gh variable get TENANT_MAP --repo "${GITHUB_REPO}" 2>/dev/null || true)"
merged="${tenant_map_entries}"
if [ -n "${current}" ]; then
  merged="${current},${tenant_map_entries}"
fi

cat <<MSG

The role lets these identities write. TENANT_MAP decides which tenant each may
write for, and without an entry they are still refused -- with 403, the same
answer an unregistered producer gets.

Set it:

  gh variable set TENANT_MAP --repo ${GITHUB_REPO} \\
    --body '${merged}'

The ingestion service reads TENANT_MAP as an environment variable set at
deploy time, so it takes effect on the next CD run, not when the variable is
saved. Re-run CD before starting a load run:

  gh workflow run CD --repo ${GITHUB_REPO} --ref main
MSG
