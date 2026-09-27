"""Offline tests: python -B -m unittest discover -s scripts -p test_evidence_batch.py -v"""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("batch", Path(__file__).with_name("upload-evidence-batch.py"))
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class BlobCLI:
    def __init__(self, blobs=None, race=False, denied=False, content_type="image/jpeg"):
        self.blobs = blobs or {}
        self.calls = []
        self.race = race
        self.denied = denied
        self.content_type = content_type

    def __call__(self, *args):
        self.calls.append(args)
        if self.denied:
            raise RuntimeError("Azure CLI failed: AuthorizationPermissionMismatch")
        command = args[2]
        name = args[args.index("--name") + 1]
        if command == "exists":
            return {"exists": name in self.blobs}
        if command == "show":
            return {"properties": {"contentLength": len(self.blobs[name]), "etag": '"etag-1"',
                                   "contentSettings": {"contentType": self.content_type}}}
        if command == "download":
            assert args[args.index("--if-match") + 1] == '"etag-1"'
            Path(args[args.index("--file") + 1]).write_bytes(self.blobs[name])
            return {}
        if command == "upload":
            assert args[args.index("--overwrite") + 1] == "false"
            assert args[args.index("--if-none-match") + 1] == "*"
            assert args[args.index("--type") + 1] == "block"
            assert name not in self.blobs
            self.blobs[name] = Path(args[args.index("--file") + 1]).read_bytes()
            if self.race:
                raise RuntimeError("Azure CLI failed: BlobAlreadyExists")
            return {}
        raise AssertionError(args)


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.manifest, cls.payloads = batch.prepare()

    def one(self, cli, mode="upload"):
        manifest = {"images": [self.manifest["images"][0]]}
        return batch.run_batch(manifest, self.payloads, mode, [], cli)[0]

    def existing(self, data=None, **kwargs):
        entry = self.manifest["images"][0]
        data = self.payloads[entry["local_path"]] if data is None else data
        return BlobCLI({entry["blob_path"]: data}, **kwargs)

    def test_full_local_dry_run_never_calls_azure(self):
        def forbidden(*args):
            self.fail("Azure invoked in dry-run")
        results = batch.run_batch(self.manifest, self.payloads, "dry-run", cli=forbidden)
        self.assertEqual(len(results), 43)
        self.assertEqual({r["status"] for r in results}, {"planned"})

    def test_upload_all_43_and_verify_contents(self):
        cli = BlobCLI()
        results = batch.run_batch(self.manifest, self.payloads, "upload", [], cli)
        self.assertEqual(len(cli.blobs), 43)
        self.assertEqual({r["status"] for r in results}, {"successful"})

    def test_check_missing_is_read_only(self):
        cli = BlobCLI()
        self.assertEqual(self.one(cli, "check")["status"], "would_upload")
        self.assertFalse(any(c[2] == "upload" for c in cli.calls))

    def test_existing_match_never_uploads(self):
        cli = self.existing()
        self.assertEqual(self.one(cli)["status"], "already_existing")
        self.assertFalse(any(c[2] == "upload" for c in cli.calls))

    def test_same_size_different_hash_conflicts(self):
        entry = self.manifest["images"][0]
        data = self.payloads[entry["local_path"]]
        cli = self.existing(bytes([data[0] ^ 1]) + data[1:])
        self.assertEqual(self.one(cli)["status"], "conflicting")
        self.assertFalse(any(c[2] == "upload" for c in cli.calls))

    def test_size_conflict_avoids_download(self):
        cli = self.existing(b"different size")
        self.assertEqual(self.one(cli)["status"], "conflicting")
        self.assertFalse(any(c[2] in ("upload", "download") for c in cli.calls))

    def test_matching_content_wrong_mime_is_conflict(self):
        self.assertEqual(self.one(self.existing(content_type="application/octet-stream"))["status"], "conflicting")

    def test_concurrent_create_or_lost_response_is_compared(self):
        self.assertEqual(self.one(BlobCLI(race=True))["status"], "already_existing")

    def test_access_denied_reports_every_image_failed(self):
        results = batch.run_batch(self.manifest, self.payloads, "upload", [], BlobCLI(denied=True))
        self.assertEqual(len(results), 43)
        self.assertEqual({r["status"] for r in results}, {"failed"})

    def test_remote_mode_rejects_local_personal_session_before_cli(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "GitHub Actions"):
                batch.resolve_target(self.manifest, lambda *a: self.fail("CLI called"))

    def test_identity_target_and_privacy_preflight(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main",
               "AZURE_CLIENT_ID": "client", "AZURE_TENANT_ID": "tenant",
               "AZURE_SUBSCRIPTION_ID": "subscription", "RESOURCE_GROUP": "group"}

        def cli(*args):
            if args[:2] == ("account", "show"):
                return {"user": {"type": "servicePrincipal", "name": "client"},
                        "tenantId": "tenant", "id": "subscription"}
            if args[:2] == ("containerapp", "show"):
                return [{"name": "EVIDENCE_BLOB_ENDPOINT", "value": self.manifest["storage"]["blob_endpoint"]},
                        {"name": "EVIDENCE_CONTAINER", "value": "evidence"}]
            self.assertEqual(args[:3], ("storage", "container", "show"))
            self.assertNotIn("--container-name", args)
            self.assertEqual(args[args.index("--name") + 1], "evidence")
            return {"properties": {"publicAccess": None}}

        with patch.dict(os.environ, env, clear=True):
            target = batch.resolve_target(self.manifest, cli)
            self.assertIn("login", target)
            self.assertNotIn("--name", target)
            self.assertEqual(target[target.index("--container-name") + 1], "evidence")
            blob_cli = self.existing()
            batch.run_batch({"images": [self.manifest["images"][0]]},
                            self.payloads, "check", target, blob_cli)
            for call in blob_cli.calls:
                self.assertEqual(call[call.index("--container-name") + 1], "evidence")
            for field in ("blob_endpoint", "container"):
                bad = copy.deepcopy(self.manifest)
                bad["storage"][field] += "wrong"
                with self.assertRaisesRegex(ValueError, "differs"):
                    batch.resolve_target(bad, cli)
            with patch.dict(os.environ, {"AZURE_CLIENT_ID": "wrong"}):
                with self.assertRaisesRegex(ValueError, "identity"):
                    batch.resolve_target(self.manifest, cli)
            with patch.dict(os.environ, {"AZURE_STORAGE_KEY": "forbidden"}):
                with self.assertRaisesRegex(ValueError, "forbidden"):
                    batch.resolve_target(self.manifest, cli)

            def public_cli(*args):
                return {"properties": {"publicAccess": "blob"}} if args[:2] == ("storage", "container") else cli(*args)
            with self.assertRaisesRegex(ValueError, "not private"):
                batch.resolve_target(self.manifest, public_cli)

    def test_parser_error_reports_command_exit_and_missing_name(self):
        result = subprocess.CompletedProcess([], 2, "secret stdout",
                    "ERROR: the following arguments are required: --name/-n\n")
        with patch.object(batch.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                batch.az("storage", "container", "show", "--container-name", "evidence")
        self.assertEqual(str(caught.exception),
                         "az storage container show failed (exit 2): missing required argument --name/-n")

    def test_cli_diagnostics_do_not_echo_secrets(self):
        secret = "https://example.invalid/blob?sig=SECRET_TOKEN"
        for stderr, expected in (
            ("ERROR: unrecognized arguments: " + secret, "unrecognized CLI arguments"),
            ("ERROR: the following arguments are required: " + secret, "missing required CLI arguments"),
            ("ERROR: (AuthenticationFailed) Bearer " + secret, "AuthenticationFailed"),
            ("ErrorCode: SECRET_TOKEN\n" + secret, "CLI details suppressed"),
        ):
            with self.subTest(stderr=stderr):
                result = subprocess.CompletedProcess([], 2, secret, stderr)
                with patch.object(batch.subprocess, "run", return_value=result):
                    with self.assertRaises(RuntimeError) as caught:
                        batch.az("storage", "blob", "show", "--sas-token", secret)
                self.assertEqual(str(caught.exception),
                                 "az storage blob show failed (exit 2): " + expected)

    def test_unknown_command_and_timeout_do_not_echo_arguments(self):
        result = subprocess.CompletedProcess([], 2, "", "secret")
        with patch.object(batch.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, r"az \[unrecognized command\] failed \(exit 2\): CLI details suppressed"):
                batch.az("SECRET_TOKEN")
        error = subprocess.TimeoutExpired(["az", "SECRET_TOKEN"], 120, stderr="secret")
        with patch.object(batch.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                batch.az("account", "show")
        self.assertEqual(str(caught.exception), "az account show timed out after 120s (exit unavailable)")

    def test_bad_hash_duplicate_missing_file_and_uri_fail_preflight(self):
        for mutation in ("hash", "duplicate", "missing", "uri"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=batch.ROOT) as temporary:
                bad = copy.deepcopy(self.manifest)
                if mutation == "hash":
                    bad["images"][0]["image_sha256"] = "0" * 64
                elif mutation == "duplicate":
                    bad["images"][1] = copy.deepcopy(bad["images"][0])
                elif mutation == "missing":
                    bad["images"][0]["local_path"] = "evidence_images/intrusion/missing.jpg"
                else:
                    bad["images"][0]["event"]["evidence_uri"] += "?sig=forbidden"
                path = Path(temporary) / "manifest.json"
                path.write_text(json.dumps(bad), encoding="utf-8")
                with patch.object(batch, "MANIFEST", path), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "validation failed"):
                        batch.prepare()


if __name__ == "__main__":
    unittest.main()
