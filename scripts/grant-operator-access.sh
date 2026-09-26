#!/usr/bin/env bash
#
# Lets the people who build this platform call the incident API as themselves.
#
# `Incidents.Write` was defined for applications only, and the API exposed no
# delegated scope at all, so no human could obtain a token carrying it however
# much Azure RBAC they held -- Azure roles govern resources, not this API. The
# documented local path (`az account get-access-token --resource $AUTH_AUDIENCE`)
# therefore failed at consent with AADSTS65001. This closes that gap.
#
# Four things are needed, and all four matter:
#
#   1. A delegated scope, so the Azure CLI has something to be consented for.
#      It is called `user_impersonation` rather than `Incidents.Write` because
#      Entra refuses an app role and a scope that share a value, and because the
#      scope deliberately grants nothing on its own.
#   2. The CLI pre-authorized for that scope, which removes the consent prompt.
#   3. `Incidents.Write` assignable to users, and assigned to each operator.
#      This, not the scope, is what the API checks: auth.py looks for the role
#      in `roles` or `scp`, and a user-assigned app role lands in `roles`.
#   4. `appRoleAssignmentRequired` on the API, so Entra issues tokens only to
#      assigned principals.
#
# Steps 3 and 4 are each sufficient to keep strangers out, and both are applied.
# Without either one, pre-authorizing the CLI in step 2 would let any account in
# the tenant obtain a token for a publicly reachable API, because TENANT_MAP --
# which otherwise refuses unregistered clients -- has to carry the CLI's id for
# this to work at all.
#
# Run from a shell logged in with `az login` as an owner of the API
# registration, or an Application Administrator. Everyone else gets 403 from
# Microsoft Graph.
#
# Safe to re-run: every step is skipped when already in place.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-incident-platform}"
GITHUB_REPO="${GITHUB_REPO:-RameshMalar/scalable-incident-platform}"
ROLE_VALUE="${ROLE_VALUE:-Incidents.Write}"
SCOPE_VALUE="${SCOPE_VALUE:-user_impersonation}"
OPERATOR_TENANT="${OPERATOR_TENANT:-TENANT-DEV}"

# Microsoft's first-party Azure CLI client. Fixed across every tenant, which is
# why `az account get-access-token` can be pre-authorized at all.
AZURE_CLI_APP_ID="${AZURE_CLI_APP_ID:-04b07795-8ddb-461a-bbee-02f9e1bf7b46}"

for tool in az jq python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "This script needs ${tool}." >&2
    exit 1
  }
done

# Read from the repository so there is one source of both, with overrides for
# anyone working outside it.
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

# The same list core.bicep uses for data-plane roles, so the people who can read
# the store are the people who can write to the API, described in one place.
if [ -z "${OPERATOR_PRINCIPAL_IDS:-}" ]; then
  OPERATOR_PRINCIPAL_IDS="$(gh variable get DEVELOPER_PRINCIPAL_IDS \
    --repo "${GITHUB_REPO}" 2>/dev/null || true)"
fi

if [ -z "${OPERATOR_PRINCIPAL_IDS}" ]; then
  echo "No operators to grant. Set DEVELOPER_PRINCIPAL_IDS on ${GITHUB_REPO}," >&2
  echo "or pass OPERATOR_PRINCIPAL_IDS as a comma-separated list of object ids." >&2
  exit 1
fi

api_app_id="${AUTH_AUDIENCE#api://}"

echo "==> Reading the API registration ${AUTH_AUDIENCE}"
app_object_id="$(az ad app show --id "${api_app_id}" --query id -o tsv)"
api_sp_id="$(az ad sp show --id "${api_app_id}" --query id -o tsv)"
az ad app show --id "${api_app_id}" -o json >/tmp/operator-api-app.json

scope_id="$(jq -r --arg v "${SCOPE_VALUE}" \
  '.api.oauth2PermissionScopes[]? | select(.value == $v) | .id' \
  /tmp/operator-api-app.json)"

role_allows_users="$(jq -r --arg v "${ROLE_VALUE}" \
  '[.appRoles[]? | select(.value == $v) | .allowedMemberTypes[]] | index("User") != null' \
  /tmp/operator-api-app.json)"

# Both live under `api` and `appRoles`, and a PATCH replaces each collection
# wholesale, so they are sent together to avoid dropping one by writing the
# other.
if [ -z "${scope_id}" ] || [ "${role_allows_users}" != "true" ]; then
  if [ -z "${scope_id}" ]; then
    scope_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    echo "==> Exposing the ${SCOPE_VALUE} scope"
  fi
  [ "${role_allows_users}" = "true" ] ||
    echo "==> Making ${ROLE_VALUE} assignable to users"

  python3 - "${scope_id}" "${SCOPE_VALUE}" "${ROLE_VALUE}" <<'PY' >/tmp/operator-patch.json
import json
import sys

scope_id, scope_value, role_value = sys.argv[1], sys.argv[2], sys.argv[3]
app = json.load(open("/tmp/operator-api-app.json"))

