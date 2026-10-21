"""Read-only checks for the provisioned 13-panel dashboard; requires PyYAML.

Run: python grafana/validate.py
This checks configuration contracts, not Grafana browser rendering or Azure access.
"""
import json
from itertools import combinations
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
SUBSCRIPTION = "a3bb9202-635d-4ab5-aaa4-5458f296b64e"
WORKSPACE = "c424063b-39b1-4ffc-bdf6-1d8e9b55c932"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-incident-platform/providers/Microsoft.ServiceBus/namespaces/incident-bus-4pqaac2c4e4hs"
LOG_RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-incident-platform/providers/Microsoft.OperationalInsights/workspaces/incident-logs"
METRICS = {"ActiveMessages": "Average", "DeadletteredMessages": "Average", "IncomingMessages": "Total", "OutgoingMessages": "Total"}
QUEUES = {"A": "incident-events", "B": "notification-events"}
# id: (title, visualization, x, y, width, height, metric/source)
PANELS = {
    1: ("Service Bus | Active Messages", "timeseries", 0, 22, 12, 8, "ActiveMessages"),
    2: ("Service Bus | Dead-Lettered Messages", "timeseries", 12, 22, 12, 8, "DeadletteredMessages"),
    3: ("Service Bus | Incoming Messages (per interval)", "timeseries", 0, 14, 12, 8, "IncomingMessages"),
    4: ("Service Bus | Outgoing Messages (per interval)", "timeseries", 12, 14, 12, 8, "OutgoingMessages"),
    5: ("Container Apps | System Events by App and Severity", "timeseries", 0, 30, 24, 9, None),
    6: ("How to Read This Dashboard", "text", 0, 39, 24, 10, None),
    7: ("Incoming | Observed Total by Queue", "stat", 0, 0, 6, 6, "IncomingMessages"),
    8: ("Outgoing | Observed Total by Queue", "stat", 6, 0, 6, 6, "OutgoingMessages"),
    9: ("Active | Last Reported Depth by Queue", "stat", 12, 0, 6, 6, "ActiveMessages"),
    10: ("Dead-Lettered | Last Reported Depth by Queue", "stat", 18, 0, 6, 6, "DeadletteredMessages"),
    11: ("Incoming | Observed Messages by Queue", "barchart", 0, 6, 8, 8, "IncomingMessages"),
    12: ("Outgoing | Observed Messages by Queue", "barchart", 8, 6, 8, 8, "OutgoingMessages"),
    13: ("Incoming | Share of Observed Traffic", "piechart", 16, 6, 8, 8, "IncomingMessages"),
}
TOTAL_TRANSFORMS = [
    {"id": "reduce", "options": {"mode": "seriesToRows", "reducers": ["sum", "count"]}},
    {"id": "filterByValue", "options": {"type": "include", "match": "all", "filters": [{"fieldName": "Count", "config": {"id": "greater", "options": {"value": 0}}}]}},
    {"id": "organize", "options": {"excludeByName": {"Count": True}, "renameByName": {"Field": "Queue", "Total": "Messages"}}},
]
TOTAL_DISPLAY = {"values": True, "fields": "/^Messages$/", "calcs": []}
LOG_QUERY = "\n".join([
    "ContainerAppSystemLogs_CL", "| where $__timeFilter(TimeGenerated)",
    "| where EnvironmentName_s == 'incident-env'", "| where isnotempty(ContainerAppName_s)",
    "| summarize Events=count() by bin(TimeGenerated, $__interval), ContainerAppName_s, Type_s",
    "| order by TimeGenerated asc",
])


