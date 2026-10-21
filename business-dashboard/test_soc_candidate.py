"""Recovered SOC contract: complete screen, external-only navigation and real aggregations."""
import json
from unittest.mock import Mock
import pandas as pd
from streamlit.testing.v1 import AppTest
import test_dashboard
app=test_dashboard.dashboard

def sample():
    frame=pd.DataFrame([dict(event_id=f'E{i}',tenant_id='TENANT-01',site_id=f'SITE-{i}',source_id=f'CAM-{i}',analytics_type='intrusion_detection',event_type='intrusion_detected',severity=['critical','high','medium','low'][i%4],timestamp=pd.Timestamp('2026-10-21T16:01Z')+pd.Timedelta(minutes=i),evidence_uri=None) for i in range(7)])
    frame.attrs['refreshed_at']='2026-10-21T16:10:00+00:00'
    return frame

def test_complete_overview_and_single_external_controls(monkeypatch):
    monkeypatch.setenv('DASHBOARD_AUTH_MODE','local')
    monkeypatch.setattr(app,'require_operator',lambda:test_dashboard.operator())
    monkeypatch.setattr(app,'load_incidents',lambda:sample())
    monkeypatch.setattr(app,'get_evidence_store',lambda:None)
    publish=Mock(side_effect=AssertionError('navigation must never publish'))
    monkeypatch.setattr(app.simulator,'publish',publish)
    at=AppTest.from_string('import soc_dashboard as app\napp.main()').run(timeout=20)
    assert not at.exception
    markup='\n'.join(m.value for m in at.markdown)
    assert markup.count('class="kpi-card"')==8
    assert 'DEMO MODE' not in markup
    charts=[json.loads(chart.proto.spec) for chart in at.get('plotly_chart')]
    assert len(charts)==5
    assert charts[2]['data'][0]['type']=='pie' and charts[2]['data'][0]['hole']>0
    assert sum(charts[2]['data'][0]['values'])==7
    assert charts[3]['data'][0]['type']=='heatmap'
    assert len(charts[4]['data'][0]['y'])==5
    links=at.get('link_button')
    assert sum(b.proto.label=='Simulate Event' for b in links)==1
    assert next(b for b in links if b.proto.label=='Simulate Event').proto.url==app.INGESTION_SIMULATOR_URL
    assert next(b for b in links if b.proto.label=='Platform Observability').proto.url==app.GRAFANA_DASHBOARD_URL
    assert any(b.label=='Sign out' for b in at.button)
    assert not hasattr(app,'render_observability')
    publish.assert_not_called()

def test_sgt_boundary_and_future_events():
    frame=sample().iloc[:2].copy()
    frame['timestamp']=pd.to_datetime(['2026-10-21T15:59Z','2026-10-21T16:01Z'],utc=True)
    daily=app.daily_severity_figure(frame)
    assert daily.data[0].x[0].day==21 and daily.data[0].x[1].day==22
    grid=app.activity_heatmap_figure(frame).data[0].z
    assert grid[2][23]==1 and grid[3][0]==1
    cards=app.overview_kpis(frame,frame.iloc[:0],now=pd.Timestamp('2026-10-21T16:00Z').to_pydatetime())
    assert '>1<' in cards[4] and 'kpi-trend flat' in cards[4]
