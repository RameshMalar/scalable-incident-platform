"""Evidence upload, with a fake blob service.

Faked rather than skipped. A tenant Conditional Access policy refuses user
accounts a token for `storage.azure.com`, so there is no credential available
here, in CI, or on any laptop that could make a real upload -- only a workload
identity can, and that is a deployment to exercise, not a unit test. The naming,
the refusals and the URI are still worth pinning.
"""

from __future__ import annotations

import pytest

from incident_producer import EvidenceUploader, EvidenceUploadFailed

ENDPOINT = "https://incidentevi4pqaac2c4e4hs.blob.core.windows.net"


class FakeBlob:
    def __init__(self):
        self.uploaded = None
        self.content_type = None
        self.overwrite = None

    def upload_blob(self, data, overwrite=None, content_settings=None):
        self.uploaded = data.read()
        self.overwrite = overwrite
        self.content_type = getattr(content_settings, "content_type", None)


class FakeBlobService:
    def __init__(self):
        self.blobs = {}
        self.asked_for = []

    def get_blob_client(self, container, name):
        self.asked_for.append((container, name))
        return self.blobs.setdefault((container, name), FakeBlob())


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "intrusion-test.jpg"
    path.write_bytes(b"\xff\xd8\xff not really a jpeg")
    return path


def an_uploader(service=None, container="evidence"):
    return EvidenceUploader(ENDPOINT, container, blob_service=service or FakeBlobService())


def test_an_upload_returns_the_uri_to_put_on_an_incident(image):
    service = FakeBlobService()

    uri = an_uploader(service).upload(image)

    assert uri == f"{ENDPOINT}/evidence/intrusion-test.jpg"
    assert service.asked_for == [("evidence", "intrusion-test.jpg")]


def test_the_bytes_and_the_content_type_reach_the_blob(image):
    service = FakeBlobService()

    an_uploader(service).upload(image)

    blob = service.blobs[("evidence", "intrusion-test.jpg")]
    assert blob.uploaded == image.read_bytes()
    assert blob.content_type == "image/jpeg"


def test_an_existing_blob_is_not_overwritten(image):
    # Evidence is the record of what a camera saw, so a name collision is a
    # mistake to surface rather than one to resolve by discarding an image.
    service = FakeBlobService()

    an_uploader(service).upload(image)

    assert service.blobs[("evidence", "intrusion-test.jpg")].overwrite is False


def test_a_name_can_be_given(image):
    service = FakeBlobService()

    uri = an_uploader(service).upload(image, blob_name="SITE-01/CAM-07/evt-1.jpg")

    assert uri == f"{ENDPOINT}/evidence/SITE-01/CAM-07/evt-1.jpg"


def test_a_missing_file_fails_before_any_call():
    service = FakeBlobService()

    with pytest.raises(EvidenceUploadFailed, match="no such file"):
        an_uploader(service).upload("/nonexistent/x.jpg")

    assert service.asked_for == []


@pytest.mark.parametrize("name", ["/etc/passwd", "../../secrets.jpg", "a/../../b.jpg"])
def test_an_unsafe_blob_name_is_refused(image, name):
    with pytest.raises(EvidenceUploadFailed, match="unsafe"):
        an_uploader().upload(image, blob_name=name)


def test_an_endpoint_without_tls_is_refused():
    with pytest.raises(ValueError, match="refusing"):
        EvidenceUploader("http://account.blob.core.windows.net", blob_service=object())


def test_a_name_needing_encoding_is_encoded():
    # Consumers validate the URI by parsing it, so a raw space or '#' in a
    # filename would change where it points.
    uri = an_uploader().uri_for("loading bay #2.jpg")

    assert uri == f"{ENDPOINT}/evidence/loading%20bay%20%232.jpg"


def test_an_unrelated_azure_failure_is_reported_as_an_upload_failure(image):
    from azure.core.exceptions import ServiceRequestError

    class Failing(FakeBlobService):
        def get_blob_client(self, container, name):
            blob = FakeBlob()
            blob.upload_blob = lambda *a, **k: (_ for _ in ()).throw(
                ServiceRequestError("network is unreachable")
            )
            return blob

    with pytest.raises(EvidenceUploadFailed, match="could not upload"):
        an_uploader(Failing()).upload(image)
