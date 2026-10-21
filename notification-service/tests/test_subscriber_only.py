"""Subscriber-only delivery: NOTIFICATION_ACCEPT_LEGACY=false."""
import json

import pytest

from app.notification_main import accept_legacy, build_notifier
from app.notifier import MockNotifier
from app.processor import InvalidIncident, NotificationProcessor
from app.telegram_notifier import TelegramNotifier
from tests.test_telegram_notifier import FakeClock, FakeResponse, FakeSession, make_event


def envelope(chat="555000111"):
    return json.dumps({
        "schema_version": 1, "subscription_id": "sub-1", "channel": "telegram",
        "telegram_chat_id": chat, "incident": json.loads(make_event().model_dump_json()),
    })


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("TRUE", True), ("false", False), (" False ", False)])
def test_legacy_switch_defaults_to_unchanged_behaviour(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("NOTIFICATION_ACCEPT_LEGACY", raising=False)
    else:
        monkeypatch.setenv("NOTIFICATION_ACCEPT_LEGACY", value)
    assert accept_legacy() is expected


async def test_subscriber_only_rejects_flat_messages_and_sends_routed_ones():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier, accept_legacy=False)
    with pytest.raises(InvalidIncident):
        await processor.process(make_event().model_dump_json())  # would have gone to the group chat
    assert notifier.sent_destinations == []
    await processor.process(envelope("555000111"))
    assert notifier.sent_destinations == ["555000111"]


def test_subscriber_only_needs_no_global_chat(monkeypatch):
    monkeypatch.setenv("NOTIFIER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ValueError):
        build_notifier(legacy=True)
    assert isinstance(build_notifier(legacy=False), TelegramNotifier)


async def test_without_a_global_chat_an_unrouted_send_is_refused_before_any_request():
    session = FakeSession([FakeResponse(200, '{"ok":true}')])
    clock = FakeClock()
    notifier = TelegramNotifier(bot_token="test-token", session_factory=lambda: session, sleep=clock.sleep,
                                clock=clock, require_global_chat=False)
    with pytest.raises(ValueError):
        await notifier.notify(make_event())
    assert session.requests == []
    await notifier.notify(make_event(), telegram_chat_id="555000111")
    assert len(session.requests) == 1


@pytest.mark.parametrize("responses", [
    [FakeResponse(200, '{"ok":true}')],
    [FakeResponse(400, '{"ok":false,"description":"Bad Request: chat not found"}')],
])
async def test_send_logs_never_contain_the_bot_token_or_chat_id(caplog, responses):
    session = FakeSession(responses)
    clock = FakeClock()
    notifier = TelegramNotifier(bot_token="123456789:SECRET-token-value", session_factory=lambda: session,
                                sleep=clock.sleep, clock=clock, require_global_chat=False)
    with caplog.at_level("DEBUG"):
        try:
            await notifier.notify(make_event(event_purpose="e2e_test"), telegram_chat_id="555000111")
        except Exception:
            pass  # the failure path is what is being checked
    assert "SECRET-token-value" not in caplog.text and "555000111" not in caplog.text
