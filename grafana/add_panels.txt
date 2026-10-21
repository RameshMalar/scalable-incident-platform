
import json
from pathlib import Path

dashboard_path = Path(
    "grafana/dashboards/incident-platform-operations.json"
)

with dashboard_path.open(encoding="utf-8") as file:
    dashboard = json.load(file)

# Do not overwrite panels if the script is run again.
if dashboard.get("panels"):
    raise SystemExit(
        "Dashboard already contains panels. No changes made."
    )

subscription_id = "a3bb9202-635d-4ab5-aaa4-5458f296b64e"

service_bus_resource = (
    "/subscriptions/" + subscription_id
    + "/resourceGroups/rg-incident-platform"
    + "/providers/Microsoft.ServiceBus"
    + "/namespaces/incident-bus-4pqaac2c4e4hs"
)

def queue_target(queue_name, ref_id):
    return {
        "refId": ref_id,
        "queryType": "Azure Monitor",
        "azureMonitor": {
            "resourceUri": service_bus_resource,
            "metricNamespace": "Microsoft.ServiceBus/namespaces",
            "metricName": "ActiveMessages",
            "aggregation": "Average",
            "timeGrain": "auto",
            "dimensionFilters": [
                {
                    "dimension": "EntityName",
                    "operator": "eq",
                    "filters": [queue_name],
                }
            ],
        },
    }

panel = {
    "id": 1,
    "title": "Service Bus | Active Messages",
    "type": "timeseries",
    "datasource": {
        "type": "grafana-azure-monitor-datasource",
        "uid": "azure-monitor",
    },
    "gridPos": {
        "h": 9,
        "w": 24,
        "x": 0,
        "y": 0,
    },
    "targets": [
        queue_target("incident-events", "A"),
        queue_target("notification-events", "B"),
    ],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "min": 0,
        },
        "overrides": [],
    },
    "options": {
        "legend": {
            "displayMode": "table",
            "placement": "bottom",
        },
        "tooltip": {
            "mode": "multi",
        },
    },
}

dashboard["panels"] = [panel]

with dashboard_path.open("w", encoding="utf-8") as file:
    json.dump(dashboard, file, indent=2)
    file.write("\n")

print("Service Bus Active Messages panel added.")