def validate(dashboard, datasources, provisioning):
    """Raise AssertionError on a configuration regression; never contact Azure."""
    assert datasources["apiVersion"] == provisioning["apiVersion"] == 1, "Provisioning version"
    assert len(datasources["datasources"]) == 1, "Single existing datasource required"
    source = datasources["datasources"][0]
    expected_source = {
        "name": "Azure Monitor", "uid": "azure-monitor", "type": "grafana-azure-monitor-datasource",
        "access": "proxy", "isDefault": True, "editable": False,
        "jsonData": {"azureAuthType": "msi", "subscriptionId": SUBSCRIPTION,
                     "tenantId": "5ba5ef5e-3109-4e77-85bd-cfeb0d347e82", "cloudName": "azuremonitor",
                     "logAnalyticsDefaultWorkspace": WORKSPACE},
    }
    assert source == expected_source, "Existing MSI datasource configuration changed"
    assert len(provisioning["providers"]) == 1, "Single dashboard provider required"
    provider = provisioning["providers"][0]
    assert provider["type"] == "file" and provider["orgId"] == 1, "File provisioning"
    assert provider["disableDeletion"] is True and provider["allowUiUpdates"] is False, "Provisioning protections"
    assert provider["options"] == {"path": "/var/lib/grafana/dashboards", "foldersFromFilesStructure": False}, "Provisioned path"
    assert dashboard["uid"] == "incident-platform-ops" and dashboard["title"] == "Platform Observability | Health & Performance", "Dashboard identity"
    assert dashboard["schemaVersion"] == 41 and dashboard["refresh"] == "5m", "Schema/refresh contract"
    assert dashboard["time"] == {"from": "now-6h", "to": "now"}, "Default time window"
    panels = dashboard["panels"]
    assert len(panels) == 13 and len({p["id"] for p in panels}) == 13, "Exactly 13 unique panel IDs required"
    by_id = {p["id"]: p for p in panels}
    assert set(by_id) == set(PANELS), "Unexpected/missing panel IDs"
    assert [p["id"] for p in panels] == [7, 8, 9, 10, 11, 12, 13, 3, 4, 1, 2, 5, 6], "Overview-to-diagnostics order"
    seen = set()
    for panel in panels:
        ident = panel["id"]
        title, kind, x, y, width, height, metric = PANELS[ident]
        assert (panel["title"], panel["type"]) == (title, kind), f"Panel {ident}: title/type"
        assert panel["gridPos"] == dict(x=x, y=y, w=width, h=height), f"Panel {ident}: layout"
        assert 0 <= x and x + width <= 24 and y >= 0 and height > 0, "Grid bounds"
        assert not any(k in panel for k in ["timeFrom", "timeShift", "repeat"]), "No hidden range/repeat overrides"
        assert panel.get("description") or kind == "text", f"Panel {ident}: explanation required"
        if kind == "text":
            assert panel["options"]["mode"] == "markdown" and not panel.get("targets"), "Explanation panel"
            for phrase in ["Depth is not throughput", "Missing is not zero", "Container Apps", "Overview analytics", "Circular indicator"]:
                assert phrase in panel["options"]["content"], "Operational interpretation guidance"
            continue
        assert panel["datasource"] == {"type": source["type"], "uid": source["uid"]}, "Datasource binding"
        defaults = panel["fieldConfig"]["defaults"]
        assert defaults["unit"] == "short" and defaults["min"] == 0 and defaults.get("noValue"), "Count units/null display"
        refs = [t["refId"] for t in panel["targets"]]
        assert len(refs) == len(set(refs)), f"Panel {ident}: duplicate query references"
        if metric:
            assert refs == ["A", "B"], f"Panel {ident}: both queue targets required"
            for target in panel["targets"]:
                queue = QUEUES[target["refId"]]
                assert target["queryType"] == "Azure Monitor" and not target.get("hide"), "Visible Azure Monitor query"
                q = target["azureMonitor"]
                assert q == {"resourceUri": RESOURCE, "metricNamespace": "Microsoft.ServiceBus/namespaces",
                             "metricName": metric, "aggregation": METRICS[metric], "timeGrain": "auto",
                             "dimensionFilters": [{"dimension": "EntityName", "operator": "eq", "filters": [queue]}],
                             "alias": queue}, f"Panel {ident}: metric/resource/aggregation/queue mismatch"
                seen.add(metric)
        else:
            assert ident == 5 and refs == ["A"], "Only existing scoped log panel allowed"
            target = panel["targets"][0]
            assert target["queryType"] == "Azure Log Analytics", "Log query type"
            q = target["azureLogAnalytics"]
            assert q == {"resources": [LOG_RESOURCE], "workspace": WORKSPACE, "resultFormat": "time_series", "query": LOG_QUERY}, "Existing log query changed"
            assert "$__timeFilter(TimeGenerated)" in q["query"] and "Log_s" not in q["query"], "No raw logs/unbounded query"
        if kind == "timeseries":
            assert defaults["custom"]["spanNulls"] is False and not panel.get("transformations"), "Preserve gaps/original series"
            expected_calcs = ["lastNotNull"] if ident == 5 else ["lastNotNull", "max"]
            assert panel["options"]["legend"] == {"displayMode": "table", "placement": "bottom", "calcs": expected_calcs}, "Original legend"
            assert panel["options"]["tooltip"] == {"mode": "multi"}, "Original tooltip"
        else:
            assert not defaults.get("thresholds") and "max" not in defaults, "No invented thresholds/capacity"
            assert defaults["color"]["mode"] == "palette-classic" and not panel["fieldConfig"]["overrides"], "No hidden threshold/percentage overrides"
            if ident in [7, 8, 11, 12, 13]:
                assert panel.get("transformations") == TOTAL_TRANSFORMS, "Per-queue totals require Count > 0 null guard and queue labels"
                assert defaults["decimals"] == 0, "Traffic totals are counts"
            if kind == "stat":
                assert panel["options"]["textMode"] == "value_and_name", "Keep per-queue labels"
                expected = TOTAL_DISPLAY if ident in [7, 8] else {"values": False, "fields": "", "calcs": ["lastNotNull"]}
                assert panel["options"]["reduceOptions"] == expected, "Stat must not combine queues or sum depth"
                assert panel["options"]["graphMode"] == "none", "No misleading reduced sparkline"
                if ident in [9, 10]:
                    assert not panel.get("transformations"), "Depth remains separate last observations"
            elif kind == "barchart":
                opts = panel["options"]
                assert opts["xField"] == "Queue" and opts["orientation"] == "horizontal", "Queue categories"
                assert opts["stacking"] == "none" and opts["showValue"] == "always", "Unstacked real message counts"
            elif kind == "piechart":
                opts = panel["options"]
                assert metric == "IncomingMessages" and opts["pieType"] == "donut", "Donut uses observed incoming counts"
                assert opts["reduceOptions"] == TOTAL_DISPLAY, "Donut preserves separate queue values"
                assert opts["displayLabels"] == ["name", "value"] and opts["legend"]["values"] == ["value"], "No fabricated percentages"
    assert seen == set(METRICS), "All four supported metrics required"
    for first, second in combinations(panels, 2):
        a, b = first["gridPos"], second["gridPos"]
        assert (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or
                a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]), "Overlapping panels"


if __name__ == "__main__":
    validate(
        json.loads((ROOT / "dashboards/incident-platform-operations.json").read_text(encoding="utf-8-sig")),
        yaml.safe_load((ROOT / "provisioning/datasources/azure-monitor.yaml").read_text()),
        yaml.safe_load((ROOT / "provisioning/dashboards/dashboard-provider.yaml").read_text()),
    )
    print("PASS: all 13 panels; IDs/titles/layout; queue-specific queries and KPI reductions; null-safe totals; bars/donut; original series/logs; MSI datasource/provisioning")
