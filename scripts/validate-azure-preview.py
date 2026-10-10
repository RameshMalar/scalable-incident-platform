"""Trusted manual-preview validation. Never import or execute candidate code."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
MAX_SIZE = 8 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_inputs(env):
    require(bool(SHA.fullmatch(env.get("SOURCE_SHA", ""))), "Full lowercase 40-character SHA required")
    require(bool(re.fullmatch(r"[1-9][0-9]{0,9}", env.get("PR_NUMBER", ""))), "Explicit positive PR number required")
    reference = env.get("REVIEW_REFERENCE", "")
    require(1 <= len(reference.strip()) <= 500 and not any(ord(c) < 32 for c in reference), "Review reference required; control characters forbidden")


def validate_protection(repo, environment, policies):
    require(repo.get("private") is True, "Preview artifacts require a private repository")
    reviewers = [r for r in environment.get("protection_rules", []) if r.get("type") == "required_reviewers"]
    require(any(r.get("reviewers") and r.get("prevent_self_review") is True for r in reviewers), "Required reviewers and prevent-self-review must be enabled")
    require(environment.get("can_admins_bypass") is False, "Administrator bypass must be disabled")
    branch_policy = environment.get("deployment_branch_policy") or {}
    require(branch_policy.get("custom_branch_policies") is True and branch_policy.get("protected_branches") is False, "Explicit main-only environment policy required")
    rules = policies.get("branch_policies", [])
    require(len(rules) == 1 and rules[0].get("name") == "main" and rules[0].get("type") == "branch", "Only main may enter azure-preview")


REPOSITORY = "RameshMalar/scalable-incident-platform"


def api_read(suffix):
    require(os.environ.get("REPOSITORY") == REPOSITORY, "Unexpected repository")
    request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + suffix,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def protection():
    validate_protection(api_read(""), api_read("/environments/azure-preview"),
                        api_read("/environments/azure-preview/deployment-branch-policies?per_page=100"))


def validate_pr(pr, number, sha):
    require(bool(SHA.fullmatch(sha)), "Invalid candidate SHA")
    require(isinstance(number, str) and bool(re.fullmatch(r"[1-9][0-9]{0,9}", number)), "Invalid PR number")
    require(type(pr.get("number")) is int and pr["number"] == int(number), "PR number mismatch")
    require(pr.get("state") == "open" and pr.get("merged") is False, "Open unmerged PR required")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    require((head.get("repo") or {}).get("full_name") == REPOSITORY, "Fork or missing head repository rejected")
    require((base.get("repo") or {}).get("full_name") == REPOSITORY and base.get("ref") == "main", "Same-repository PR targeting main required")
    require(head.get("sha") == sha, "Candidate must equal current reviewed PR head")
    return {"pr_number": int(number), "repository": REPOSITORY, "source_sha": sha, "base_branch": "main"}


def provenance():
    validate_inputs(os.environ)
    record = validate_pr(api_read("/pulls/" + os.environ["PR_NUMBER"]),
                         os.environ["PR_NUMBER"], os.environ["SOURCE_SHA"])
    Path("verified-provenance.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    # Free-text review references are evidence labels, never permission grants.
    return record


def validate_identity(env):
    for key in ("CLIENT_ID", "TENANT_ID", "SUBSCRIPTION_ID"):
        require(bool(UUID.fullmatch(env.get("AZURE_PREVIEW_" + key, ""))), "Missing or invalid dedicated preview " + key)
    production = env.get("PRODUCTION_CLIENT_ID", "")
    require(bool(UUID.fullmatch(production)), "Production identity ID required for separation check")
    require(env["AZURE_PREVIEW_CLIENT_ID"].lower() != production.lower(), "Preview identity must differ from production")
    group = env.get("AZURE_PREVIEW_RESOURCE_GROUP", "")
    require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,90}", group)), "Invalid preview resource group")
    require(env.get("AZURE_PREVIEW_CONFIGURATION_APPROVED") == "true", "Administrator-reviewed identity/RBAC configuration required")
    principals = json.loads(env.get("AZURE_PREVIEW_DEVELOPER_PRINCIPAL_IDS", ""))
    require(isinstance(principals, list) and all(isinstance(p, str) and UUID.fullmatch(p) for p in principals), "Explicit JSON principal ID array required; [] is allowed")


def validate_source(root):
    require(root.is_dir() and not root.is_symlink(), "Missing candidate infrastructure")
    require(not (root.parent / "bicepconfig.json").exists(), "Candidate root compiler configuration forbidden")
    for path in root.rglob("*"):
        require(not path.is_symlink(), "Candidate symlinks forbidden")
        if path.is_file():
            require(path.stat().st_size <= MAX_SIZE, "Oversized candidate input")
            require(path.name != "bicepconfig.json", "Candidate compiler configuration forbidden")
    require((root / "core.bicep").is_file(), "Missing core template")
    # --no-restore also prevents fetching candidate-selected external Bicep modules.


def validate_template(value):
    require(isinstance(value, dict) and isinstance(value.get("resources"), (list, dict)), "Invalid compiled ARM template")
    def walk(node):
        if isinstance(node, dict):
            resource_type = str(node.get("type", "")).lower()
            require(resource_type != "microsoft.resources/deploymentscripts", "Deployment scripts forbidden")
            require("templatelink" not in {str(k).lower() for k in node}, "External linked templates forbidden")
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)


def read_regular(path):
    require(path.is_file() and not path.is_symlink(), "Missing or non-regular bundle file")
    require(path.stat().st_size <= MAX_SIZE, "Oversized bundle")
    return path.read_bytes()


def verify_bundle(root, sha, expected_hash, reference, provenance_record):
    require(bool(SHA.fullmatch(sha)) and bool(re.fullmatch(r"[0-9a-f]{64}", expected_hash)), "Invalid immutable bundle identifiers")
    require(not root.is_symlink() and {p.name for p in root.iterdir()} == {"core.json", "manifest.json"}, "Unexpected bundle paths")
    template = read_regular(root / "core.json")
    manifest = json.loads(read_regular(root / "manifest.json"))
    require(hashlib.sha256(template).hexdigest() == expected_hash, "Compiled template hash mismatch")
    require(manifest == {"source_sha": sha, "template_hash": expected_hash, "review_reference": reference, "provenance": provenance_record}, "Bundle provenance mismatch")
    validate_template(json.loads(template))


def bundle():
    validate_inputs(os.environ)
    path = Path("bundle/core.json")
    value = json.loads(read_regular(path))
    validate_template(value)
    # Outputs are not needed to predict resource changes; avoid evaluating disclosures.
    value.pop("outputs", None)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha = os.environ["SOURCE_SHA"]
    record = json.loads(read_regular(Path("verified-provenance.json")))
    require(record == {"pr_number": int(os.environ["PR_NUMBER"]), "repository": REPOSITORY,
                       "source_sha": sha, "base_branch": "main"}, "Missing verified PR provenance")
    Path("bundle/manifest.json").write_text(json.dumps({"source_sha": sha, "template_hash": digest, "review_reference": os.environ["REVIEW_REFERENCE"], "provenance": record}), encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"source_sha={sha}\ntemplate_hash={digest}\n")


def summary(path):
    result = json.loads(read_regular(Path(path)))
    require(result.get("status") == "Succeeded", "Preview did not succeed")
    changes = result.get("changes")
    require(isinstance(changes, list), "Preview has no change list")
    allowed = {"Create", "Delete", "Ignore", "NoChange", "NoEffect", "Modify", "Deploy", "Unsupported"}
    require(all(c.get("changeType") in allowed for c in changes), "Unknown preview change type")
    counts = collections.Counter(c["changeType"] for c in changes)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write(f"Source SHA: {os.environ['SOURCE_SHA']}\n\nTemplate SHA-256: {os.environ['TEMPLATE_HASH']}\n\n")
        output.write("Preview completed using ProviderNoRbac. Counts only; raw details withheld.\n\n")
        for kind in sorted(counts):
            output.write(f"- {kind}: {counts[kind]}\n")
        output.write("\nIgnore/Unsupported and unresolved expressions limit analysis. No deployment or runtime verification occurred.\n")


def main():
    mode = sys.argv[1]
    if mode == "inputs": validate_inputs(os.environ)
    elif mode == "protection": protection()
    elif mode == "provenance": provenance()
    elif mode == "identity": validate_identity(os.environ)
    elif mode == "source": validate_source(Path("candidate/infra"))
    elif mode == "bundle": bundle()
    elif mode == "verify": verify_bundle(Path("bundle"), os.environ["SOURCE_SHA"], os.environ["TEMPLATE_HASH"], os.environ["REVIEW_REFERENCE"], json.loads(read_regular(Path("verified-provenance.json"))))
    elif mode == "cli-version": require(tuple(map(int, sys.stdin.read().strip().split(".")[:2])) >= (2, 76), "Azure CLI 2.76+ required")
    elif mode == "summary": summary(sys.argv[2])
    else: raise ValueError("Unknown validation mode")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("::error::Azure preview validation failed; check reviewed inputs and administrator prerequisites", file=sys.stderr)
        sys.exit(1)
