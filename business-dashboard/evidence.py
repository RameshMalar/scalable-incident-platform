"""Resolve `evidence_uri` into image bytes for the dashboard to render.

The evidence container is private and the storage account has
`allowSharedKeyAccess: false`, so there is no URL a browser can be handed: a
blob reference put into an `<img src>` comes back 404. The dashboard therefore
reads the bytes itself, with the managed identity granted **Storage Blob Data
Reader** on the container in `infra/core.bicep`, and serves them inline.

That role carries `blobs/read` and nothing else. Minting a user-delegation SAS
so the browser could fetch directly would need `Storage Blob Delegator`, which
is deliberately not granted -- a SAS is a bearer token in a URL, shareable by
whoever receives it and revocable only in bulk.

This mirrors `notification-service/app/evidence.py`, which does the same job for
Telegram delivery. The two are separate because that one is async and this one
runs inside Streamlit's synchronous render, but the validation rules are the
same on purpose: `evidence_uri` reaches the store from an analytics producer, so
it is untrusted whichever service reads it. The host and container are matched
against the configured account and only the remaining path is used, so a
producer cannot aim the dashboard at a URL of its choosing or at another
container in the same account.

Failure is never fatal. An incident whose evidence cannot be read is still worth
showing, so callers get None and render the incident without an image.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

logger = logging.getLogger("dashboard.evidence")

# Enough for a camera still, and low enough that a mistaken reference to a video
# does not buffer hundreds of megabytes into the replica serving the page.
DEFAULT_MAX_EVIDENCE_BYTES = 10 * 1024 * 1024


class EvidenceUnavailable(Exception):
    """Evidence cannot be shown, so the incident renders without an image."""


@dataclass(frozen=True)
class Evidence:
    data: bytes
    filename: str
    content_type: str


class EvidenceStore:
    """Downloads evidence images from one private blob container."""

    def __init__(
        self,
        blob_endpoint: str,
        container: str,
        max_bytes: int | None = None,
        credential=None,
        blob_client_factory=None,
    ) -> None:
        if not blob_endpoint:
            raise ValueError("blob_endpoint is required")

        if not container:
            raise ValueError("container is required")

        self.blob_endpoint = blob_endpoint.rstrip("/")
        self.container = container

        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else int(
                os.environ.get(
                    "EVIDENCE_MAX_BYTES",
                    str(DEFAULT_MAX_EVIDENCE_BYTES),
                )
            )
        )

        if self.max_bytes <= 0:
            raise ValueError("EVIDENCE_MAX_BYTES must be positive")

        self._host = urlparse(self.blob_endpoint).netloc.lower()

        if not self._host:
            raise ValueError(
                f"blob_endpoint must be an absolute URL, got {blob_endpoint!r}"
            )

        self._credential = credential or DefaultAzureCredential()
        self._blob_client_factory = blob_client_factory or BlobClient

    @classmethod
    def from_env(cls) -> "EvidenceStore | None":
        """Build from configuration, or None when evidence is not configured.

        Returning None keeps the dashboard runnable against a deployment that
        has no storage account, and keeps local runs free of Azure storage --
        which matters more than it sounds, because a tenant Conditional Access
        policy refuses user accounts a token for `storage.azure.com`, so a
        developer's own credential cannot read this container at all. Deployed,
        the managed identity is unaffected by that policy and evidence appears.
        """
        endpoint = os.environ.get("EVIDENCE_BLOB_ENDPOINT", "").strip()

        if not endpoint:
            return None

        return cls(
            blob_endpoint=endpoint,
            container=os.environ.get("EVIDENCE_CONTAINER", "evidence").strip(),
        )

    def describes_evidence(self, evidence_uri: str | None) -> bool:
        """Whether this URI points at a blob this store could read.

        Lets a caller count or filter incidents without downloading anything.
        Simulated PPE incidents, and any simulated incident from a local run,
        carry placeholder paths such as `/evidence/ppe_sample.jpg` that name no
        real blob, and listing those as
        having evidence would promise an image that never arrives.
        """
        if not evidence_uri:
            return False

        try:
            self._blob_name(evidence_uri)
        except EvidenceUnavailable:
            return False

        return True

    def fetch(self, evidence_uri: str) -> Evidence:
        """Download one evidence image.

        Raises EvidenceUnavailable for anything that should render the incident
        without an image.
        """
        blob_name = self._blob_name(evidence_uri)

        client = self._blob_client_factory(
            account_url=self.blob_endpoint,
            container_name=self.container,
            blob_name=blob_name,
            credential=self._credential,
        )

        try:
            with client:
                properties = client.get_blob_properties()

                size = getattr(properties, "size", None)

                # Checked before downloading so an oversized blob is never
                # buffered into the replica's memory.
                if size is not None and size > self.max_bytes:
                    raise EvidenceUnavailable(
                        f"evidence {blob_name} is {size} bytes, "
                        f"above the {self.max_bytes} byte limit"
                    )

                # A ranged read of a 0-byte blob is answered 416 by the
                # service; an empty blob has no image to show anyway.
                if size == 0:
                    raise EvidenceUnavailable(f"evidence {blob_name} is empty")

                content_type = self._image_content_type(properties, blob_name)

                # Bound the transfer even if the Blob grows after properties
                # were read. A limit+1 response is rejected below. The SDK
                # refuses a length without an offset (ValueError), so the
                # range is stated in full: bytes 0 .. max_bytes.
                data = client.download_blob(
                    offset=0, length=self.max_bytes + 1
                ).readall()

        # ValueError covers the SDK's own argument and response validation:
        # whatever goes wrong fetching one image, the incident still renders.
        except (AzureError, ValueError) as exc:
            raise EvidenceUnavailable(
                f"could not download evidence {blob_name}: {type(exc).__name__}"
            ) from exc

        # A blob can grow between the properties call and the download.
        if len(data) > self.max_bytes:
            raise EvidenceUnavailable(
                f"evidence {blob_name} is {len(data)} bytes, "
                f"above the {self.max_bytes} byte limit"
            )

        if not data:
            raise EvidenceUnavailable(f"evidence {blob_name} is empty")

        return Evidence(
            data=data,
            filename=blob_name.rsplit("/", 1)[-1] or "evidence",
            content_type=content_type,
        )

    def _blob_name(self, evidence_uri: str) -> str:
        """Validate the producer-supplied URI and return the blob path."""
        if not evidence_uri:
            raise EvidenceUnavailable("evidence_uri is empty")

        parsed = urlparse(evidence_uri)

        if parsed.scheme.lower() != "https":
            raise EvidenceUnavailable(
                f"evidence_uri scheme {parsed.scheme!r} is not https"
            )

        if parsed.netloc.lower() != self._host:
            raise EvidenceUnavailable(
                f"evidence_uri host {parsed.netloc!r} is not {self._host!r}"
            )

        path = unquote(parsed.path).lstrip("/")
        container, _, blob_name = path.partition("/")

        if container != self.container:
            raise EvidenceUnavailable(
                f"evidence_uri container {container!r} "
                f"is not {self.container!r}"
            )

        if not blob_name:
            raise EvidenceUnavailable("evidence_uri has no blob path")

        # A traversal segment cannot escape the container on the service side,
        # but it signals a malformed or hostile reference either way.
        if any(segment == ".." for segment in blob_name.split("/")):
            raise EvidenceUnavailable(
                f"evidence_uri path {blob_name!r} contains a traversal segment"
            )

        # Any query string is dropped rather than honoured. Access is decided
        # by this dashboard's own identity, never by a caller-supplied SAS.
        return blob_name

    @staticmethod
    def _image_content_type(properties, blob_name: str) -> str:
        content_settings = getattr(properties, "content_settings", None)
        content_type = getattr(content_settings, "content_type", None)

        if not content_type:
            content_type = mimetypes.guess_type(blob_name)[0]

        if not content_type or not content_type.startswith("image/"):
            raise EvidenceUnavailable(
                f"evidence {blob_name} has content type {content_type!r}, "
                "which is not an image"
            )

        return content_type
