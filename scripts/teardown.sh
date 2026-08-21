#!/usr/bin/env bash
#
# Deletes everything the templates create, so an idle project stops costing
# anything. The registry is the only resource that bills while idle -- the
# container app scales to zero and Log Analytics charges by ingestion.
#
# Rebuilding is one pipeline run: the templates are idempotent and the image
# is rebuilt from the commit it is tagged with. Nothing here is unrecoverable
# except the pushed images, which the pipeline recreates.
#
# The deploy identity is kept by default: deleting it would mean re-running
# setup-github-oidc.sh and updating the GitHub variables. Pass --all to remove
# it too.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-incident-platform}"
KEEP_IDENTITY=true
[[ "${1:-}" == "--all" ]] && KEEP_IDENTITY=false

echo "About to delete resources in '${RESOURCE_GROUP}':"
az resource list -g "${RESOURCE_GROUP}" --query "[].{name:name, type:type}" -o table

if [[ "${KEEP_IDENTITY}" == true ]]; then
  echo
  echo "Keeping the deploy identity so the GitHub variables stay valid."
fi

read -r -p $'\nType the resource group name to confirm: ' confirm
[[ "${confirm}" == "${RESOURCE_GROUP}" ]] || { echo "Aborted."; exit 1; }

# Deleted in dependency order rather than by dropping the resource group, which
# would take the deploy identity and its role assignments with it.
for name in incident-ingestion incident-env incidentacr incident-logs incident-ingestion-id; do
  az resource list -g "${RESOURCE_GROUP}" --query "[?starts_with(name, '${name}')].id" -o tsv \
    | while read -r id; do
        [[ -n "${id}" ]] || continue
        if [[ "${KEEP_IDENTITY}" == true && "${id}" == *"/incident-github-deploy" ]]; then
          continue
        fi
        echo "==> Deleting ${id##*/}"
        az resource delete --ids "${id}" --output none || echo "    (already gone)"
      done
done

if [[ "${KEEP_IDENTITY}" == false ]]; then
  echo "==> Deleting deploy identity"
  az identity delete -n incident-github-deploy -g "${RESOURCE_GROUP}" --output none || true
fi

echo
echo "Done. Rebuild by re-running the CD workflow:"
echo "  gh workflow run cd.yml"
