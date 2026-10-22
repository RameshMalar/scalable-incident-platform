"""Local Admin/Viewer roles and the Phase 1 SOC dashboard features. Offline."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import test_local_auth as tla
from test_local_auth import PASSWORD, dashboard, local_auth, local_mode  # noqa: F401 (fixture)

ROOT = Path(__file__).parent
VIEWER_PASSWORD = "Viewer-Horse-Battery-77"  # synthetic test value, not a real credential


def write_accounts(path, accounts, testing_only=False):
    body = "".join(
        f'\n[[local_auth.accounts]]\nusername = "{name}"\npassword_hash = "{local_auth.hash_password(pw)}"\nrole = "{role}"\n'
        for name, pw, role in accounts)
    path.write_text(f"[local_auth]\nlocal_testing_only = {'true' if testing_only else 'false'}\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def two_roles(local_mode, monkeypatch):
    path = write_accounts(local_mode / "roles.toml", [("admin", PASSWORD, "admin"), ("soc-viewer", VIEWER_PASSWORD, "viewer")])
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTH_FILE", str(path))
    return path


@pytest.fixture
def records():
    df = pd.DataFrame([
        dict(event_id="EVT-A1", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-001", analytics_type="fire_smoke_detection",
             event_type="fire_detected", severity="critical", timestamp="2026-10-19T10:00:00Z", event_purpose="incident",
             evidence_uri="https://s.blob.core.windows.net/evidence/a.jpg", description="Fire detected in monitored area"),
        dict(event_id="EVT-B2", tenant_id="TENANT-02", site_id="SITE-02", source_id="CAM-002", analytics_type="intrusion_detection",
             event_type="intrusion_detected", severity="high", timestamp="2026-10-20T10:00:00Z", event_purpose="e2e_test",
             evidence_uri=None, description="=HYPERLINK(\"http://x\")"),
        dict(event_id="EVT-C3", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-003", analytics_type="crowd_detection",
             event_type="crowd_threshold_exceeded", severity="low", timestamp="2026-10-18T10:00:00Z", event_purpose="incident",
             evidence_uri="", description="Crowd threshold exceeded"),
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df.attrs["refreshed_at"] = "2026-10-20T10:01:00+00:00"
    return df


def capture_navigation(monkeypatch):
    sections = {}

    def navigation(pages, **kwargs):
        sections.clear()
        sections["pages"] = [page.title for page in pages]
        return SimpleNamespace(run=lambda: None)
    monkeypatch.setattr(dashboard.st, "navigation", navigation)
    return sections


# --- credentials file ----------------------------------------------------------------

def test_accounts_file_carries_roles(tmp_path):
    creds = local_auth.load_credentials(str(write_accounts(tmp_path / "a.toml", [("admin", PASSWORD, "admin"), ("v", PASSWORD, "viewer")])))
    assert {n: a.role for n, a in creds.accounts.items()} == {"admin": "admin", "v": "viewer"}


@pytest.mark.parametrize("body", [
    # duplicate username
    '[[local_auth.accounts]]\nusername="a"\npassword_hash="{h}"\nrole="admin"\n[[local_auth.accounts]]\nusername="a"\npassword_hash="{h}"\nrole="viewer"\n',
    # unknown role
    '[[local_auth.accounts]]\nusername="a"\npassword_hash="{h}"\nrole="root"\n',
    # role is mandatory in the accounts format
    '[[local_auth.accounts]]\nusername="a"\npassword_hash="{h}"\n',
    # plaintext password next to a hash
    '[[local_auth.accounts]]\nusername="a"\npassword_hash="{h}"\nrole="admin"\npassword="x"\n',
    # no accounts at all
    'accounts = []\n',
])
def test_bad_accounts_reject_the_whole_file(tmp_path, body):
    path = tmp_path / "bad.toml"
    path.write_text("[local_auth]\n" + body.replace("{h}", local_auth.hash_password(PASSWORD)), encoding="utf-8")
    with pytest.raises(local_auth.LocalAuthUnavailable):
        local_auth.load_credentials(str(path))


def test_mixed_legacy_and_accounts_format_is_rejected(tmp_path):
    path = write_accounts(tmp_path / "m.toml", [("admin", PASSWORD, "admin")])
    path.write_text(path.read_text(encoding="utf-8").replace("[local_auth]\n", '[local_auth]\nusername = "x"\n', 1), encoding="utf-8")
    with pytest.raises(local_auth.LocalAuthUnavailable):
        local_auth.load_credentials(str(path))


def test_unknown_username_still_costs_one_scrypt(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(local_auth, "FAILURE_DELAY_SECONDS", 0)
    creds = local_auth.load_credentials(str(write_accounts(tmp_path / "a.toml", [("admin", PASSWORD, "admin")])))
    seen = []
    real = local_auth.verify_password
    monkeypatch.setattr(local_auth, "verify_password", lambda stored, pw: seen.append(stored) or real(stored, pw))
    assert not local_auth.attempt_sign_in("nobody", PASSWORD, "1.1.1.1", creds, local_auth.AttemptLimiter())
    assert len(seen) == 1 and seen[0].startswith("scrypt$") and seen[0] != creds.accounts["admin"].password_hash


def test_role_permissions_are_server_side():
    admin, viewer = dashboard.local_operator_context("a", "admin"), dashboard.local_operator_context("v", "viewer")
    assert admin.is_admin and admin.can_export and admin.can_simulate and admin.role == "Admin"
    assert not (viewer.is_admin or viewer.can_export or viewer.can_simulate) and viewer.role == "Viewer"
    # Neither local role reaches Entra-only central access management.
    assert not admin.can_manage_access and not viewer.can_manage_access


# --- the signed-in experience per role -------------------------------------------------

def test_admin_and_viewer_see_different_navigation(two_roles, monkeypatch, records):
    monkeypatch.setattr(dashboard, "load_incidents", Mock(return_value=records))
    sections = capture_navigation(monkeypatch)
    at = tla.sign_in(tla.run_app(), "admin", PASSWORD)
    assert not at.exception
    assert sections["pages"] == ["Overview", "All Incidents", "Evidence Gallery",
                                 "Telegram Subscriptions"]
    markup = " ".join(m.value for m in at.markdown)
    assert "ADMIN" not in markup and "Administration" not in sections["pages"]  # no admin badge or page

    at = tla.sign_in(tla.run_app(), "soc-viewer", VIEWER_PASSWORD)
    assert not at.exception
    assert sections["pages"] == ["Overview", "All Incidents", "Evidence Gallery"]


def test_role_change_takes_effect_on_the_next_request(two_roles, monkeypatch, records):
    monkeypatch.setattr(dashboard, "load_incidents", Mock(return_value=records))
    sections = capture_navigation(monkeypatch)
    at = tla.sign_in(tla.run_app(), "admin", PASSWORD)
    assert any(b.proto.label == "Simulate Event" for b in at.get("link_button"))
    write_accounts(two_roles, [("admin", PASSWORD, "viewer")])
    at.run(timeout=20)
    assert not any(b.proto.label == "Simulate Event" for b in at.get("link_button")) and "Telegram Subscriptions" not in sections["pages"]
    write_accounts(two_roles, [("someone-else", PASSWORD, "admin")])
    at.run(timeout=20)
    assert [b.label for b in at.button] == ["Sign in"]


def run_page(monkeypatch, role, code, records):
    context = dashboard.local_operator_context(role, role)
    monkeypatch.setattr(dashboard, "require_operator", lambda: context)
    monkeypatch.setattr(dashboard, "get_container", Mock(side_effect=AssertionError("unexpected Cosmos read")))
    monkeypatch.setattr(dashboard, "get_evidence_store", Mock(return_value=None))
    monkeypatch.setattr(dashboard, "test_records", records, raising=False)
    return AppTest.from_string("import soc_dashboard as app\n" + code).run(timeout=20)


def test_export_is_admin_only(monkeypatch, records):
    admin = run_page(monkeypatch, "admin", "app.render_all_incidents(app.test_records)", records)
    assert not admin.exception and len(admin.get("download_button")) == 1
    viewer = run_page(monkeypatch, "viewer", "app.render_all_incidents(app.test_records)", records)
    assert not viewer.exception and not viewer.get("download_button")
    assert any("available to Admins" in c.value for c in viewer.caption)


def test_admin_console_refuses_viewer_and_never_shows_hashes(two_roles, monkeypatch, records):
    viewer = run_page(monkeypatch, "viewer", "app.render_admin_console()", records)
    assert any("Administrator role is required" in e.value for e in viewer.error) and not viewer.dataframe
    admin = run_page(monkeypatch, "admin", "app.render_admin_console()", records)
    assert not admin.exception
    accounts = admin.dataframe[-1].value
    assert list(accounts["Username"]) == ["admin", "soc-viewer"] and list(accounts["Role"]) == ["Admin", "Viewer"]
    rendered = " ".join(str(e.value) for e in admin.text) + " ".join(str(d.value) for d in admin.dataframe)
    assert "scrypt$" not in rendered


# --- All incidents search and filters --------------------------------------------------

def test_search_is_literal_and_case_insensitive(records):
    assert list(dashboard.filter_incidents(records, "evt-a1")["event_id"]) == ["EVT-A1"]
    assert list(dashboard.filter_incidents(records, "Fire")["event_id"]) == ["EVT-A1"]  # readable analytics name
    assert list(dashboard.filter_incidents(records, "cam-003")["event_id"]) == ["EVT-C3"]
    assert dashboard.filter_incidents(records, ".*").empty  # never a regex
    assert len(dashboard.filter_incidents(records, "x" * 500)) == 0


def test_filters_and_sorting(records):
    f = dashboard.filter_incidents
    assert list(f(records)["event_id"]) == ["EVT-B2", "EVT-A1", "EVT-C3"]
    assert list(f(records, sort="Oldest first")["event_id"]) == ["EVT-C3", "EVT-A1", "EVT-B2"]
    assert list(f(records, sort="Most severe first")["event_id"]) == ["EVT-A1", "EVT-B2", "EVT-C3"]
    assert list(f(records, severities=["low"])["event_id"]) == ["EVT-C3"]
    assert list(f(records, purpose="Tests only")["event_id"]) == ["EVT-B2"]
    assert list(f(records, purpose="Real incidents")["event_id"]) == ["EVT-A1", "EVT-C3"]
    assert list(f(records, evidence="With image")["event_id"]) == ["EVT-A1"]
    assert list(f(records, evidence="Without image")["event_id"]) == ["EVT-B2", "EVT-C3"]


def test_csv_export_neutralises_formulas(records):
    text = dashboard.incidents_csv(records).decode("utf-8")
    assert "'=HYPERLINK" in text and "\n=HYPERLINK" not in text and ",=HYPERLINK" not in text


def test_table_shows_severity_with_shape_and_label(records):
    table = dashboard.incident_table(records)
    assert list(table["Severity"]) == ["■ CRITICAL", "▲ HIGH", "● LOW"]
    assert list(table["Evidence"]) == [True, False, False]
    assert list(table["Purpose"]) == ["Incident", "TEST · e2e_test", "Incident"]


# --- overview -----------------------------------------------------------------------------

def test_eight_kpis_and_rising_incidents_read_as_worse(records):
    import pandas as pd
    now = pd.Timestamp("2026-10-20T12:00:00Z").to_pydatetime()
    current, previous = pd.concat([records] * 10), pd.concat([records] * 4)
    cards = dashboard.overview_kpis(current, previous, now=now)
    assert len(cards) == 8
    assert dashboard.TREND_WORSE in cards[0]  # 30 incidents vs 12 before: up is bad
    assert "Last 24 hours" in cards[4] and ">10<" in cards[4]
    # A percentage against fewer than MIN_TREND_BASE incidents is not shown.
    assert dashboard.pct_change(42, 2) is None and dashboard.pct_change(20, 10) == 100
    falling = dashboard.kpi_card("Total incidents", "1", "◆", "#000", -50.0)
    assert dashboard.TREND_BETTER in falling


def test_overview_charts_render(monkeypatch, records):
    at = run_page(monkeypatch, "viewer", "app.render_overview(app.test_records, app.test_records.iloc[:0])", records)
    assert not at.exception
    assert len(at.get("plotly_chart")) == 5


def test_severity_palette_is_complete_and_badges_escape():
    assert set(dashboard.SEVERITY_COLORS) == set(dashboard.SEVERITY_ICONS) == set(dashboard.SEVERITY_ORDER)
    assert "<script>" not in dashboard.severity_badge("<script>")


# --- account tooling -------------------------------------------------------------------------

def test_hash_tool_builds_an_admin_and_viewer_file(tmp_path):
    tool = ROOT / "tools" / "make_password_hash.py"
    out = tmp_path / "accounts.toml"

    def run(*args, password):
        return subprocess.run([sys.executable, str(tool), "--password-stdin", "--out", str(out), *args],
                              input=password + "\n", capture_output=True, text=True)

    assert run("--username", "admin", "--role", "admin", password=PASSWORD).returncode == 0
    assert run("--username", "soc-viewer", "--role", "viewer", password=VIEWER_PASSWORD).returncode != 0  # no --append
    assert run("--username", "soc-viewer", "--role", "viewer", "--append", password=VIEWER_PASSWORD).returncode == 0
    assert run("--username", "admin", "--append", password=PASSWORD).returncode != 0  # existing, no --force
    creds = local_auth.load_credentials(str(out))
    assert {n: a.role for n, a in creds.accounts.items()} == {"admin": "admin", "soc-viewer": "viewer"}
    assert local_auth.verify_password(creds.accounts["soc-viewer"].password_hash, VIEWER_PASSWORD)
    assert PASSWORD not in out.read_text(encoding="utf-8")
