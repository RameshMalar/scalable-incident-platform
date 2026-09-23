from app.notifier import MockNotifier
from app.notification_main import build_notifier
from app.telegram_notifier import TelegramNotifier


def test_build_notifier_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("NOTIFIER", raising=False)

    notifier = build_notifier()

    assert isinstance(notifier, MockNotifier)


def test_build_notifier_uses_telegram(monkeypatch):
    monkeypatch.setenv("NOTIFIER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")

    notifier = build_notifier()

    assert isinstance(notifier, TelegramNotifier)