#!/usr/bin/env bash
#
# Starts CD with the reviewed inputs in infra/cd-production.json, rather than
# typing ten fields into the "Run workflow" form.
#
# The inputs are read from origin/main, not the working tree: changing them
# takes a pull request, so what CD receives is what was reviewed. They are
# identifiers, not credentials.
#
# `"core_deployment_name": "latest"` resolves to the newest successful core
# deployment in the resource group: CD's `core-<sha>`, or `core` from a hand
# deployment after a teardown. Every CD run creates a new one, so a fixed name
# would be stale after the first run.
#
# Before dispatching, it runs the same two checks as CD's preflight job, so a
# mistake fails here in seconds instead of in a queued run:
#   - scripts/validate-rollout-config.py over the inputs and repository
#     variables;
#   - scripts/verify-approved-merge.py: the tip of main must be the merge of
#     a pull request that someone other than its author approved.
#
# Run from a shell logged in with both `az login` and `gh auth login`.
# Pass --yes to skip the confirmation prompt.
set -euo pipefail

REPO="${GITHUB_REPO:-RameshMalar/scalable-incident-platform}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-incident-platform}"
CONFIG="infra/cd-production.json"

for tool in az gh git jq python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "This script needs ${tool}." >&2
    exit 1
  }
done

cd "$(git rev-parse --show-toplevel)"
git fetch -q origin main
config="$(git show "origin/main:${CONFIG}")"

core="$(jq -r .core_deployment_name <<<"$config")"
if [ "$core" = latest ]; then
  core="$(az deployment group list --resource-group "$RESOURCE_GROUP" \
    --query "sort_by([?(name == 'core' || starts_with(name, 'core-')) && properties.provisioningState == 'Succeeded'], &properties.timestamp)[-1].name" \
    -o tsv)"
  if [ -z "$core" ]; then
    echo "No successful core deployment in ${RESOURCE_GROUP}. CD only updates an existing environment." >&2
    exit 1
  fi
fi
config="$(jq --arg core "$core" '.core_deployment_name = $core' <<<"$config")"

# The validator reads each input under its upper-cased name, plus the
# repository variables CD passes alongside them.
validator_env=(DEPLOY_REF=refs/heads/main "RESOURCE_GROUP=${RESOURCE_GROUP}")
while IFS= read -r pair; do
  validator_env+=("$pair")
done < <(jq -r 'to_entries[] | "\(.key | ascii_upcase)=\(.value | tostring)"' <<<"$config")
while IFS= read -r pair; do
  validator_env+=("$pair")
done < <(gh variable list --repo "$REPO" --json name,value --jq '.[] | "\(.name)=\(.value)"')
env "${validator_env[@]}" python3 scripts/validate-rollout-config.py

REPOSITORY="$REPO" DEPLOY_SHA="$(git rev-parse origin/main)" python3 scripts/verify-approved-merge.py

echo
echo "CD inputs (from origin/main:${CONFIG}):"
jq -r 'to_entries[] | "  \(.key) = \(.value)"' <<<"$config"
echo

if [ "${1:-}" != "--yes" ]; then
  read -r -p "Dispatch CD on main with these inputs? [y/N] " answer
  [ "$answer" = y ] || exit 1
fi

inputs=()
while IFS= read -r pair; do
  inputs+=(-f "$pair")
done < <(jq -r 'to_entries[] | "\(.key)=\(.value | tostring)"' <<<"$config")
gh workflow run cd.yml --repo "$REPO" --ref main "${inputs[@]}"

# The run takes a moment to appear after dispatch.
sleep 5
gh run list --repo "$REPO" --workflow cd.yml --limit 1 --json url --jq '.[0].url'
