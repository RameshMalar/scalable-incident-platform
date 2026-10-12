"""Offline checks of compiled infrastructure against pre-phase tracked files."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent


def baseline(name, compiler):
    source = subprocess.check_output(["git", "show", "HEAD:infra/" + name + ".bicep"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="phase3b-bicep-") as directory:
        path = Path(directory) / (name + ".bicep")
        path.write_bytes(source)
        result = subprocess.check_output([compiler, "build", str(path), "--no-restore", "--stdout"])
        return json.loads(result)


def compiled(name):
    return json.loads((ARTIFACTS / (name + "-phase3b.json")).read_text(encoding="utf-8-sig"))


def resources(template):
    return {(r["type"], r["name"]): r for r in template["resources"]}


def main(compiler):
    old_core, core = baseline("core", compiler), compiled("core")
    old_resources, new_resources = resources(old_core), resources(core)
    for key, resource in old_resources.items():
        assert new_resources[key] == resource, f"Existing resource changed: {key}"
    for key, output in old_core["outputs"].items():
        assert core["outputs"][key] == output, f"Existing output changed: {key}"
    assert core["parameters"] == old_core["parameters"]
    added = [value for key, value in new_resources.items() if key not in old_resources]
    assert len(added) == 4
    container = next(r for r in added if r["type"].endswith("/containers"))
    assert container["properties"]["resource"]["partitionKey"] == {"paths": ["/tenant_id"], "kind": "Hash"}
    assert container["properties"]["resource"]["id"] == "[variables('cosmosSubscriptionsContainerName')]"
    assert core["variables"]["cosmosSubscriptionsContainerName"] == "subscriptions"
    assert "options" not in container["properties"]
    assert "defaultTtl" not in container["properties"]["resource"]
    assignment = next(r for r in added if r["type"].endswith("/sqlRoleAssignments"))
    assert "/dbs/" in assignment["properties"]["scope"] and "/colls/" in assignment["properties"]["scope"]
    assert set(core["outputs"]) - set(old_core["outputs"]) == {"subscriptionIdentityId", "subscriptionIdentityClientId", "cosmosSubscriptionsContainerName"}
    print("PASS: all existing core resources, outputs and parameters preserved; four additive resources; shared throughput, partition key, no TTL and container RBAC checked")

    old_worker, worker = baseline("worker", compiler), compiled("worker")
    normalized = copy.deepcopy(worker)
    del normalized["parameters"]["cosmosSubscriptionsContainer"]
    env = normalized["resources"][0]["properties"]["template"]["containers"][0]["env"]
    entries = [e for e in env if e["name"] == "COSMOS_SUBSCRIPTIONS_CONTAINER"]
    assert entries == [{"name": "COSMOS_SUBSCRIPTIONS_CONTAINER", "value": "[parameters('cosmosSubscriptionsContainer')]"}]
    env.remove(entries[0])
    for field in ["parameters", "resources", "outputs"]:
        assert normalized[field] == old_worker[field], f"Unexpected Worker change: {field}"
    print("PASS: Worker changes limited to one defaulted parameter and one env entry; persistence, identity, scaling and messaging preserved")

    api = compiled("subscription")
    app = api["resources"][0]
    config = app["properties"]["configuration"]
    assert config["ingress"]["external"] is True
    assert config["ingress"]["allowInsecure"] is False
    assert config["ingress"]["targetPort"] == 8000
    container = app["properties"]["template"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AUTH_MODE"] == "entra" and env["SUBSCRIPTION_STORE"] == "cosmos"
    assert set(env) == {"AZURE_CLIENT_ID", "SUBSCRIPTION_STORE", "COSMOS_ENDPOINT", "COSMOS_DATABASE", "COSMOS_SUBSCRIPTIONS_CONTAINER", "AUTH_MODE", "AUTH_TENANT_ID", "AUTH_AUDIENCE", "TENANT_MAP"}
    for name in ["AUTH_TENANT_ID", "AUTH_AUDIENCE", "TENANT_MAP"]:
        assert env[name].startswith("[parameters(")
    assert "defaultValue" not in api["parameters"]["authAudience"]
    assert "defaultValue" not in api["parameters"]["tenantMap"]
    assert api["parameters"]["maxReplicas"]["allowedValues"] == [1]
    assert container["resources"] == {"cpu": "[json('0.5')]", "memory": "1Gi"}
    probes = {p["type"]: p for p in container["probes"]}
    assert probes["Liveness"]["httpGet"] == {"path": "/health", "port": 8000}
    assert probes["Readiness"]["httpGet"] == {"path": "/ready", "port": 8000}
    assert probes["Readiness"]["timeoutSeconds"] > 5
    assert set(api["outputs"]) == {"appName", "url", "latestRevisionName"}
    for field in ["environmentId", "identityId", "identityClientId", "cosmosEndpoint", "cosmosDatabase", "cosmosSubscriptionsContainer"]:
        assert field in api["parameters"]
    # The actual startup and application configuration must match the template.
    dockerfile = (ROOT / "subscription-service/Dockerfile").read_text()
    assert '"app.main:app"' in dockerfile and '"8000"' in dockerfile and "USER appuser" in dockerfile
    source = "\n".join((ROOT / "subscription-service/app" / name).read_text() for name in ["auth.py", "store.py"])
    assert all(name in source for name in set(env) - {"AZURE_CLIENT_ID"})
    assert "COSMOS_SUBSCRIPTIONS_CONTAINER" in (ROOT / "worker-service/app/subscription_store.py").read_text()
    print("PASS: API module contracts, Docker startup, production auth, HTTPS ingress, env names, probes and bounded resources checked")


if __name__ == "__main__":
    main(sys.argv[1])
