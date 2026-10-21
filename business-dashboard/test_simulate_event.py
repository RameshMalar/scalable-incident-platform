"""Simulate Event, the analytics catalog and the Telegram subscriber view. Offline."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from streamlit.testing.v1 import AppTest

import analytics_catalog
import simulator
import telegram_subscribers
from test_dashboard import dashboard, offline, operator  # noqa: F401 (autouse offline guard)

ROOT = Path(__file__).parent
# Taken before the autouse guard replaces it, for the test of publish itself.
REAL_PUBLISH = simulator.publish
REAL_REQUIRE_OPERATOR = dashboard.require_operator
SCOPE = "api://fbbe63e1-1f8f-4d4d-b695-0f9982842081/.default"
ENV = {"ENABLE_DASHBOARD_SIMULATOR": "true", "SIMULATOR_INGESTION_ORIGIN": "https://ingestion.example",
       "SIMULATOR_API_SCOPE": SCOPE, "SIMULATOR_TENANT": "TENANT-01"}


def person(label, chat, events, approved=True, tenant="TENANT-01"):
    return telegram_subscribers.Subscriber(tenant, telegram_subscribers.mask_chat(chat), telegram_subscribers.chat_kind(chat),
                                           label, approved, tuple(events))


# --- configuration -----------------------------------------------------------------

def test_simulator_is_off_unless_fully_configured():
    with pytest.raises(simulator.SimulatorUnavailable):
        simulator.simulator_config({})
    for key, bad in [("SIMULATOR_INGESTION_ORIGIN", "http://ingestion.example"),
                     ("SIMULATOR_INGESTION_ORIGIN", "https://ingestion.example/api"),
                     ("SIMULATOR_INGESTION_ORIGIN", "https://user:pw@ingestion.example"),
                     ("SIMULATOR_API_SCOPE", "api://not-a-guid/.default"),
                     ("SIMULATOR_API_SCOPE", "https://graph.microsoft.com/.default"),
                     ("SIMULATOR_TENANT", "TENANT-01,TENANT-02"), ("SIMULATOR_TENANT", "")]:
        with pytest.raises(simulator.SimulatorUnavailable):
            simulator.simulator_config({**ENV, key: bad})
    assert simulator.simulator_config(ENV) == simulator.SimulatorConfig("https://ingestion.example", SCOPE, "TENANT-01")


# --- the event --------------------------------------------------------------------------

def test_event_is_a_test_with_real_evidence():
    pool = [{"source_id": "CAM-101", "blob": "TENANT-DEV/fire/a.jpg"}]
    now = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    event = simulator.build_event(analytics_type="fire_smoke_detection", event_type="fire_detected", severity="critical",
                                  description="Fire", tenant_id="TENANT-01", site_id="SITE-02", evidence_pool=pool,
                                  evidence_endpoint="https://acct.blob.core.windows.net/", evidence_container="evidence",
                                  now=now, choose=lambda items: items[0])
    assert event["event_purpose"] == "e2e_test" and event["event_id"].startswith("EVT-SIM-")
    assert event["evidence_uri"] == "https://acct.blob.core.windows.net/evidence/TENANT-DEV/fire/a.jpg"
    assert event["source_id"] == "CAM-101" and event["timestamp"] == now.isoformat()
    assert (event["tenant_id"], event["site_id"], event["severity"]) == ("TENANT-01", "SITE-02", "critical")
    try:
        from common_event_model import IncidentEvent
    except ImportError:
        return
    IncidentEvent.model_validate(event)  # exactly what ingestion validates


def test_event_without_evidence_is_text_only_and_inputs_are_checked():
    event = simulator.build_event(analytics_type="abandoned_object_detection", event_type="object_left_unattended",
                                  severity="medium", description="d", tenant_id="TENANT-01", site_id="SITE-01",
                                  evidence_pool=None, evidence_endpoint="https://x", evidence_container="evidence")
    assert event["evidence_uri"] is None and event["source_id"] == "CAM-SIM-01"
    for change in ({"severity": "extreme"}, {"site_id": "../x"}):
        args = dict(analytics_type="a", event_type="b", severity="low", description="d", tenant_id="TENANT-01",
                    site_id="SITE-01", evidence_pool=None, evidence_endpoint="", evidence_container="evidence")
        with pytest.raises(ValueError):
            simulator.build_event(**{**args, **change})


def response(body, status=202):
    r = Mock(status=status)
    r.read.return_value = json.dumps(body).encode()
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


def test_publish_is_one_authorized_post_to_the_real_endpoint():
    event = {"event_id": "EVT-SIM-1"}
    opener = Mock()
    opener.open.return_value = response({"status": "accepted", "event_id": "EVT-SIM-1"})
    assert REAL_PUBLISH("https://ingestion.example/", "token-abc", event, opener) == "EVT-SIM-1"
    request = opener.open.call_args.args[0]
    assert opener.open.call_count == 1 and request.method == "POST"
    assert request.full_url == "https://ingestion.example/api/v1/incidents"
    assert request.get_header("Authorization") == "Bearer token-abc" and json.loads(request.data) == event
    for body in ({"status": "error"}, {"status": "accepted", "event_id": "OTHER"}, ["accepted"]):
        opener.open.return_value = response(body)
        with pytest.raises(ValueError):
            REAL_PUBLISH("https://ingestion.example", "token-abc", event, opener)
    with pytest.raises(ValueError):
        REAL_PUBLISH("https://ingestion.example", "bad\ntoken", event, opener)


# --- the page ----------------------------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EVIDENCE_BLOB_ENDPOINT", "https://acct.blob.core.windows.net/")
    token = Mock(return_value="mi-token")
    monkeypatch.setattr(dashboard, "ingestion_token", token)
    monkeypatch.setattr(dashboard, "incident_persisted", Mock(return_value=False))
    return token


def page():
    return AppTest.from_string("import soc_dashboard as app\napp.render_simulator()").run(timeout=20)


def send(at):
    at.checkbox[0].check()
    next(b for b in at.button if b.label == "Send test event").click()
    return at.run(timeout=20)


def test_disabled_simulator_explains_and_never_posts():
    at = page()
    assert not at.exception and any("not enabled" in i.value for i in at.info)
    assert not at.button
    dashboard.simulator.publish.assert_not_called()


def test_viewer_cannot_simulate(monkeypatch, enabled):
    monkeypatch.setattr(dashboard, "require_operator", lambda: operator("viewer"))
    at = page()
    assert any("permission is required" in e.value for e in at.error) and not at.button
    enabled.assert_not_called()


def test_tenant_outside_scope_is_refused(monkeypatch, enabled):
    monkeypatch.setenv("SIMULATOR_TENANT", "TENANT-09")
    at = page()
    assert any("outside your authorized scope" in e.value for e in at.error)
    dashboard.simulator.publish.assert_not_called()


def test_one_confirmed_event_then_confirmation_from_cosmos(monkeypatch, enabled):
    publish = Mock(side_effect=lambda origin, token, event: event["event_id"])
    monkeypatch.setattr(dashboard.simulator, "publish", publish)
    at = page()
    next(b for b in at.button if b.label == "Send test event").click().run(timeout=20)
    publish.assert_not_called()  # confirmation not ticked
    at = send(at)
    assert not at.exception and publish.call_count == 1
    origin, token, event = publish.call_args.args
    assert (origin, token) == ("https://ingestion.example", "mi-token")
    assert event["tenant_id"] == "TENANT-01" and event["event_purpose"] == "e2e_test"
    assert event["analytics_type"] == "intrusion_detection" and event["evidence_uri"].startswith("https://acct.blob")
    enabled.assert_called_once_with(SCOPE)
    assert any("waiting for the Worker" in i.value for i in at.info)
    dashboard.incident_persisted.return_value = True
    at = next(b for b in at.button if b.label == "Check again").click().run(timeout=20)
    assert any("Stored in Cosmos DB: " + event["event_id"] in s.value for s in at.success)
    send(at)
    assert publish.call_count == 1  # never a second event without "Send another event"


def test_definite_refusal_publishes_nothing_and_unclear_answers_are_not_retried(monkeypatch, enabled):
    refused = Mock(side_effect=HTTPError("https://ingestion.example", 403, "Forbidden", {}, None))
    monkeypatch.setattr(dashboard.simulator, "publish", refused)
    at = send(page())
    assert any("refused the event (HTTP 403)" in e.value for e in at.error)
    assert "simulator_pending" not in at.session_state
    unclear = Mock(side_effect=URLError("timed out"))
    monkeypatch.setattr(dashboard.simulator, "publish", unclear)
    at.session_state["simulator_last_attempt"] = -1000.0
    at = send(at)
    assert any("unclear" in e.value for e in at.error) and unclear.call_count == 1
    assert any("not retried automatically" in w.value for w in at.warning)


def test_custom_analytics_name_is_validated_and_sent(monkeypatch, enabled):
    publish = Mock(side_effect=lambda origin, token, event: event["event_id"])
    monkeypatch.setattr(dashboard.simulator, "publish", publish)
    at = page()
    at.selectbox(key="sim_analytics").select("Custom analytics…").run(timeout=20)
    at.text_input(key="sim_custom_type").input("Bad Name!").run(timeout=20)
    at.text_input(key="sim_custom_event").input("wrong_way").run(timeout=20)
    assert any("lowercase letters" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "Send test event").disabled
    at.text_input(key="sim_custom_type").input("vehicle_wrong_way_detection").run(timeout=20)
    at = send(at)
    event = publish.call_args.args[2]
    assert (event["analytics_type"], event["event_type"]) == ("vehicle_wrong_way_detection", "wrong_way")
    assert event["evidence_uri"] is None


def test_page_shows_exactly_who_will_be_alerted(monkeypatch, enabled):
    monkeypatch.setattr(dashboard, "subscribers_for", Mock(return_value=[
        person("Malar", "111222333", ["intrusion_detected"]),
        person("Lee", "444555666", ["fire_detected"]),
        person("Pending", "777888999", ["intrusion_detected"], approved=False),
    ]))
    at = page()
    alerted = " ".join(s.value for s in at.success)
    assert "1 Telegram subscriber: Malar (…333)" in alerted and "Lee" not in alerted and "Pending" not in alerted
    assert "111222333" not in alerted


# --- analytics catalog ------------------------------------------------------------------------

def test_catalog_display_names_aliases_and_unknown_values():
    catalog = analytics_catalog.load_catalog()
    assert catalog.analytics_name("PPE_detection") == catalog.analytics_name("ppe_detection") == "PPE Compliance"
    assert catalog.analytics_name("vehicle_wrong_way_detection") == "Vehicle Wrong Way Detection"
    assert catalog.event_name("crowd_density_exceeded") == "Crowd density exceeded"
    assert dashboard.analytics_name("fire_smoke_detection") == "Fire & Smoke Detection"
    assert any(spec.custom for spec in catalog.analytics)


def test_catalog_entries_are_complete_and_evidence_pools_exist():
    catalog = analytics_catalog.load_catalog()
    pools = simulator.load_evidence_pools()
    for spec in catalog.analytics:
        for event in spec.events:
            assert event.severity in analytics_catalog.SEVERITIES
            assert event.evidence_pool is None or pools.get(event.evidence_pool), event.type


def test_custom_names_are_strict_identifiers():
    ok = ["vehicle_wrong_way_detection", "abc", "loitering_v2"]
    bad = ["", "ab", "Bad", "has space", "1starts_with_digit", "x" * 49, "drop;table", "a-b", None]
    assert all(analytics_catalog.valid_custom_name(v) for v in ok)
    assert not any(analytics_catalog.valid_custom_name(v) for v in bad)


def test_duplicate_catalog_entries_are_rejected(tmp_path):
    data = json.loads((ROOT / "analytics_catalog.json").read_text(encoding="utf-8"))
    data["analytics"][1]["aliases"].append("intrusion_detection")
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        analytics_catalog.load_catalog.__wrapped__(str(path))


def test_dashboard_evidence_pools_match_ingestion():
    ingestion = ROOT.parent / "ingestion-service" / "app" / "simulated_evidence.json"
    if not ingestion.exists():
        pytest.skip("ingestion-service not in this checkout")
    assert json.loads(ingestion.read_text()) == json.loads((ROOT / "simulated_evidence.json").read_text())


# --- Telegram subscribers ------------------------------------------------------------------------

class FakeContainer:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query_items(self, query, parameters, partition_key):
        self.calls.append(partition_key)
        return [r for r in self.rows if r["_t"] == partition_key]


def test_subscribers_come_from_routing_data_and_are_masked():
    subs = FakeContainer([
        dict(_t="TENANT-01", event_type="intrusion_detected", telegram_chat_id="111222333", enabled=True, channel="telegram"),
        dict(_t="TENANT-01", event_type="fire_detected", telegram_chat_id="111222333", enabled=False, channel="telegram"),
        dict(_t="TENANT-01", event_type="intrusion_detected", telegram_chat_id="-4800000508", enabled=True, channel="telegram"),
        dict(_t="TENANT-02", event_type="intrusion_detected", telegram_chat_id="999", enabled=True, channel="telegram"),
    ])
    dests = FakeContainer([
        dict(_t="TENANT-01", telegram_chat_id="111222333", enabled=True, label="Malar"),
        dict(_t="TENANT-01", telegram_chat_id="-4800000508", enabled=False),
    ])
    db = SimpleNamespace(get_container_client=lambda name: subs if name == "subscriptions" else dests)
    result = telegram_subscribers.load_subscribers(db, ("TENANT-01",))
    assert subs.calls == ["TENANT-01"] and dests.calls == ["TENANT-01"]  # one partition, never cross-tenant
    malar = next(s for s in result if s.label == "Malar")
    group = next(s for s in result if s.kind == "Group")
    assert (malar.chat, malar.event_types, malar.receives_alerts) == ("…333", ("intrusion_detected",), True)
    assert group.chat == "…508" and not group.receives_alerts  # subscribed but not approved
    assert telegram_subscribers.recipients(result, "TENANT-01", "intrusion_detected") == [malar]
    assert telegram_subscribers.recipients(result, "TENANT-01", "fire_detected") == []
    assert not any("111222333" in str(s) for s in result)


def test_telegram_page_is_admin_only_and_masks_chats(monkeypatch):
    monkeypatch.setattr(dashboard, "subscribers_for", Mock(return_value=[person("Malar", "111222333", ["intrusion_detected"])]))
    at = AppTest.from_string("import soc_dashboard as app\napp.render_telegram_subscribers()").run(timeout=20)
    assert not at.exception
    table = at.dataframe[0].value
    assert list(table["Chat"]) == ["…333"] and list(table["Alerts for"]) == ["Intrusion detected"]
    monkeypatch.setattr(dashboard, "require_operator", lambda: operator("viewer"))
    at = AppTest.from_string("import soc_dashboard as app\napp.render_telegram_subscribers()").run(timeout=20)
    assert any("Administrator role is required" in e.value for e in at.error) and not at.dataframe


# --- demo guarantees ------------------------------------------------------------------------

def test_custom_analytics_event_passes_the_common_event_model():
    common = pytest.importorskip("common_event_model")
    event = simulator.build_event(analytics_type="vehicle_wrong_way_detection", event_type="wrong_way_driving",
                                  severity="high", description="Wrong Way Driving detected in monitored area",
                                  tenant_id="TENANT-01", site_id="SITE-03", evidence_pool=None,
                                  evidence_endpoint="", evidence_container="evidence")
    parsed = common.IncidentEvent.model_validate(event)
    assert parsed.event_purpose == "e2e_test" and parsed.analytics_type == "vehicle_wrong_way_detection"


def test_the_dashboard_never_writes_to_cosmos(monkeypatch):
    # Only reads exist on this fake; any write attribute would raise.
    container = Mock(spec=["read_item", "query_items"])
    monkeypatch.setattr(dashboard, "get_container", lambda: container)
    assert dashboard.incident_persisted("EVT-SIM-1", "TENANT-01") is True
    container.read_item.assert_called_once_with(item="EVT-SIM-1", partition_key="TENANT-01")
    container.read_item.side_effect = Exception("404")
    assert dashboard.incident_persisted("EVT-SIM-2", "TENANT-01") is False


def test_display_names_never_change_stored_values(monkeypatch):
    import pandas as pd
    records = pd.DataFrame([dict(event_id="E1", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-1",
                                 analytics_type="ppe_detection", event_type="vest_missing", severity="medium",
                                 timestamp=pd.Timestamp("2026-10-20T10:00:00Z"), evidence_uri=None)])
    before = records.copy(deep=True)
    monkeypatch.setattr(dashboard, "test_records", records, raising=False)
    at = AppTest.from_string("import soc_dashboard as app\napp.render_all_incidents(app.test_records)").run(timeout=20)
    assert not at.exception
    assert at.dataframe[0].value["Analytics"].tolist() == ["PPE Compliance"]
    assert at.dataframe[0].value["Event"].tolist() == ["Safety vest missing"]
    pd.testing.assert_frame_equal(records, before)  # stored values untouched


def test_three_team_members_share_the_existing_login_concurrently(monkeypatch, tmp_path):
    import pandas as pd
    import local_auth
    import test_local_auth as tla
    monkeypatch.setattr(dashboard, "require_operator", REAL_REQUIRE_OPERATOR)
    for name in local_auth.ENTRA_ONLY_FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DASHBOARD_AUTH_MODE", "local")
    # The one existing single-account secret, exactly as deployed today.
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTH_FILE", str(tla.write_credentials(tmp_path / "team.toml")))
    monkeypatch.setattr(local_auth, "LIMITER", local_auth.AttemptLimiter())
    df = pd.DataFrame([dict(event_id="E1", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-1",
                            analytics_type="intrusion_detection", event_type="intrusion_detected", severity="high",
                            timestamp=pd.Timestamp("2026-10-20T10:00:00Z"), evidence_uri=None)])
    df.attrs["refreshed_at"] = "2026-10-20T10:01:00+00:00"
    monkeypatch.setattr(dashboard, "load_incidents", Mock(return_value=df))
    sessions = [tla.sign_in(tla.run_app(), "admin", tla.PASSWORD) for _ in range(3)]
    for at in sessions:
        assert not at.exception and any('chip role-admin">ADMIN<' in m.value for m in at.markdown)
    next(b for b in sessions[0].button if b.label == "Sign out").click().run(timeout=20)
    assert [b.label for b in sessions[0].button] == ["Sign in"]
    sessions[1].run(timeout=20)
    assert any('chip role-admin">ADMIN<' in m.value for m in sessions[1].markdown)  # the others stay signed in
