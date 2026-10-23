"""Evidence downloads through the REAL azure-storage-blob SDK, offline.

A fake HTTP transport stands in for the Blob service, so the SDK's own argument
checks run. The original bounded download passed `length` without `offset`,
which the SDK rejects with ValueError("Offset value must not be None if length
is set."); a Mock-based test accepted it, and every evidence image crashed the
page. Nothing here touches the network or Azure.
"""
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from azure.core.pipeline.transport import RequestsTransport, RequestsTransportResponse
from azure.storage.blob import BlobClient
from PIL import Image
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from evidence import EvidenceStore, EvidenceUnavailable  # noqa: E402

if "soc_dashboard" in sys.modules:
    dashboard = sys.modules["soc_dashboard"]
else:
    spec = importlib.util.spec_from_file_location("soc_dashboard", ROOT / "app.py")
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules["soc_dashboard"] = dashboard
    spec.loader.exec_module(dashboard)

ENDPOINT = "https://storage.blob.core.windows.net"
URI = f"{ENDPOINT}/evidence/TENANT-01/evt-1.png"


def png(width=64, height=48):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(out, format="PNG")
    return out.getvalue()


class FakeBlobService(RequestsTransport):
    """Answers the SDK's HEAD (properties) and ranged GET like Azure Blob does."""

    def __init__(self, body=b"", content_type="image/png", status=200, grow_to=None):
        super().__init__()
        self.body, self.content_type, self.status, self.grow_to = body, content_type, status, grow_to
        self.ranges = []

    def send(self, request, **kwargs):
        response = requests.Response()
        response.request = requests.Request(request.method, request.url).prepare()
        headers = {"Content-Type": self.content_type, "x-ms-blob-type": "BlockBlob", "ETag": '"0x1"',
                   "Last-Modified": "Mon, 19 Oct 2026 10:00:00 GMT"}
        if self.status != 200:
            response.status_code, response.raw = self.status, io.BytesIO(b"")
            headers.update({"Content-Length": "0", "x-ms-error-code": "BlobNotFound" if self.status == 404 else "AuthorizationPermissionMismatch"})
        elif request.method == "HEAD":
            response.status_code, response.raw = 200, io.BytesIO(b"")
            headers["Content-Length"] = str(len(self.body))
        else:
            body = self.grow_to if self.grow_to is not None else self.body  # blob grew after HEAD
            rng = request.headers.get("x-ms-range") or request.headers.get("Range")
            self.ranges.append(rng)
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            chunk = body[start:end + 1]
            response.status_code, response.raw = 206, io.BytesIO(chunk)
            headers.update({"Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(body)}"})
        response.headers.update(headers)
        return RequestsTransportResponse(request, response)


def store_for(service, max_bytes=10 * 1024 * 1024):
    def factory(account_url, container_name, blob_name, credential):
        return BlobClient(account_url, container_name, blob_name, credential=None, transport=service)
    return EvidenceStore(ENDPOINT, "evidence", max_bytes=max_bytes, credential=object(), blob_client_factory=factory)


# --- the regression ------------------------------------------------------------------

def test_sdk_rejects_length_without_offset_which_was_the_crash():
    client = BlobClient(ENDPOINT, "evidence", "x.png", credential=None, transport=FakeBlobService(png()))
    with pytest.raises(ValueError, match="Offset value must not be None if length is set"):
        client.download_blob(length=10)


def test_bounded_download_succeeds_through_the_real_sdk():
    service = FakeBlobService(png())
    evidence = store_for(service).fetch(URI)
    assert evidence.data == png() and evidence.content_type == "image/png" and evidence.filename == "evt-1.png"
    assert service.ranges == [f"bytes=0-{10 * 1024 * 1024}"]  # offset 0, length max_bytes + 1


# --- size limits are preserved ---------------------------------------------------------

def test_oversized_blob_is_refused_before_any_download():
    service = FakeBlobService(b"x" * 101)
    with pytest.raises(EvidenceUnavailable, match="above the 100 byte limit"):
        store_for(service, max_bytes=100).fetch(URI)
    assert service.ranges == []


def test_blob_that_grows_after_properties_is_cut_off_at_limit_plus_one():
    service = FakeBlobService(b"x" * 50, grow_to=b"x" * 5000)
    with pytest.raises(EvidenceUnavailable, match="above the 100 byte limit"):
        store_for(service, max_bytes=100).fetch(URI)
    assert service.ranges == ["bytes=0-100"]  # never more than limit + 1 bytes requested


