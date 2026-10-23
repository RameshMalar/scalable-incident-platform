"""Offline tests: python -m pytest scripts/test_telegram_demo_subscribers.py

The records must pass the real Worker and Subscription API validators, and
nothing may be written before the member proves chat ownership.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("demo_subscribers", Path(__file__).with_name("telegram-demo-subscribers.py"))
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_records_match_what_worker_and_api_accept():
    sub = tool.subscription_record("TENANT-01", "555000111", "intrusion_detected")
    dest = tool.destination_record("TENANT-01", "555000111", "Malar")
    assert sub == {**sub, "owner_object_id": None, "channel": "telegram", "enabled": True}
    assert sub["id"] == tool.subscription_record("TENANT-01", "555000111", "intrusion_detected")["id"]  # idempotent
    assert sub["id"] != tool.subscription_record("TENANT-01", "555000111", "fire_detected")["id"]
    # The Worker's destination check (same rule in worker and Subscription API).
    assert all(key in dest for key in ("id", "tenant_id", "channel", "telegram_chat_id", "enabled"))
    assert dest["id"] == "telegram:" + dest["telegram_chat_id"] and type(dest["enabled"]) is bool
    sys.path.insert(0, str(ROOT / "packages" / "common-event-model" / "src"))
    try:
        sys.path.insert(0, str(ROOT / "worker-service"))
        from app.notification_models import SubscriptionRecord
        SubscriptionRecord.model_validate(sub)
    except ImportError:
        pytest.skip("worker-service dependencies not installed")
    finally:
        sys.path[:] = [p for p in sys.path if not p.endswith(("worker-service", "src"))]
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
    sys.path.insert(0, str(ROOT / "subscription-service"))
    try:
        from app.models import Subscription
        Subscription.model_validate(sub)  # API list/get must still read script-created records
    except ImportError:
        pytest.skip("subscription-service dependencies not installed")
    finally:
        sys.path.remove(str(ROOT / "subscription-service"))
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]


def update(text, chat_type="private", chat_id=555000111):
    return {"message": {"text": text, "chat": {"id": chat_id, "type": chat_type, "first_name": "M"}}}


def test_only_the_exact_code_from_a_private_chat_proves_ownership():
    assert tool.find_start([update("/start LML-ABC123")], "LML-ABC123")["chat_id"] == "555000111"
    assert tool.find_start([update("/start LML-ABC123", chat_type="group")], "LML-ABC123") is None
    assert tool.find_start([update("/start LML-WRONG1")], "LML-ABC123") is None
    assert tool.find_start([update("hello"), {"edited_message": {}}], "LML-ABC123") is None


class FakeTelegram:
    def __init__(self, updates, webhook=""):
        self.updates, self.webhook, self.calls = updates, webhook, []

    def call(self, method, **params):
        self.calls.append(method)
        return {"getWebhookInfo": {"url": self.webhook}, "getUpdates": self.updates}.get(method, {})


def test_enrolment_times_out_without_writing_and_never_touches_a_webhook():
    ticks = iter([0, 0, 1000])
    with pytest.raises(SystemExit, match="Nothing was written"):
        tool.wait_for_start(FakeTelegram([]), "LML-ABC123", timeout=10, clock=lambda: next(ticks))
    hooked = FakeTelegram([], webhook="https://example/hook")
    with pytest.raises(SystemExit, match="webhook"):
        tool.wait_for_start(hooked, "LML-ABC123")
    assert hooked.calls == ["getWebhookInfo"]  # no getUpdates, no deleteWebhook


def test_enrol_writes_approval_then_subscriptions_then_confirms(monkeypatch, capsys):
    telegram = FakeTelegram([update("/start LML-AAAAAA")])
    sent = []
    telegram.call = (lambda original: lambda method, **p: (sent.append((method, p)), {"getMe": {"username": "lml_bot"}}.get(method) or original(method, **p))[1])(telegram.call)
    monkeypatch.setattr(tool, "Telegram", lambda token: telegram)
    monkeypatch.setattr(tool, "bot_token", lambda: "secret-token")
    monkeypatch.setattr(tool.secrets, "token_hex", lambda n: "AAAAAA")
    containers = {"approved-destinations": Mock(), "subscriptions": Mock()}
    monkeypatch.setattr(tool, "database", lambda: SimpleNamespace(get_container_client=containers.__getitem__))
    tool.main(["enrol", "--tenant", "TENANT-01", "--label", "Malar", "--events", "intrusion_detected,fire_detected"])
    dest = containers["approved-destinations"].upsert_item.call_args.args[0]
    assert (dest["tenant_id"], dest["telegram_chat_id"], dest["enabled"], dest["label"]) == ("TENANT-01", "555000111", True, "Malar")
    events = [c.args[0]["event_type"] for c in containers["subscriptions"].upsert_item.call_args_list]
    assert events == ["intrusion_detected", "fire_detected"]
    assert sent[-1][0] == "sendMessage" and sent[-1][1]["chat_id"] == "555000111"
    out = capsys.readouterr().out
    assert "secret-token" not in out and "555000111" not in out and "…111" in out


@pytest.mark.parametrize("argv", [
    ["enrol", "--tenant", "tenant-01", "--label", "M", "--events", "intrusion_detected"],
    ["enrol", "--tenant", "TENANT-01", "--label", "<script>", "--events", "intrusion_detected"],
    ["remove", "--tenant", "TENANT-01", "--chat-suffix", "5"],
])
def test_inputs_are_validated_before_any_azure_call(monkeypatch, argv):
    monkeypatch.setattr(tool, "database", Mock(side_effect=AssertionError("Azure called")))
    monkeypatch.setattr(tool, "bot_token", Mock(side_effect=AssertionError("Key Vault called")))
    with pytest.raises(SystemExit):
        tool.main(argv)


def test_remove_disables_exactly_one_chat(monkeypatch):
    rows = {"approved-destinations": [{"id": "telegram:-4800000508", "tenant_id": "TENANT-01", "telegram_chat_id": "-4800000508", "enabled": True, "_etag": "x"}],
            "subscriptions": [{"id": "SUB-1", "tenant_id": "TENANT-01", "event_type": "fire_detected", "telegram_chat_id": "-4800000508", "enabled": True},
                              {"id": "SUB-2", "tenant_id": "TENANT-01", "event_type": "fire_detected", "telegram_chat_id": "555000111", "enabled": True}]}
    containers = {name: Mock(**{"query_items.return_value": data}) for name, data in rows.items()}
    monkeypatch.setattr(tool, "database", lambda: SimpleNamespace(get_container_client=containers.__getitem__))
    tool.main(["remove", "--tenant", "TENANT-01", "--chat-suffix", "508"])
    written = [c.args[0] for c in containers["subscriptions"].upsert_item.call_args_list]
    assert [(w["id"], w["enabled"]) for w in written] == [("SUB-1", False)]
    approval = containers["approved-destinations"].upsert_item.call_args.args[0]
    assert approval["enabled"] is False and "_etag" not in approval
    with pytest.raises(SystemExit, match="exactly one chat"):
        tool.main(["remove", "--tenant", "TENANT-01", "--chat-suffix", "999"])


def test_remove_with_backup_then_restore_puts_the_exact_records_back(monkeypatch, tmp_path):
    original = {"approved-destinations": [{"id": "telegram:-4800000508", "tenant_id": "TENANT-01", "channel": "telegram",
                                           "telegram_chat_id": "-4800000508", "enabled": True, "_etag": "x"}],
                "subscriptions": [{"id": "SUB-1", "tenant_id": "TENANT-01", "event_type": "fire_detected", "channel": "telegram",
                                   "telegram_chat_id": "-4800000508", "enabled": True, "created_at": "c", "updated_at": "u",
                                   "owner_object_id": None, "_rid": "r"}]}
    containers = {name: Mock(**{"query_items.return_value": rows}) for name, rows in original.items()}
    monkeypatch.setattr(tool, "database", lambda: SimpleNamespace(get_container_client=containers.__getitem__))
    backup = tmp_path / "group.json"
    tool.main(["remove", "--tenant", "TENANT-01", "--chat-suffix", "508", "--backup", str(backup)])
    assert containers["subscriptions"].upsert_item.call_args.args[0]["enabled"] is False
    with pytest.raises(FileExistsError):  # never overwrite an earlier backup
        tool.main(["remove", "--tenant", "TENANT-01", "--chat-suffix", "508", "--backup", str(backup)])
    for c in containers.values():
        c.reset_mock()
    tool.main(["restore", "--from", str(backup)])
    restored_sub = containers["subscriptions"].upsert_item.call_args.args[0]
    restored_dest = containers["approved-destinations"].upsert_item.call_args.args[0]
    assert restored_sub == {k: v for k, v in original["subscriptions"][0].items() if not k.startswith("_")}
    assert restored_dest["enabled"] is True and "_etag" not in restored_dest
