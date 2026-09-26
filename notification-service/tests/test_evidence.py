import pytest
from azure.core.exceptions import ResourceNotFoundError

from app.evidence import (
    DEFAULT_MAX_EVIDENCE_BYTES,
    EvidenceStore,
    EvidenceUnavailable,
)


ENDPOINT = "https://incidentevi123.blob.core.windows.net"


class FakeContentSettings:
    def __init__(self, content_type):
        self.content_type = content_type


class FakeProperties:
    def __init__(self, size, content_type):
        self.size = size
        self.content_settings = FakeContentSettings(content_type)


class FakeDownloader:
    def __init__(self, data):
        self._data = data

    async def readall(self):
        return self._data


class FakeBlobClient:
    def __init__(
        self,
        data=b"jpeg-bytes",
        size=None,
        content_type="image/jpeg",
        properties_error=None,
    ):
        self.data = data
        self.size = len(data) if size is None else size
        self.content_type = content_type
        self.properties_error = properties_error
        self.downloaded = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get_blob_properties(self):
        if self.properties_error is not None:
            raise self.properties_error
        return FakeProperties(self.size, self.content_type)

    async def download_blob(self):
        self.downloaded = True
        return FakeDownloader(self.data)


class FakeBlobClientFactory:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


def make_store(client=None, **kwargs):
    client = client if client is not None else FakeBlobClient()
    factory = FakeBlobClientFactory(client)
    store = EvidenceStore(
        blob_endpoint=kwargs.pop("blob_endpoint", ENDPOINT),
        container=kwargs.pop("container", "evidence"),
        credential=object(),
        blob_client_factory=factory,
        **kwargs,
    )
    return store, factory, client


async def test_fetch_returns_image_bytes_for_a_matching_uri():
    store, factory, _ = make_store()

    evidence = await store.fetch(f"{ENDPOINT}/evidence/2026/09/EVT-001.jpg")

    assert evidence.data == b"jpeg-bytes"
    assert evidence.filename == "EVT-001.jpg"
    assert evidence.content_type == "image/jpeg"

    assert factory.calls[0]["container_name"] == "evidence"
    assert factory.calls[0]["blob_name"] == "2026/09/EVT-001.jpg"
    assert factory.calls[0]["account_url"] == ENDPOINT


async def test_uri_pointing_at_another_host_is_refused():
    store, factory, _ = make_store()

    with pytest.raises(EvidenceUnavailable, match="host"):
        await store.fetch("https://attacker.example.test/evidence/EVT-001.jpg")

    assert factory.calls == []


async def test_uri_pointing_at_another_container_is_refused():
    store, factory, _ = make_store()

    with pytest.raises(EvidenceUnavailable, match="container"):
        await store.fetch(f"{ENDPOINT}/secrets/EVT-001.jpg")

    assert factory.calls == []


async def test_non_https_uri_is_refused():
    store, _, _ = make_store()

    with pytest.raises(EvidenceUnavailable, match="scheme"):
        await store.fetch("http://incidentevi123.blob.core.windows.net/evidence/a.jpg")


async def test_uri_without_a_blob_path_is_refused():
    store, _, _ = make_store()

    with pytest.raises(EvidenceUnavailable, match="no blob path"):
        await store.fetch(f"{ENDPOINT}/evidence")


async def test_traversal_segment_is_refused():
    store, _, _ = make_store()

    with pytest.raises(EvidenceUnavailable, match="traversal"):
        await store.fetch(f"{ENDPOINT}/evidence/../secrets/key.jpg")


async def test_caller_supplied_sas_query_is_dropped():
    store, factory, _ = make_store()

    await store.fetch(f"{ENDPOINT}/evidence/EVT-001.jpg?sig=stolen&se=2030-01-01")

    # Access is decided by this service's identity, so the token must not
    # survive into the blob request.
    assert factory.calls[0]["blob_name"] == "EVT-001.jpg"
    assert "stolen" not in str(factory.calls[0])


async def test_percent_encoded_path_is_decoded():
    store, factory, _ = make_store()

    await store.fetch(f"{ENDPOINT}/evidence/site%20a/EVT%2D001.jpg")

    assert factory.calls[0]["blob_name"] == "site a/EVT-001.jpg"


async def test_oversized_blob_is_refused_without_downloading():
    client = FakeBlobClient(size=DEFAULT_MAX_EVIDENCE_BYTES + 1)
    store, _, _ = make_store(client)

    with pytest.raises(EvidenceUnavailable, match="above the"):
        await store.fetch(f"{ENDPOINT}/evidence/huge.jpg")

    assert client.downloaded is False


async def test_non_image_content_type_is_refused():
    client = FakeBlobClient(content_type="video/mp4")
    store, _, _ = make_store(client)

    with pytest.raises(EvidenceUnavailable, match="sendPhoto"):
        await store.fetch(f"{ENDPOINT}/evidence/clip.mp4")

    assert client.downloaded is False


async def test_missing_content_type_is_inferred_from_the_blob_name():
    client = FakeBlobClient(content_type=None)
    store, _, _ = make_store(client)

    evidence = await store.fetch(f"{ENDPOINT}/evidence/EVT-001.png")

    assert evidence.content_type == "image/png"


async def test_missing_content_type_on_an_unknown_extension_is_refused():
    client = FakeBlobClient(content_type=None)
    store, _, _ = make_store(client)

    with pytest.raises(EvidenceUnavailable, match="sendPhoto"):
        await store.fetch(f"{ENDPOINT}/evidence/EVT-001.bin")


async def test_storage_error_becomes_evidence_unavailable():
    client = FakeBlobClient(
        properties_error=ResourceNotFoundError("blob does not exist")
    )
    store, _, _ = make_store(client)

    with pytest.raises(EvidenceUnavailable, match="could not download"):
        await store.fetch(f"{ENDPOINT}/evidence/missing.jpg")


async def test_empty_blob_is_refused():
    client = FakeBlobClient(data=b"")
    store, _, _ = make_store(client)

    with pytest.raises(EvidenceUnavailable, match="empty"):
        await store.fetch(f"{ENDPOINT}/evidence/EVT-001.jpg")


async def test_trailing_slash_on_the_endpoint_is_tolerated():
    store, factory, _ = make_store(blob_endpoint=f"{ENDPOINT}/")

    await store.fetch(f"{ENDPOINT}/evidence/EVT-001.jpg")

    assert factory.calls[0]["account_url"] == ENDPOINT


def test_from_env_is_disabled_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("EVIDENCE_BLOB_ENDPOINT", raising=False)

    assert EvidenceStore.from_env() is None


def test_from_env_reads_endpoint_and_container(monkeypatch):
    monkeypatch.setenv("EVIDENCE_BLOB_ENDPOINT", f"{ENDPOINT}/")
    monkeypatch.setenv("EVIDENCE_CONTAINER", "proof")

    store = EvidenceStore.from_env()

    assert store is not None
    assert store.blob_endpoint == ENDPOINT
    assert store.container == "proof"
    assert store.max_bytes == DEFAULT_MAX_EVIDENCE_BYTES
