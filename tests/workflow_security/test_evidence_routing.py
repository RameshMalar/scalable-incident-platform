"""Evidence preflight tests; no cloud or notification IO."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("evidence_gate", ROOT / "scripts/verify-evidence-routing.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def record(**changes):
    return SimpleNamespace(**({"tenant_id": "T", "event_type": "intrusion_detected", "enabled": True, "channel": "telegram", "telegram_chat_id": "synthetic"} | changes))


@pytest.mark.parametrize("records,approval,success", [([record()], True, True), ([], True, False), ([record(enabled=False)], True, False), ([record(event_type="other")], True, False), ([record(tenant_id="other")], True, False), ([record()], False, False), ([record(),record()], True, False)])
def test_evidence_requires_exact_approved_route(records, approval, success):
    reader = AsyncMock(find_matches=AsyncMock(return_value=records))
    policy = AsyncMock(approved=AsyncMock(return_value=approval))
    if success: asyncio.run(gate.require_route(reader, policy, "T"))
    else:
        with pytest.raises(ValueError): asyncio.run(gate.require_route(reader, policy, "T"))
    reader.find_matches.assert_awaited_once_with("T", "intrusion_detected")


def test_policy_outage_blocks_event():
    with pytest.raises(RuntimeError):
        asyncio.run(gate.require_route(AsyncMock(find_matches=AsyncMock(return_value=[record()])), AsyncMock(approved=AsyncMock(side_effect=RuntimeError("private"))), "T"))


def test_tenant_mapping():
    gate.verify_tenant("client:T", "client", "T")
    with pytest.raises(ValueError): gate.verify_tenant("client:other", "client", "T")


def test_workflow_gate_precedes_post_and_photo_timeout_fails():
    workflow = yaml.safe_load((ROOT / ".github/workflows/evidence.yml").read_text())
    steps = workflow["jobs"]["upload"]["steps"]
    gate_index = next(i for i,s in enumerate(steps) if s.get("run") == "python3 scripts/verify-evidence-routing.py")
    post_index = next(i for i,s in enumerate(steps) if s.get("id") == "alert")
    assert gate_index < post_index
    assert steps[gate_index]["if"] == steps[post_index]["if"] == "inputs.send_test_alert"
    assert "::add-mask::$token" in steps[post_index]["run"]
    photo = next(s["run"] for s in steps if s.get("name") == "Confirm Telegram delivered a photo")
    assert "method=sendPhoto" in photo and photo.rstrip().endswith("exit 1")
    source = (ROOT / "scripts/verify-evidence-routing.py").read_text()
    assert "create_item" not in source and "telegram.org" not in source
