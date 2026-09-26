"""Resolve `evidence_uri` into image bytes for Telegram delivery.

Evidence lives in a private blob container. Nothing in the pipeline hands out
SAS URLs or account keys, so the Notification Service reads the blob with its
own managed identity and the `Storage Blob Data Reader` role granted in
`infra/core.bicep`.

`evidence_uri` arrives from an analytics producer, which makes it untrusted
input. The URI is therefore never fetched as given: the host and container are
matched against the configured account, and only the remaining blob path is
used to build the request. A producer cannot point the service at a URL of its
choosing, and cannot reach another container in the same account.

Every failure here is non-fatal by design. The caller degrades to the text-only
alert rather than dropping the notification.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobClient

logger = logging.getLogger("app.evidence")

# Telegram rejects sendPhoto uploads above 10 MB.
DEFAULT_MAX_EVIDENCE_BYTES = 10 * 1024 * 1024


class EvidenceUnavailable(Exception):
    """Evidence cannot be delivered, so the alert must go out as text."""


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

        self._credential = credential
        self._owns_credential = credential is None
        self._blob_client_factory = blob_client_factory or BlobClient

    @classmethod
    def from_env(cls) -> "EvidenceStore | None":
        """Build from configuration, or None when evidence is not configured.

        Returning None keeps the service deployable before the blob account
        exists, and keeps local runs free of Azure dependencies.
        """
        endpoint = os.environ.get("EVIDENCE_BLOB_ENDPOINT", "").strip()

        if not endpoint:
            return None

        return cls(
            blob_endpoint=endpoint,
            container=os.environ.get("EVIDENCE_CONTAINER", "evidence").strip(),
        )

    async def fetch(self, evidence_uri: str) -> Evidence:
        """Download one evidence image.

        Raises EvidenceUnavailable for anything that should downgrade the
        notification to text.
        """
        blob_name = self._blob_name(evidence_uri)

        if self._credential is None:
            self._credential = DefaultAzureCredential()

        client = self._blob_client_factory(
            account_url=self.blob_endpoint,
            container_name=self.container,
            blob_name=blob_name,
            credential=self._credential,
        )

        try:
            async with client:
                properties = await client.get_blob_properties()

                size = getattr(properties, "size", None)

                # Checked before downloading so an oversized blob is never
                # buffered into the replica's memory.
                if size is not None and size > self.max_bytes:
                    raise EvidenceUnavailable(
                        f"evidence {blob_name} is {size} bytes, "
                        f"above the {self.max_bytes} byte limit"
                    )

                content_type = self._image_content_type(properties, blob_name)

                stream = await client.download_blob()
                data = await stream.readall()

        except AzureError as exc:
            raise EvidenceUnavailable(
                f"could not download evidence {blob_name}: {exc}"
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
        # by this service's own identity, never by a caller-supplied SAS.
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
                "which Telegram sendPhoto cannot deliver"
            )

        return content_type

    async def close(self) -> None:
        if self._credential is not None and self._owns_credential:
            await self._credential.close()
            self._credential = None
