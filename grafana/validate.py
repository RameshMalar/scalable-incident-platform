"""Offline dashboard/provisioning validation; does not imply live MSI access."""
import json
from pathlib import Path
import yaml
from validate_consolidation import validate
ROOT=Path(__file__).parent
if __name__=='__main__':
    validate(json.loads((ROOT/'dashboards/incident-platform-operations.json').read_text(encoding='utf-8')),yaml.safe_load((ROOT/'provisioning/datasources/azure-monitor.yaml').read_text()),yaml.safe_load((ROOT/'provisioning/dashboards/dashboard-provider.yaml').read_text()))
    print('PASS: Workbook query fidelity, preserved queue metrics, six sections, cost controls and layout')
