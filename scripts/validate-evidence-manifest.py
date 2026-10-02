"""Read-only Phase 1 validation. Run: python scripts/validate-evidence-manifest.py

Requires the Common Event Model: pip install -r scripts/requirements.txt. No
Azure access, uploads, or writes. Manual label transcriptions are checked for
consistency, not independently OCR-verified. An optional manifest path supports
local review.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

from common_event_model import IncidentEvent

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "evidence_images/manifest.json"
BASELINE_SHA256 = "16ec163c31f090cb40b5d668cd13e226f9f9b6c17ffd7da4b2d49bc184fa895a"
CATEGORIES = {"intrusion": 14, "people_counting": 14, "fire_smoke": 15}


def validate(manifest_path):
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    original_bytes = BASELINE.read_bytes()
    original = json.loads(original_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check(hashlib.sha256(original_bytes).hexdigest() == BASELINE_SHA256,
          "Existing manifest.json differs from the Phase 1 baseline")
    check(manifest["baseline"] == {"local_path": "evidence_images/manifest.json",
                                   "sha256": BASELINE_SHA256}, "Baseline provenance differs")
    check(manifest["manifest_version"] == 2, "Manifest version must be 2")
    check(manifest["status"] == "proposed_not_uploaded", "Incorrect proposal status")
    check(manifest["storage"] == original["storage"], "Storage configuration changed")
    storage = manifest["storage"]
    endpoint = storage["blob_endpoint"].rstrip("/")
    check(endpoint == f'https://{storage["account_name"]}.blob.core.windows.net',
          "Storage account and endpoint differ")
    check(storage["container"] == "evidence", "Unexpected evidence container")

    model = IncidentEvent
    entries = manifest["images"]
    check(len(entries) == 43, "Expected exactly 43 records")
    base_by_path = {e["local_path"]: e for e in original["images"]}
    actual_paths = {p.relative_to(ROOT).as_posix()
                    for category in CATEGORIES
                    for p in (ROOT / "evidence_images" / category).rglob("*") if p.is_file()}
    local_paths = [e["local_path"] for e in entries]
    check(set(local_paths) == actual_paths == set(base_by_path),
          "Image coverage differs from disk or original manifest")
    for label, values in (
        ("local paths", local_paths),
        ("event IDs", [e["event"]["event_id"] for e in entries]),
        ("Blob paths", [e["blob_path"] for e in entries]),
        ("evidence URIs", [e["event"]["evidence_uri"] for e in entries]),
    ):
        check(len(values) == len(set(values)), f"Duplicate {label}")

    categories = Counter()
    classifications = Counter()
    issues = Counter()
    sources = defaultdict(set)
    model_valid = 0
    for entry in entries:
        local_path = entry["local_path"]
        prefix = local_path + ": "
        path = PurePosixPath(local_path)
        check(not path.is_absolute() and ".." not in path.parts and "\\" not in local_path,
              prefix + "Local path must be repository-relative POSIX form")
        disk_path = (ROOT / local_path).resolve()
        check(disk_path.is_relative_to(ROOT), prefix + "Path escapes repository")
        if not disk_path.is_relative_to(ROOT) or not disk_path.is_file():
            check(False, prefix + "Image is missing or unsafe")
            continue
        check(entry["filename"] == path.name, prefix + "Filename mismatch")
        check(hashlib.sha256(disk_path.read_bytes()).hexdigest() == entry["image_sha256"],
              prefix + "Image has changed since visual review")
        category = path.parts[1]
        categories[category] += 1
        event = entry["event"]
        baseline = base_by_path.get(local_path)
        check(baseline is not None, prefix + "No original record")
        if baseline:
            for field in ("event_id", "evidence_uri"):
                check(event[field] == baseline["event"][field], prefix + field + " changed")
            check(entry["blob_path"] == baseline["blob_path"], prefix + "Blob path changed")
        check(set(event) == set(model.model_fields), prefix + "Common Event Model field mismatch")
        try:
            parsed = model.model_validate(event)
            model_valid += 1
            check(parsed.timestamp.utcoffset() == timedelta(0), prefix + "Event time is not UTC")
        except Exception as exc:
            check(False, prefix + "Common Event Model rejected event: " + str(exc))
        check(event["timestamp"].endswith("Z"), prefix + "UTC timestamp must use Z")
        check(event["tenant_id"] == "TENANT-DEV", prefix + "Wrong tenant")
        check(event["event_purpose"] == "e2e_test", prefix + "Wrong event purpose")
        check(event["source_type"] == "video_analytics", prefix + "Wrong source type")
        check(event["severity"] in {"low", "medium", "high"}, prefix + "Invalid severity")
        check(event["confidence"] == 0.95, prefix + "Expected documented simulated confidence")
        for field in ("site_id", "source_id", "source_name", "description"):
            check(isinstance(event[field], str) and bool(event[field].strip()), prefix + field + " is empty")
        check(event["source_id"] in event["source_name"], prefix + "Source name omits camera ID")
        expected_uri = f'{endpoint}/{storage["container"]}/{entry["blob_path"]}'
        check(event["evidence_uri"] == expected_uri, prefix + "URI does not match Blob path")
        uri = urlsplit(event["evidence_uri"])
        check(not uri.query and not uri.fragment and not uri.username, prefix + "URI contains credentials or suffix")
        check(entry["blob_path"] == f'TENANT-DEV/{category}/{event["event_id"]}.jpg',
              prefix + "Blob naming convention mismatch")

        review = entry["label_review"]
        labels = review["visible_labels"]
        provenance = entry["provenance"]
        check(review["method"] == "manual_visual_inspection", prefix + "Missing visual review")
        check(labels["camera_id"] == event["source_id"], prefix + "Visible camera conflicts with event")
        camera_in_filename = re.search(r"CAM-\d+", path.name)
        if camera_in_filename:
            check(camera_in_filename.group() == event["source_id"], prefix + "Filename camera conflict")
        check(labels["timestamp_timezone"] is None, prefix + "Timezone must not be inferred")
        visible_time = datetime.strptime(labels["timestamp"], "%Y-%m-%d %H:%M:%S")
        event_time = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        check(visible_time != event_time.replace(tzinfo=None), prefix + "Expected documented simulated time difference")
        check(review["timestamp_matches_event"] is False, prefix + "Timestamp difference not recorded")
        check(review["camera_matches_event"] is True and review["incident_label_matches_event"] is True,
              prefix + "Label agreement flags incorrect")
        check(review["visible_timestamp_verification"] == "unverified_timezone_not_present",
              prefix + "Visible time incorrectly verified")
        check(provenance["timestamp_kind"] == "simulated_event_time" and
              provenance["confidence_origin"] == "simulated_test_value" and
              provenance["synthetic"] is True, prefix + "Simulation provenance missing")
        codes = {i["code"] for i in review["issues"] if i["status"] == "unresolved"}
        expected_codes = {"capture_time_unverified"}
        if category == "intrusion":
            expected_codes.add("shared_camera_location_ambiguity")
            expected = ("intrusion_detection", "intrusion_detected")
            check(labels["incident_label"].upper() == "INTRUSION DETECTED", prefix + "Wrong intrusion label")
            check(event["source_id"] == "CAM-01", prefix + "Embedded CAM-01 must be retained")
            sources[event["source_id"]].add(labels["location_label"])
        elif category == "people_counting":
            expected = ("people_counting", "people_count_observed")
            check(labels["incident_label"] == f'People Count: {labels["people_count_label_value"]}',
                  prefix + "Count label mismatch")
        elif category == "fire_smoke":
            lookup = {"FIRE DETECTED": "fire_detected", "SMOKE DETECTED": "smoke_detected"}
            expected = ("fire_smoke_detection", lookup.get(labels["incident_label"]))
        else:
            expected = (None, None)
            check(False, prefix + "Unknown category")
        check((event["analytics_type"], event["event_type"]) == expected,
              prefix + "Category/visible label/event classification conflict")
        check(codes == expected_codes, prefix + "Unresolved issues missing or unexpected")
        issues.update(codes)
        classifications[event["event_type"]] += 1

    check(dict(categories) == CATEGORIES, "Incorrect category counts")
    check(sources["CAM-01"] == {"Perimeter Gate", "Perimeter Entrance", "Perimeter - North Gate", "PERIMETER"},
          "Intrusion location ambiguity is not fully represented")
    print(f'Common Event Model: {model_valid}/{len(entries)} records valid')
    print('Category counts: ' + json.dumps(dict(categories), sort_keys=True))
    print('Event types: ' + json.dumps(dict(classifications), sort_keys=True))
    print('Unresolved review issues (record counts): ' + json.dumps(dict(issues), sort_keys=True))
    print('Manual review covers camera, incident/count headline, timestamp and intrusion location labels.')
    print('Limitations: no independent OCR, timezone verification, live Azure or Blob existence checks.')
    if errors:
        for error in errors:
            print('ERROR: ' + error)
        print(f'FAIL: {len(errors)} validation error(s)')
        return 1
    print('PASS: 43 files present and hashed; IDs, Blob paths and URIs unique and preserved.')
    print('PASS: original manifest SHA-256 unchanged; existing storage target preserved.')
    print('PASS with documented uncertainties; no unresolved camera-ID or event-classification conflicts.')
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path,
                        default=ROOT / "evidence_images/manifest.proposed.json")
    args = parser.parse_args()
    try:
        sys.exit(validate(args.manifest))
    except (OSError, ValueError, KeyError, TypeError, IndexError, ImportError) as exc:
        print(f'FAIL: cannot validate manifest: {exc}', file=sys.stderr)
        sys.exit(1)