def test_empty_blob_is_refused_without_a_ranged_read():
    service = FakeBlobService(b"")
    with pytest.raises(EvidenceUnavailable, match="empty"):
        store_for(service).fetch(URI)
    assert service.ranges == []


# --- failures become "no image", never a crash ----------------------------------------

@pytest.mark.parametrize("status", [404, 403])
def test_missing_or_inaccessible_blob_is_unavailable(status):
    with pytest.raises(EvidenceUnavailable):
        store_for(FakeBlobService(png(), status=status)).fetch(URI)


def test_non_image_content_type_is_unavailable():
    with pytest.raises(EvidenceUnavailable, match="not an image"):
        store_for(FakeBlobService(b"<html>", content_type="text/html")).fetch(URI)


def test_any_sdk_value_error_is_unavailable_not_fatal():
    client = SimpleNamespace(__enter__=None)

    class Raising:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_blob_properties(self): return SimpleNamespace(size=10, content_settings=SimpleNamespace(content_type="image/png"))
        def download_blob(self, **kwargs): raise ValueError("SDK validation failure")

    store = EvidenceStore(ENDPOINT, "evidence", credential=object(), blob_client_factory=lambda **kw: Raising())
    with pytest.raises(EvidenceUnavailable):
        store.fetch(URI)


def test_corrupt_original_becomes_no_image(monkeypatch):
    monkeypatch.setattr(dashboard, "get_evidence_store", lambda: store_for(FakeBlobService(b"not really a png")))
    dashboard._load_original.clear()
    assert dashboard._load_original(("TENANT-01",), URI) is None
    monkeypatch.setattr(dashboard, "get_evidence_store", lambda: store_for(FakeBlobService(png())))
    dashboard._load_original.clear()
    assert dashboard._load_original(("TENANT-01",), URI) == png()


# --- the three pages that show evidence render real images ---------------------------

@pytest.fixture
def pages(monkeypatch):
    records = pd.DataFrame([dict(event_id=f"E{n}", tenant_id="TENANT-01", site_id="SITE-01", source_id="CAM-1",
                                 analytics_type="intrusion_detection", event_type="intrusion_detected", severity="high",
                                 timestamp=f"2026-10-19T10:0{n}:00Z", evidence_uri=f"{ENDPOINT}/evidence/TENANT-01/evt-{n}.png")
                            for n in range(3)])
    records["timestamp"] = pd.to_datetime(records["timestamp"], utc=True)
    records.attrs["refreshed_at"] = "2026-10-19T10:05:00+00:00"
    context = SimpleNamespace(tenants=("TENANT-01",), can_simulate=False, can_manage_access=False)
    monkeypatch.setattr(dashboard, "require_operator", lambda: context)
    monkeypatch.setattr(dashboard, "load_incidents", lambda: records)
    monkeypatch.setattr(dashboard, "_load_incidents", lambda tenants: records)
    dashboard._load_original.clear()
    dashboard._load_preview.clear()
    return records


@pytest.mark.parametrize("render", ["render_latest_incidents", "render_evidence"])
def test_evidence_pages_render_images_through_the_real_sdk(pages, monkeypatch, render):
    monkeypatch.setattr(dashboard, "get_evidence_store", lambda: store_for(FakeBlobService(png())))
    at = AppTest.from_string(f"import soc_dashboard as app\napp.{render}(app.load_incidents())").run(timeout=30)
    assert not at.exception, at.exception
    assert len(at.get("image")) == 3  # one evidence image per incident


@pytest.mark.parametrize("render", ["render_latest_incidents", "render_evidence", "render_all_incidents"])
@pytest.mark.parametrize("service", [FakeBlobService(png(), status=403), FakeBlobService(b"broken"), FakeBlobService(b"")])
def test_evidence_failures_never_crash_a_page(pages, monkeypatch, render, service):
    monkeypatch.setattr(dashboard, "get_evidence_store", lambda: store_for(service))
    at = AppTest.from_string(f"import soc_dashboard as app\napp.{render}(app.load_incidents())").run(timeout=30)
    assert not at.exception, at.exception


def test_evidence_outside_the_authorized_dataset_is_never_downloaded(pages, monkeypatch):
    service = FakeBlobService(png())
    monkeypatch.setattr(dashboard, "get_evidence_store", lambda: store_for(service))
    foreign = f"{ENDPOINT}/evidence/TENANT-02/other.png"
    assert dashboard.load_evidence(foreign) is None and service.ranges == []
