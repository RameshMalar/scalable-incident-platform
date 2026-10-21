import copy
import json
import sys
from pathlib import Path
import pytest
import yaml
import ast
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from validate_consolidation import validate

def inputs():
    return (json.loads((ROOT/'dashboards/incident-platform-operations.json').read_text()),yaml.safe_load((ROOT/'provisioning/datasources/azure-monitor.yaml').read_text()),yaml.safe_load((ROOT/'provisioning/dashboards/dashboard-provider.yaml').read_text()))

def test_configuration_passes():
    validate(*inputs())

def test_streamlit_links_directly_to_existing_grafana():
    tree=ast.parse((ROOT.parent/'business-dashboard/app.py').read_text(encoding='utf-8'))
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='link_button']
    links=[n for n in calls if len(n.args)>1 and isinstance(n.args[0],ast.Constant) and n.args[0].value=='Platform Observability']
    assert len(links)==1
    assert isinstance(links[0].args[1],ast.Name)
    assert links[0].args[1].id=='GRAFANA_DASHBOARD_URL'
    import ast as _ast
    constants={n.targets[0].id:_ast.literal_eval(n.value) for n in tree.body if isinstance(n,_ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],_ast.Name) and n.targets[0].id=='GRAFANA_DASHBOARD_URL'}
    assert constants['GRAFANA_DASHBOARD_URL']=='https://incident-grafana.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/d/incident-platform-ops/incident-platform-7c-operations'
    assert not any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='render_observability' for n in ast.walk(tree))

@pytest.mark.parametrize('mutation',['formula','queue','overlap','refresh','missing_zero'])
def test_rejects_semantic_regressions(mutation):
    dashboard,sources,provisioning=inputs()
    panels={p['id']:p for p in dashboard['panels']}
    if mutation=='formula':
        panels[25]['targets'][0]['azureLogAnalytics']['query']=panels[25]['targets'][0]['azureLogAnalytics']['query'].replace('percentile(Maximum, 95)','avg(Average)')
    elif mutation=='queue':
        panels[1]['targets'][0]['azureMonitor']['aggregation']='Total'
    elif mutation=='overlap':
        panels[21]['gridPos']=copy.deepcopy(panels[20]['gridPos'])
    elif mutation=='refresh':
        dashboard['refresh']='5s'
    else:
        panels[9]['fieldConfig']['defaults']['noValue']='0'
    with pytest.raises(AssertionError):
        validate(dashboard,sources,provisioning)