roles = [
    dict(role, allowedMemberTypes=sorted({*role["allowedMemberTypes"], "User"}))
    if role["value"] == role_value
    else role
    for role in app["appRoles"]
]

scopes = [s for s in app.get("api", {}).get("oauth2PermissionScopes") or []
          if s["value"] != scope_value]
scopes.append({
    "id": scope_id,
    "value": scope_value,
    "type": "Admin",
    "isEnabled": True,
    "adminConsentDisplayName": "Access the incident API as the signed-in user",
    "adminConsentDescription":
        "Lets a client call the incident API on behalf of the signed-in user. "
        f"It does not by itself permit publishing: that needs the {role_value} "
        "application role, assigned to the user.",
    "userConsentDisplayName": "Access the incident API as you",
    "userConsentDescription":
        "Lets this client call the incident API on your behalf.",
})

json.dump({"appRoles": roles, "api": {"oauth2PermissionScopes": scopes}},
          sys.stdout)
PY

  az rest --method patch \
    --url "https://graph.microsoft.com/v1.0/applications/${app_object_id}" \
    --headers 'Content-Type=application/json' \
    --body @/tmp/operator-patch.json \
    --output none
else
  echo "    ${SCOPE_VALUE} already exposed, ${ROLE_VALUE} already allows users"
fi

# A separate call, because Entra rejects a scope id in preAuthorizedApplications
# that the same request is still in the middle of creating.
preauthorized="$(az ad app show --id "${api_app_id}" \
  --query "api.preAuthorizedApplications[?appId=='${AZURE_CLI_APP_ID}'] | [0].appId" \
  -o tsv 2>/dev/null || true)"

if [ -z "${preauthorized}" ] || [ "${preauthorized}" = "None" ]; then
  echo "==> Pre-authorizing the Azure CLI"
  az rest --method patch \
    --url "https://graph.microsoft.com/v1.0/applications/${app_object_id}" \
    --headers 'Content-Type=application/json' \
    --body "$(jq -nc --arg app "${AZURE_CLI_APP_ID}" --arg scope "${scope_id}" \
      '{api: {preAuthorizedApplications: [
         {appId: $app, delegatedPermissionIds: [$scope]}]}}')" \
    --output none
else
  echo "    Azure CLI already pre-authorized"
fi

role_id="$(az ad sp show --id "${api_app_id}" \
  --query "appRoles[?value=='${ROLE_VALUE}'].id | [0]" -o tsv)"

existing="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${api_sp_id}/appRoleAssignedTo" \
  --query "value[?appRoleId=='${role_id}'].principalId" -o tsv)"

granted=0
skipped=0
IFS=',' read -ra operators <<<"${OPERATOR_PRINCIPAL_IDS// /}"
for principal_id in "${operators[@]}"; do
  [ -n "${principal_id}" ] || continue

  if echo "${existing}" | grep -qx "${principal_id}"; then
    echo "    already holds ${ROLE_VALUE}: ${principal_id}"
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

# Left for last on purpose. Turning it on before the operators are assigned
# would lock out whoever is running this script mid-way through.
assignment_required="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${api_sp_id}" \
  --query appRoleAssignmentRequired -o tsv)"

if [ "${assignment_required}" != "true" ]; then
  echo "==> Requiring an app role assignment to get a token"
  az rest --method patch \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/${api_sp_id}" \
    --headers 'Content-Type=application/json' \
    --body '{"appRoleAssignmentRequired":true}' \
    --output none
else
  echo "    an app role assignment is already required"
fi

echo
echo "Granted ${granted}, already held ${skipped}."

entry="${AZURE_CLI_APP_ID}:${OPERATOR_TENANT}"
current="$(gh variable get TENANT_MAP --repo "${GITHUB_REPO}" 2>/dev/null || true)"

case ",${current}," in
*",${entry},"*)
  cat <<MSG

TENANT_MAP already maps the Azure CLI to ${OPERATOR_TENANT}, so operators can
publish as soon as they hold the role.

Check it end to end:

  export TOKEN="\$(az account get-access-token \\
    --resource ${AUTH_AUDIENCE} --query accessToken -o tsv)"

The token should carry ${ROLE_VALUE} in its \`roles\` claim, and incidents must
be submitted with "tenant_id": "${OPERATOR_TENANT}" -- any other value is
refused with 403, which is tenant isolation working rather than a fault.
MSG
  exit 0
  ;;
esac

cat <<MSG

The role lets operators write. TENANT_MAP decides which tenant they write for,
and without an entry they are still refused with 403 -- the same answer an
unregistered producer gets. Every operator shares the Azure CLI's client id, so
they share one tenant; ${OPERATOR_TENANT} keeps their test incidents out of the
tenants the isolation demonstration uses.

Set it:

  gh variable set TENANT_MAP --repo ${GITHUB_REPO} \\
    --body '${current:+${current},}${entry}'

The ingestion service reads TENANT_MAP as an environment variable set at deploy
time, so it takes effect on the next CD run, not when the variable is saved:

  gh workflow run CD --repo ${GITHUB_REPO} --ref main
MSG
