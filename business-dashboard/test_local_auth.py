"""Local username/password sign-in (DASHBOARD_AUTH_MODE=local). Offline only."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import auth_preflight  # noqa: E402
import local_auth  # noqa: E402

if "soc_dashboard" in sys.modules:
    dashboard = sys.modules["soc_dashboard"]
else:
    spec = importlib.util.spec_from_file_location("soc_dashboard", ROOT / "app.py")
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules["soc_dashboard"] = dashboard
    spec.loader.exec_module(dashboard)

PASSWORD = "Correct-Horse-Battery-9"  # synthetic test value, not a real credential


def write_credentials(path, password=PASSWORD, testing_only=False, extra=""):
    path.write_text("[local_auth]\nusername = \"admin\"\n"
                    f"password_hash = \"{local_auth.hash_password(password)}\"\n"
                    f"local_testing_only = {'true' if testing_only else 'false'}\n{extra}", encoding="utf-8")
    return path


@pytest.fixture
def local_mode(monkeypatch, tmp_path):
    for name in local_auth.ENTRA_ONLY_FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DASHBOARD_AUTH_MODE", "local")
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTH_FILE", str(write_credentials(tmp_path / "local-auth.toml")))
    monkeypatch.setattr(local_auth, "LIMITER", local_auth.AttemptLimiter())
    monkeypatch.setattr(local_auth, "FAILURE_DELAY_SECONDS", 0)
    monkeypatch.setattr(dashboard, "get_container", Mock(side_effect=AssertionError("unexpected Cosmos read")))
    monkeypatch.setattr(dashboard, "get_evidence_store", Mock(return_value=None))
    return tmp_path


@pytest.fixture
def records():
    df = pd.DataFrame([dict(event_id="E1", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-1",
                            analytics_type="intrusion_detection", event_type="intrusion_detected", severity="high",
                            timestamp="2026-10-19T10:00:00Z", evidence_uri=None)])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df.attrs["refreshed_at"] = "2026-10-19T10:01:00+00:00"
    return df


def run_app():
    return AppTest.from_string("import soc_dashboard as app\napp.main()").run(timeout=20)


def sign_in(at, username, password):
    at.text_input[0].input(username)
    at.text_input[1].input(password)
    at.button[0].click()
    return at.run(timeout=20)


# --- hashing and credentials -------------------------------------------------------

def test_hash_round_trip_uses_unique_salts():
    first, second = local_auth.hash_password(PASSWORD), local_auth.hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$16384$8$1$")
    assert local_auth.verify_password(first, PASSWORD)
    assert not local_auth.verify_password(first, PASSWORD + "x")


@pytest.mark.parametrize("stored", ["", "plain-text", "scrypt$1024$8$1$AAAA$AAAA", "bcrypt$x$y$z$a$b", None,
                                    local_auth.hash_password("x")[:-4] + "!!!!"])
def test_malformed_or_weak_hashes_never_verify(stored):
    assert not local_auth.verify_password(stored, "x")


def test_credentials_file_rules(tmp_path):
    good = write_credentials(tmp_path / "a.toml")
    # A legacy single-account file without a role never grants more than Viewer.
    assert local_auth.load_credentials(str(good)).accounts["admin"].role == "viewer"
    for bad in (tmp_path / "missing.toml",
                write_credentials(tmp_path / "b.toml", extra='password = "plaintext"\n'),
                tmp_path / "c.toml"):
        if bad.name == "c.toml":
            bad.write_text('[local_auth]\nusername = "ad min"\npassword_hash = "scrypt$x"\n', encoding="utf-8")
        with pytest.raises(local_auth.LocalAuthUnavailable):
            local_auth.load_credentials(str(bad))


# --- brute-force protection and sessions -------------------------------------------

def test_rate_limit_per_client_and_overall():
    now = [0.0]
    limiter = local_auth.AttemptLimiter(clock=lambda: now[0])
    for _ in range(local_auth.FAILURES_PER_CLIENT):
        limiter.failed("10.0.0.1")
    assert limiter.blocked("10.0.0.1") and not limiter.blocked("10.0.0.2")
    now[0] += local_auth.WINDOW_SECONDS + 1
    assert not limiter.blocked("10.0.0.1")
    for n in range(local_auth.FAILURES_TOTAL):
        limiter.failed(f"10.1.0.{n}")
    assert limiter.blocked("10.9.9.9")


def test_blocked_client_cannot_sign_in_even_with_correct_password(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(local_auth, "FAILURE_DELAY_SECONDS", 0)
    credentials = local_auth.load_credentials(str(write_credentials(tmp_path / "a.toml")))
    limiter = local_auth.AttemptLimiter()
    for _ in range(local_auth.FAILURES_PER_CLIENT):
        assert not local_auth.attempt_sign_in("admin", "wrong", "1.2.3.4", credentials, limiter)
    assert not local_auth.attempt_sign_in("admin", PASSWORD, "1.2.3.4", credentials, limiter)
    assert local_auth.attempt_sign_in("admin", PASSWORD, "5.6.7.8", credentials, limiter)


def test_sessions_expire_when_idle_or_too_old(monkeypatch):
    state = {}
    monkeypatch.setattr(local_auth, "st", SimpleNamespace(session_state=state))
    state[local_auth.SESSION_KEY] = {"username": "admin", "signed_in_at": 0, "last_seen": 0}
    assert local_auth.current_session(now=local_auth.IDLE_SECONDS - 1) == "admin"
    assert local_auth.current_session(now=2 * local_auth.IDLE_SECONDS) is None and not state
    state[local_auth.SESSION_KEY] = {"username": "admin", "signed_in_at": 0, "last_seen": local_auth.ABSOLUTE_SECONDS}
    assert local_auth.current_session(now=local_auth.ABSOLUTE_SECONDS + 1) is None
    state[local_auth.SESSION_KEY] = {"username": "admin", "signed_in_at": 0, "last_seen": 0}
    local_auth.sign_out()
    assert local_auth.current_session(now=1) is None


def test_client_address_uses_the_ingress_hop():
    assert local_auth.client_address({"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}) == "203.0.113.7"
    assert local_auth.client_address({}) == "local"


def test_password_never_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(local_auth, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(local_auth, "FAILURE_DELAY_SECONDS", 0)
    credentials = local_auth.load_credentials(str(write_credentials(tmp_path / "a.toml")))
    with caplog.at_level("INFO"):
        local_auth.attempt_sign_in("admin", "Wrong-Guess-123", "1.1.1.1", credentials, local_auth.AttemptLimiter())
        local_auth.attempt_sign_in("admin", PASSWORD, "1.1.1.1", credentials, local_auth.AttemptLimiter())
    assert "Wrong-Guess-123" not in caplog.text and PASSWORD not in caplog.text and "scrypt$" not in caplog.text


# --- the dashboard gate -------------------------------------------------------------

def test_no_page_or_data_before_sign_in(local_mode, monkeypatch):
    loader = Mock(side_effect=AssertionError("data loaded before sign-in"))
    monkeypatch.setattr(dashboard, "load_incidents", loader)
    at = run_app()
    assert not at.exception
    assert [b.label for b in at.button] == ["Sign in"] and len(at.text_input) == 2
    assert at.text_input[1].proto.type == at.text_input[1].proto.PASSWORD
    # The page CSS (which names .kpi-card) is injected before the gate; no card is.
    assert not any('class="kpi-card"' in m.value for m in at.markdown)
    assert not any("Security Operations Center" in getattr(t, "value", "") for t in at.title)
    loader.assert_not_called()


def test_wrong_password_is_rejected_without_data(local_mode, monkeypatch):
    loader = Mock(side_effect=AssertionError("data loaded before sign-in"))
    monkeypatch.setattr(dashboard, "load_incidents", loader)
    at = sign_in(run_app(), "admin", "not-the-password")
    assert not at.exception and any("Incorrect username or password" in e.value for e in at.error)
    loader.assert_not_called()


def test_sign_in_shows_dashboard_and_sign_out_returns_to_form(local_mode, monkeypatch, records):
    monkeypatch.setattr(dashboard, "load_incidents", Mock(return_value=records))
    at = sign_in(run_app(), "admin", PASSWORD)
    assert not at.exception
    assert sum(m.value.count('class="kpi-card"') for m in at.markdown) == 8
    assert any(b.proto.label == "Platform Observability" for b in at.get("link_button"))
    sign_out = next(b for b in at.button if b.label == "Sign out")
    sign_out.click().run(timeout=20)
    assert [b.label for b in at.button] == ["Sign in"]


def test_local_context_is_read_only_and_tenant_scoped(local_mode):
    context = dashboard.local_operator_context("admin")
    assert context.tenants == ("TENANT-01", "TENANT-02", "TENANT-03")
    assert context.permissions == frozenset({"Dashboard.Read"})
    assert not context.can_simulate and not context.can_manage_access
    with pytest.raises(dashboard.AccessDenied):
        dashboard.operator_tokens()  # no Entra token exists in local mode


@pytest.mark.parametrize("flag", local_auth.ENTRA_ONLY_FLAGS)
def test_entra_only_features_refuse_to_run_in_local_mode(local_mode, monkeypatch, flag):
    monkeypatch.setenv(flag, "true")
    loader = Mock(side_effect=AssertionError("data loaded"))
    monkeypatch.setattr(dashboard, "load_incidents", loader)
    at = run_app()
    assert any("cannot be combined" in e.value for e in at.error) and not at.button
    loader.assert_not_called()


def test_missing_credentials_deny_access(local_mode, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTH_FILE", str(tmp_path / "absent.toml"))
    at = run_app()
    assert any("not configured" in e.value for e in at.error) and not at.button


# --- deployment safety --------------------------------------------------------------

def test_container_preflight_refuses_test_only_and_weak_hashes(tmp_path):
    base = {"DASHBOARD_AUTH_MODE": "local"}
    strong = write_credentials(tmp_path / "strong.toml")
    assert auth_preflight.check({**base, "DASHBOARD_LOCAL_AUTH_FILE": str(strong)}) == []
    test_only = write_credentials(tmp_path / "test.toml", testing_only=True)
    assert "local_auth_testing_only_hash" in auth_preflight.check({**base, "DASHBOARD_LOCAL_AUTH_FILE": str(test_only)})
    weak = write_credentials(tmp_path / "weak.toml", password=auth_preflight.WEAK_PASSWORDS[0])
    assert "local_auth_weak_password:admin" in auth_preflight.check({**base, "DASHBOARD_LOCAL_AUTH_FILE": str(weak)})
    assert auth_preflight.check({**base, "DASHBOARD_LOCAL_AUTH_FILE": str(tmp_path / "x")}) == ["local_auth_file"]
    flagged = auth_preflight.check({**base, "DASHBOARD_LOCAL_AUTH_FILE": str(strong), "ENABLE_EVENT_SIMULATOR": "true"})
    assert flagged == ["entra_only_feature_enabled:ENABLE_EVENT_SIMULATOR"]


def test_hash_tool_requires_strong_password_unless_test_only(tmp_path):
    tool = ROOT / "tools" / "make_password_hash.py"

    def run(*args, password):
        return subprocess.run([sys.executable, str(tool), *args], input=password + "\n", capture_output=True, text=True)

    weak = run("--password-stdin", "--out", str(tmp_path / "w.toml"), password="short")
    assert weak.returncode != 0 and not (tmp_path / "w.toml").exists()
    ok = run("--password-stdin", "--out", str(tmp_path / "s.toml"), password=PASSWORD)
    assert ok.returncode == 0 and PASSWORD not in ok.stdout + ok.stderr
    assert local_auth.load_credentials(str(tmp_path / "s.toml")).testing_only is False
    generated = subprocess.run([sys.executable, str(tool), "--generate", str(tmp_path / "pw.txt"), "--out", str(tmp_path / "g.toml")],
                               capture_output=True, text=True)
    secret = (tmp_path / "pw.txt").read_text(encoding="utf-8").strip()
    assert generated.returncode == 0 and len(secret) == 24 and secret not in generated.stdout + generated.stderr
    generated_file = local_auth.load_credentials(str(tmp_path / "g.toml"))
    assert local_auth.verify_password(generated_file.accounts["admin"].password_hash, secret)
    assert generated_file.testing_only is False
    assert secret not in (tmp_path / "g.toml").read_text(encoding="utf-8")
    assert auth_preflight.check({"DASHBOARD_AUTH_MODE": "local", "DASHBOARD_LOCAL_AUTH_FILE": str(tmp_path / "g.toml")}) == []
    test = run("--password-stdin", "--local-testing", "--out", str(tmp_path / "t.toml"), password="weak")
    assert test.returncode == 0 and local_auth.load_credentials(str(tmp_path / "t.toml")).testing_only is True
    assert "weak" not in (tmp_path / "t.toml").read_text(encoding="utf-8").split("password_hash")[0]
