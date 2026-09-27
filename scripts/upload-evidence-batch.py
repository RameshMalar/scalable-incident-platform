"""Controlled evidence upload. Default dry-run is local and never invokes Azure.

check/upload require GitHub Actions main and the existing azure/login OIDC
service-principal session. No login, key lookup, incident API or notification API.
"""

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evidence_images/manifest.proposed.json"
STATUSES = ("successful", "already_existing", "conflicting", "failed", "would_upload", "planned")


def az(*args):
    result = subprocess.run(["az", *args, "--only-show-errors", "--output", "json"],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        # Do not dump CLI diagnostics that could contain tokens or signed URLs.
        code = re.search(r"(?:ErrorCode:|ERROR: \()\s*([A-Za-z0-9_]+)", result.stderr)
        raise RuntimeError("Azure CLI failed: " + (code.group(1) if code else f"exit {result.returncode}"))
    return json.loads(result.stdout) if result.stdout.strip() else None


def prepare():
    spec = importlib.util.spec_from_file_location("evidence_validator", ROOT / "scripts/validate-evidence-manifest.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    if validator.validate(MANIFEST):
        raise ValueError("Manifest validation failed; no Azure calls made")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Snapshot and rehash every file before any network access. Upload these exact bytes.
    payloads = {}
    for entry in manifest["images"]:
        data = (ROOT / entry["local_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["image_sha256"]:
            raise ValueError("Image changed after validation: " + entry["local_path"])
        payloads[entry["local_path"]] = data
    return manifest, payloads


def resolve_target(manifest, cli=az):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Remote modes require the GitHub Actions main-branch OIDC workflow")
    for key in ("AZURE_STORAGE_KEY", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_SAS_TOKEN"):
        if os.environ.get(key):
            raise ValueError("Storage key/SAS environment configuration is forbidden")
    required = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "RESOURCE_GROUP")
    if any(not os.environ.get(k) for k in required):
        raise ValueError("Existing Azure identity/resource-group variables are required")
    account = cli("account", "show")
    expected = os.environ
    if (account["user"]["type"] != "servicePrincipal"
            or account["user"]["name"].lower() != expected["AZURE_CLIENT_ID"].lower()
            or account["tenantId"].lower() != expected["AZURE_TENANT_ID"].lower()
            or account["id"].lower() != expected["AZURE_SUBSCRIPTION_ID"].lower()):
        raise ValueError("Azure CLI session does not match the existing OIDC identity")
    env = cli("containerapp", "show", "--resource-group", expected["RESOURCE_GROUP"],
              "--name", "incident-notification", "--query", "properties.template.containers[0].env")
    values = {item["name"]: item.get("value") for item in env}
    endpoint = (values.get("EVIDENCE_BLOB_ENDPOINT") or "").rstrip("/")
    container = values.get("EVIDENCE_CONTAINER") or "evidence"
    storage = manifest["storage"]
    if endpoint != storage["blob_endpoint"].rstrip("/") or container != storage["container"]:
        raise ValueError("Deployed evidence endpoint/container differs from the manifest; refusing all writes")
    target = ["--auth-mode", "login", "--account-name", storage["account_name"],
              "--blob-endpoint", endpoint, "--container-name", container]
    properties = cli("storage", "container", "show", *target)
    if properties["properties"].get("publicAccess") not in (None, "None"):
        raise ValueError("Evidence container is not private; refusing all writes")
    return target


def compare_existing(entry, data, target, directory, cli=az):
    args = [*target, "--name", entry["blob_path"]]
    blob = cli("storage", "blob", "show", *args)
    properties = blob["properties"]
    if properties["contentLength"] != len(data):
        return "conflicting", "Existing blob size differs; no write attempted"
    download = directory / "remote.jpg"
    cli("storage", "blob", "download", *args, "--file", str(download),
        "--overwrite", "true", "--if-match", properties["etag"], "--no-progress")
    # Do not trust user-set SHA metadata; hash the actual ETag-guarded contents.
    if hashlib.sha256(download.read_bytes()).hexdigest() != entry["image_sha256"]:
        return "conflicting", "Existing blob SHA-256 differs; no write attempted"
    if properties.get("contentSettings", {}).get("contentType") != "image/jpeg":
        return "conflicting", "Bytes match, but Content-Type is not image/jpeg; left unchanged"
    return "already_existing", "SHA-256 matches downloaded contents; image/jpeg confirmed"


def process_entry(entry, data, mode, target, directory, cli=az):
    args = [*target, "--name", entry["blob_path"]]
    if cli("storage", "blob", "exists", *args)["exists"]:
        return compare_existing(entry, data, target, directory, cli)
    if mode == "check":
        return "would_upload", "Blob absent; read-only check"
    staged = directory / "upload.jpg"
    staged.write_bytes(data)
    try:
        cli("storage", "blob", "upload", *args, "--file", str(staged),
            "--type", "block", "--overwrite", "false", "--if-none-match", "*",
            "--content-type", "image/jpeg", "--metadata", "sha256=" + entry["image_sha256"],
            "event_id=" + entry["event"]["event_id"], "--no-progress")
    except (RuntimeError, subprocess.TimeoutExpired):
        # Concurrent create or lost response: inspect actual content, never retry an overwrite.
        if cli("storage", "blob", "exists", *args)["exists"]:
            status, detail = compare_existing(entry, data, target, directory, cli)
            return status, "After create race/uncertain response: " + detail
        raise
    status, detail = compare_existing(entry, data, target, directory, cli)
    if status != "already_existing":
        return status, "Post-upload verification: " + detail
    return "successful", "Created without overwrite; downloaded SHA-256 verified"


def run_batch(manifest, payloads, mode, target=None, cli=az):
    results = []
    with tempfile.TemporaryDirectory(prefix=".evidence-batch-", dir=ROOT) as temporary:
        directory = Path(temporary)
        for entry in manifest["images"]:
            try:
                if mode == "dry-run":
                    status, detail = "planned", "Locally validated; Azure not contacted"
                else:
                    status, detail = process_entry(entry, payloads[entry["local_path"]], mode, target, directory, cli)
            except Exception as exc:
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
            results.append({"event_id": entry["event"]["event_id"], "local_path": entry["local_path"],
                            "blob_path": entry["blob_path"], "status": status, "detail": detail})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "check", "upload"), default="dry-run")
    parser.add_argument("--report", type=Path, help="New JSON report path (must not already exist)")
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error("Report already exists; refusing to overwrite it")
    summary = {"mode": args.mode, "results": [], "preflight_error": None}
    manifest = None
    try:
        manifest, payloads = prepare()
        target = None if args.mode == "dry-run" else resolve_target(manifest)
        summary["results"] = run_batch(manifest, payloads, args.mode, target)
    except Exception as exc:
        summary["preflight_error"] = f"{type(exc).__name__}: {exc}"
        if manifest:
            summary["results"] = [{"event_id": e["event"]["event_id"], "local_path": e["local_path"],
                                   "blob_path": e["blob_path"], "status": "failed",
                                   "detail": "Preflight blocked all writes"} for e in manifest["images"]]
    counts = Counter(r["status"] for r in summary["results"])
    summary["counts"] = {key: counts[key] for key in STATUSES}
    print(json.dumps(summary, indent=2))
    if args.report:
        with args.report.open("x", encoding="utf-8") as report:
            json.dump(summary, report, indent=2)
            report.write("\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as step:
            step.write("\nEvidence batch (" + args.mode + ")\n\n")
            step.write("| Result | Images |\n| --- | ---: |\n")
            for key, count in summary["counts"].items():
                step.write(f"| {key} | {count} |\n")
            if summary["preflight_error"]:
                step.write("\nPreflight failed; no uploads attempted. See JSON report/log.\n")
    return int(bool(summary["preflight_error"] or counts["conflicting"] or counts["failed"]))


if __name__ == "__main__":
    sys.exit(main())
