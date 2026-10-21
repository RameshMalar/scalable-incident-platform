"""Offline dashboard regression tests. All cloud reads and event writes are mocked."""
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('soc_dashboard', ROOT / 'app.py')
dashboard = importlib.util.module_from_spec(spec)
sys.modules['soc_dashboard'] = dashboard
spec.loader.exec_module(dashboard)


@pytest.fixture
def records():
    df = pd.DataFrame([
        dict(event_id='OLD', tenant_id='TENANT-01', site_id='SITE-01', source_id='CAM-1', analytics_type='intrusion_detection', event_type='intrusion_detected', severity='high', timestamp='2026-10-18T10:00:00Z', evidence_uri='https://storage.blob.core.windows.net/evidence/old.jpg'),
        dict(event_id='NEW', tenant_id='TENANT-02', site_id='SITE-02', source_id='CAM-2', analytics_type='people_counting', event_type='people_counted', severity='critical', timestamp='2026-10-19T10:00:00Z', evidence_uri=None),
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df.attrs['refreshed_at'] = '2026-10-19T10:01:00+00:00'
    return df


def operator(role='admin', tenants=('TENANT-01', 'TENANT-02')):
    return dashboard.OperatorContext(f'local:{role}', 'directory', tenants, dashboard.LOCAL_ROLE_PERMISSIONS[role],
                                     4102444800, dashboard.LOCAL_ROLE_NAMES[role])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(dashboard, 'require_operator', lambda: operator('admin'))
    monkeypatch.setattr(dashboard, 'get_container', Mock(side_effect=AssertionError('unexpected Cosmos read')))
    monkeypatch.setattr(dashboard, 'get_evidence_store', Mock(return_value=None))
    monkeypatch.delenv('ENABLE_DASHBOARD_SIMULATOR', raising=False)
    monkeypatch.setattr(dashboard.simulator, 'publish', Mock(side_effect=AssertionError('unexpected POST')))
    monkeypatch.setattr(dashboard, 'ingestion_token', Mock(side_effect=AssertionError('unexpected token request')))
    monkeypatch.setattr(dashboard, 'subscribers_for', Mock(return_value=[]))


def run_ui(code):
    return AppTest.from_string('import soc_dashboard as app\n' + code).run(timeout=15)


def test_kpi_rows_render_as_html_not_code_blocks(monkeypatch, records):
    # Cards are joined into one markdown call. Markdown ends an HTML block at a
    # blank line and treats the following indented card as a code block, which
    # showed raw "<div class=..." text for 6 of the 8 cards.
    card = dashboard.kpi_card('Total incidents', '81', '◆', '#38bdf8', 4.0)
    assert card == card.strip() and card.startswith('<div class="kpi-card"')
    monkeypatch.setattr(dashboard, 'load_incidents', Mock(return_value=records))
    at = run_ui('app.main()')
    assert not at.exception
    rows = [element.value for element in at.markdown if 'class="soc-kpis"' in element.value]
    assert len(rows) == 2
    assert sum(row.count('class="kpi-card"') for row in rows) == 8
    for row in rows:
        assert row.startswith('<div class="soc-kpis">') and '\n\n' not in row


def test_overview_and_filter_rerun(monkeypatch, records):
    monkeypatch.setattr(dashboard, 'load_incidents', Mock(return_value=records))
    at = run_ui('app.main()')
    assert not at.exception
    markup = '\n'.join(element.value for element in at.markdown)
    assert 'Critical incidents' in markup and 'Latest incident' in markup
    assert any(element.proto.label == 'Platform Observability' for element in at.get('link_button'))
    assert 'Cosmos refreshed 2026-10-19 10:01 UTC' in markup
    tenant = next(element for element in at.checkbox if element.label == 'TENANT-02')
    tenant.uncheck().run()
    assert not at.exception
    assert any(element.value.startswith('<div class="soc-filters">Tenants: TENANT-01 ·') for element in at.markdown)


def test_empty_store_still_has_simulator_navigation(monkeypatch, records):
    monkeypatch.setattr(dashboard, 'load_incidents', Mock(return_value=records.iloc[:0]))
    at = run_ui('app.main()')
    assert not at.exception
    assert any('No incidents match' in element.value for element in at.warning)
    assert any(element.proto.label == 'Platform Observability' for element in at.get('link_button'))


def test_feed_newest_first_and_metadata(monkeypatch, records):
    records['event_purpose'] = ['e2e_test', 'incident']
    monkeypatch.setattr(dashboard, 'test_records', records, raising=False)
    monkeypatch.setattr(dashboard, 'load_evidence', Mock(return_value=None))
    at = run_ui('app.render_latest_incidents(app.test_records)')
    assert not at.exception
    ids = [element.value for element in at.text if element.value.startswith('Event ID:')]
    assert ids == ['Event ID: NEW', 'Event ID: OLD']
    assert any('CAM-2' in element.value and 'SITE-02' in element.value for element in at.caption)
    assert any('2026-10-19 10:00:00 UTC' == element.value for element in at.caption)
    assert any('TEST · e2e_test' == element.value for element in at.caption)


def test_gallery_only_selected_category(monkeypatch, records):
    records.loc[1, 'evidence_uri'] = 'https://storage.blob.core.windows.net/evidence/new.jpg'
    monkeypatch.setattr(dashboard, 'test_records', records, raising=False)
    monkeypatch.setattr(dashboard, 'get_evidence_store', Mock(return_value=SimpleNamespace(describes_evidence=lambda uri: bool(uri))))
    grid = Mock()
    monkeypatch.setattr(dashboard, '_render_evidence_grid', grid)
    at = run_ui('app.render_evidence(app.test_records)')
    assert not at.exception
    assert at.selectbox[0].options == ['All', 'intrusion_detection', 'people_counting']
    assert grid.call_count == 1
    at.selectbox[0].select('people_counting').run()
    assert grid.call_args.args[0]['event_id'].tolist() == ['NEW']


def test_gallery_null_reference_and_bounded_page(monkeypatch, records):
    many = pd.concat([records.iloc[[0]]] * 30, ignore_index=True)
    many['event_id'] = [f'EVT-{i}' for i in range(30)]
    many.loc[29, 'evidence_uri'] = float('nan')
    monkeypatch.setattr(dashboard, 'test_records', many, raising=False)
    store = SimpleNamespace(describes_evidence=lambda uri: uri.startswith('https://'))
    monkeypatch.setattr(dashboard, 'get_evidence_store', lambda: store)
    image = io.BytesIO()
    Image.new('RGB', (32, 32)).save(image, format='PNG')
    fetch = Mock(return_value=image.getvalue())
    monkeypatch.setattr(dashboard, 'load_evidence', fetch)
    at = run_ui('app.render_evidence(app.test_records)')
    assert not at.exception and fetch.call_count == 12
    next(button for button in at.button if button.label == 'Next page').click().run()
    assert not at.exception and fetch.call_count == 24


def test_feed_expansion_fetches_full_image(monkeypatch, records):
    image = io.BytesIO()
    Image.new('RGB', (32, 32)).save(image, format='PNG')
    fetch = Mock(return_value=image.getvalue())
    monkeypatch.setattr(dashboard, 'test_records', records.iloc[[0]], raising=False)
    monkeypatch.setattr(dashboard, 'load_evidence', fetch)
    at = run_ui('app.render_latest_incidents(app.test_records)')
    at.button[0].click().run()
    assert not at.exception
    assert any(call.kwargs.get('thumbnail') is False for call in fetch.call_args_list)


@pytest.mark.parametrize('uri', [
    'http://storage.blob.core.windows.net/evidence/a.jpg',
    'https://evil.example/evidence/a.jpg',
    'https://storage.blob.core.windows.net/other/a.jpg',
    'https://storage.blob.core.windows.net/evidence/%2e%2e/a.jpg',
])
def test_existing_blob_restrictions_reject_untrusted_uri(uri):
    store = dashboard.EvidenceStore('https://storage.blob.core.windows.net', 'evidence', credential=object())
    assert store.describes_evidence(uri) is False
    with pytest.raises(dashboard.EvidenceUnavailable):
        store.fetch(uri)


def test_html_escapes_incident_data():
    html = dashboard.bar_panel('Types', pd.Series({'<script>': 1}), {})
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_thumbnail_bounds_and_cache(monkeypatch):
    output = io.BytesIO()
    Image.new('RGB', (1920, 1080), 'navy').save(output, format='JPEG')
    store = Mock()
    store.fetch.return_value.data = output.getvalue()
    monkeypatch.setattr(dashboard, 'get_evidence_store', lambda: store)
    dashboard._load_preview.clear()
    dashboard._load_original.clear()
    monkeypatch.setattr(dashboard, '_load_incidents', lambda tenants: pd.DataFrame({'evidence_uri':['https://storage.blob.core.windows.net/evidence/test.jpg']}))
    data = dashboard.load_evidence('https://storage.blob.core.windows.net/evidence/test.jpg')
    dashboard.load_evidence('https://storage.blob.core.windows.net/evidence/test.jpg')
    assert Image.open(io.BytesIO(data)).size == (640, 360)
    assert len(data) < len(output.getvalue())
    store.fetch.assert_called_once()
    dashboard._load_preview.clear()
    dashboard._load_original.clear()
