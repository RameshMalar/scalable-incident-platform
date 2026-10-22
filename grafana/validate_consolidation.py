"""Offline contracts for original Workbook queries and existing queue semantics."""
import json
from itertools import combinations
from pathlib import Path

ROOT=Path(__file__).parent
SOURCE={'type':'grafana-azure-monitor-datasource','uid':'azure-monitor'}
WORKBOOK_PANELS={20:'query - container apps inventory',21:'query - container apps environment status',22:'query - cosmos db inventory',25:'query - cosmos db latency',28:'query - container apps replicas',29:'query - container apps cpu and memory',30:'query - cosmos db ru utilization',31:'query - cosmos db storage',34:'query - cosmos db consumed ru'}

def validate(dashboard,datasources,provisioning):
    assert dashboard['uid']=='incident-platform-ops'
    assert dashboard['refresh']=='', 'Manual refresh prevents background query multiplication'
    assert dashboard['time']=={'from':'now-6h','to':'now'}
    source=datasources['datasources'][0]
    assert len(datasources['datasources'])==1 and source['uid']=='azure-monitor'
    assert source['jsonData']['azureAuthType']=='msi' and source['editable'] is False
    assert source['jsonData']['logAnalyticsDefaultWorkspace']=='c424063b-39b1-4ffc-bdf6-1d8e9b55c932'
    assert provisioning['providers'][0]['allowUiUpdates'] is False
    assert provisioning['providers'][0]['disableDeletion'] is True
    panels=dashboard['panels']
    by_id={p['id']:p for p in panels}
    assert len(by_id)==len(panels), 'Duplicate panel IDs'
    rows=[p['title'] for p in panels if p['type']=='row']
    assert rows==['A. Platform Health','B. Ingestion and Throughput','C. Processing Latency','D. Service Bus and Queue Monitoring','E. Notification Performance','F. Container Scaling and Resource Utilization']
    originals=json.loads((ROOT/'workbook/queue-contract.json').read_text())
    for ident in [1,2,3,4,7,8,9,10]:
        actual=by_id[ident]
        expected=originals[str(ident)]
        for key in ['targets','datasource','fieldConfig','options','transformations']:
            assert actual.get(key)==expected.get(key),f'Original panel {ident}: {key} changed'
    events=by_id[5]
    original_events=originals['5']
    assert events['title']==original_events['title'] and events['datasource']==original_events['datasource']
    assert events['type']=='table' and 'timeFrom' not in events
    target=events['targets'][0]['azureLogAnalytics']
    original_target=original_events['targets'][0]['azureLogAnalytics']
    assert target['resources']==original_target['resources'] and target['workspace']==original_target['workspace']
    assert target['query']==(ROOT/'workbook/system-events.kql').read_text().strip()
    assert target['resultFormat']=='table' and target['dashboardTime'] is False
    assert events['fieldConfig']['defaults']['noValue']=='Telemetry unavailable'
    assert len(panels)==35 and set(range(1,14))<=set(by_id)
    # Deployed panel snapshots match source commit 6a3c2085. Only grid placement
    # may differ when embedding the original panels in the consolidated layout.
    for ident in [6,11,12,13]:
        assert ident in by_id, f'Original deployed panel {ident} missing'
        actual={k:v for k,v in by_id[ident].items() if k!='gridPos'}
        expected={k:v for k,v in originals[str(ident)].items() if k!='gridPos'}
        assert actual==expected, f'Original deployed panel {ident} changed'
    catalogue=json.loads((ROOT/'workbook/queries.json').read_text())
    for ident,key in WORKBOOK_PANELS.items():
        p=by_id[ident]
        target=p['targets'][0]
        c=catalogue[key]
        branch='azureLogAnalytics' if c['queryType']==0 else 'azureResourceGraph'
        assert target[branch]['query']==c['query'].replace('{ResourceGroup}','rg-incident-platform'), 'Workbook query/formula drift'
        assert target[branch]['resultFormat']=='table'
        if c['queryType']==0:
            assert p['timeFrom']=='7d'
        assert p['datasource']==SOURCE
    assert 'percentile(Maximum, 95)' in by_id[25]['targets'][0]['azureLogAnalytics']['query']
    assert 'Peak Replicas' in by_id[28]['title'], 'Seven-day maximum is not current replicas'
    assert any(o['matcher'].get('options')=='CurrentReplicas' and {'id':'displayName','value':'Peak replicas (7 days)'} in o['properties'] for o in by_id[28]['fieldConfig']['overrides'])
    latest=by_id[33]['targets'][0]['azureLogAnalytics']['query']
    assert 'arg_max(TimeGenerated, Maximum)' in latest and 'max(Maximum)' not in latest
    assert 'TimeGenerated > ago(7d)' in latest and by_id[33]['timeFrom']=='7d'
    assert 'TimeGenerated >= ago(15m)' in latest and 'real(null)' in latest
    assert 'LastReportedAt=TimeGenerated' in latest and 'join kind=leftouter' in latest
    assert 'Stale; current unavailable' in latest and 'No sample; current unavailable' in latest
    metric=by_id[35]['targets'][0]['azureMonitor']
    assert metric['metricName']=='TotalRequestUnits' and metric['aggregation']=='Total'
    assert metric['metricNamespace']=='Microsoft.DocumentDB/databaseAccounts'
    assert metric['timeGrain']=='PT1H' and by_id[35]['timeFrom']=='7d'
    assert by_id[34]['fieldConfig']['defaults']['noValue']=='No data'
    text='\n'.join(p.get('options',{}).get('content','') for p in panels)
    assert 'not request-level P95' in text and '**Unavailable:**' in text
    for p in panels:
        pos=p['gridPos']
        assert 0<=pos['x'] and pos['x']+pos['w']<=24 and pos['h']>0
        if p.get('targets'):
            assert p['datasource']==SOURCE
            assert not p['fieldConfig']['defaults'].get('custom',{}).get('spanNulls',False)
    for first,second in combinations(panels,2):
        a,b=first['gridPos'],second['gridPos']
        assert a['x']+a['w']<=b['x'] or b['x']+b['w']<=a['x'] or a['y']+a['h']<=b['y'] or b['y']+b['h']<=a['y'], 'Overlapping panels'
