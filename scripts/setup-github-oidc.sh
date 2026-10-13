#!/usr/bin/env bash
#
# One-off bootstrap: lets GitHub Actions deploy into Azure without any
# long-lived secret in the repository.
#
# We use a user-assigned managed identity rather than an Entra app
# registration. Registering applications is usually restricted to admins in a
# university tenant, whereas creating a managed identity only needs rights on
# the resource group, which the team already has.
#
# Run once, from a shell logged in with `az login`.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-incident-platform}"
LOCATION="${LOCATION:-malaysiawest}"
IDENTITY_NAME="${IDENTITY_NAME:-incident-github-deploy}"
GITHUB_REPO="${GITHUB_REPO:-RameshMalar/scalable-incident-platform}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"

# Contributor deploys the template; RBAC Administrator is needed because the
# template itself creates a role assignment (the ingestion app's AcrPull).
# Together they stay well short of Owner.
CONTRIBUTOR="b24988ac-6180-42a0-ab88-20f7382dd24c"
RBAC_ADMIN="f58310d9-a9f6-439a-9e8d-f62e7b41a168"

subscription_id="$(az account show --query id -o tsv)"
tenant_id="$(az account show --query tenantId -o tsv)"
rg_scope="/subscriptions/${subscription_id}/resourceGroups/${RESOURCE_GROUP}"

echo "==> Creating managed identity '${IDENTITY_NAME}' in ${RESOURCE_GROUP}"
az identity create \
  --name "${IDENTITY_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --output none

client_id="$(az identity show -n "${IDENTITY_NAME}" -g "${RESOURCE_GROUP}" --query clientId -o tsv)"
principal_id="$(az identity show -n "${IDENTITY_NAME}" -g "${RESOURCE_GROUP}" --query principalId -o tsv)"

echo "==> Granting roles on the resource group"
for role in "${CONTRIBUTOR}" "${RBAC_ADMIN}"; do
  az role assignment create \
    --assignee-object-id "${principal_id}" \
    --assignee-principal-type ServicePrincipal \
    --role "${role}" \
    --scope "${rg_scope}" \
    --output none
done

# GitHub signs its tokens with immutable subject claims: the subject carries
# the numeric owner and repository ids, not just their names. A credential
# written with the plain names is never matched, and the failure surfaces as
# AADSTS700213 at sign-in rather than as anything about naming.
if ! command -v gh >/dev/null 2>&1; then
  echo "This script needs the GitHub CLI to read the repository's numeric ids." >&2
  exit 1
fi

owner="${GITHUB_REPO%%/*}"
repo="${GITHUB_REPO##*/}"
owner_id="$(gh api "users/${owner}" --jq .id)"
repo_id="$(gh api "repos/${GITHUB_REPO}" --jq .id)"
qualified="repo:${owner}@${owner_id}/${repo}@${repo_id}"

# The deploy branch is the only trusted GitHub context. A workflow triggered
# from any other branch, a pull request or a fork gets no token at all.
add_federated_credential() {
  local name="$1" subject="$2"
  echo "==> Federated credential: ${name}"
  az identity federated-credential create \
    --name "${name}" \
    --identity-name "${IDENTITY_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "${subject}" \
    --audiences "api://AzureADTokenExchange" \
    --output none
}

# Deliberately no `pull_request` or `environment:` credential. No workflow signs
# in to Azure on a pull request, and on GitHub Free a private repository cannot
# restrict which branches use an environment, so an environment subject would
# let a workflow on any branch sign in. Jobs that deploy therefore name no
# environment and carry this branch subject.
add_federated_credential "branch-${DEPLOY_BRANCH}" "${qualified}:ref:refs/heads/${DEPLOY_BRANCH}"

cat <<MSG

Done. Add these three repository variables in GitHub
(Settings -> Secrets and variables -> Actions -> Variables):

  AZURE_CLIENT_ID        ${client_id}
  AZURE_TENANT_ID        ${tenant_id}
  AZURE_SUBSCRIPTION_ID  ${subscription_id}
  AZURE_RESOURCE_GROUP   ${RESOURCE_GROUP}

They are identifiers, not credentials, so variables are enough -- there is no
secret to store.
MSG
