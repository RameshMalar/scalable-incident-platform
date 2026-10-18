"""Regenerate ingestion-service/app/simulated_evidence.json from the evidence manifest.

/api/v1/simulate picks one of these images at random for each simulated incident.
Every blob comes from evidence_images/manifest.proposed.json, which the Evidence
batch workflow uploads, so each one is known to be in the evidence container.
The camera is the one printed on the image.

Run after the manifest changes; a test fails until the two agree.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evidence_images/manifest.proposed.json"
OUTPUT = ROOT / "ingestion-service/app/simulated_evidence.json"

# The simulate endpoint's analytics types, and the manifest event each draws on.
# There is no PPE image yet; adding one to the manifest is enough to use it.
EVENT_TYPES = {
    "intrusion": "intrusion_detected",
    "crowd": "people_count_observed",
    "ppe": "helmet_missing",
    "fire": "fire_detected",
    "smoke": "smoke_detected",
}


def build(manifest):
    return {
        kind: sorted(
            (
                {"source_id": image["event"]["source_id"], "blob": image["blob_path"]}
                for image in manifest["images"]
                if image["event"]["event_type"] == event_type
            ),
            key=lambda evidence: evidence["blob"],
        )
        for kind, event_type in EVENT_TYPES.items()
    }


def render(manifest):
    return json.dumps(build(manifest), indent=2) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(render(json.loads(MANIFEST.read_text())))
    counts = {kind: len(images) for kind, images in build(json.loads(MANIFEST.read_text())).items()}
    print(f"Wrote {OUTPUT.relative_to(ROOT)}: {counts}")
