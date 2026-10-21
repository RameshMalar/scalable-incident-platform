
import json
from copy import deepcopy
from pathlib import Path

path = Path("grafana/dashboards/incident-platform-operations.json")

with path.open(encoding="utf-8") as file:
    dashboard = json.load(file)

panels = dashboard.get("panels", [])

if len(panels) != 1 or panels[0].get("title") != "Service Bus | Active Messages":
    raise SystemExit("Unexpected dashboard structure. No changes made.")

dlq_panel = deepcopy(panels[0])
dlq_panel["id"] = 2
dlq_panel["title"] = "Service Bus | Dead-Lettered Messages"
dlq_panel["gridPos"] = {
    "h": 9,
    "w": 24,
    "x": 0,
    "y": 9
}

for target in dlq_panel["targets"]:
    target["azureMonitor"]["metricName"] = "DeadletteredMessages"

dashboard["panels"].append(dlq_panel)

with path.open("w", encoding="utf-8") as file:
    json.dump(dashboard, file, indent=2)
    file.write("\n")

print("Dead-Letter Queue panel added successfully.")
