"""Uploading an image and getting the `evidence_uri` to put on an incident.

Needs the `evidence` extra: pip install ./packages/producer-sdk[evidence]

**This does not work from a laptop, and that is not a misconfiguration.** A
tenant Conditional Access policy refuses *user* accounts a token for
`storage.azure.com` whatever Azure roles they hold, so an `az login` identity
cannot write to the container however the upload is attempted. It works where a
workload identity is doing the writing:

* the Evidence workflow, as the deploy identity (**Storage Blob Data
  Contributor**)
* a producer running in Azure, as its own managed identity

The account has `allowSharedKeyAccess: false` and no key is accepted, which is
what forces the identity route. Do not re-enable it to work around the policy: an
account key is a long-lived credential that can be copied, cannot be revoked per
person, and appears in no audit trail.

The returned URI is a plain `https` blob reference, not a SAS. Minting a
user-delegation SAS would need **Storage Blob Delegator**, which is deliberately
not granted -- a SAS is a bearer token in a URL, passed on by whoever receives it
and revocable only by rotating the key behind every SAS at once. The notification
service and the dashboard each read the bytes with their own identity instead.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("incident_producer.evidence")

DEFAULT_CONTAINER = "evidence"


class EvidenceUploadFailed(RuntimeError):
    """The image was not stored, so no incident should reference it."""


class EvidenceUploader:
    """Puts evidence images in one container and names them for one producer.

    `blob_service` is injectable so this is testable without Azure and without a
    credential that a Conditional Access policy would refuse anyway. Left unset,
    it builds an `azure.storage.blob.BlobServiceClient` with
    `DefaultAzureCredential`.
    """

    def __init__(
        self,
        account_endpoint: str,
        container: str = DEFAULT_CONTAINER,
        *,
        blob_service=None,
        credential=None,
    ):
        if not account_endpoint.startswith("https://"):
            raise ValueError(f"refusing to upload over {account_endpoint!r}")

        self._endpoint = account_endpoint.rstrip("/")
        self._container = container
        self._blob_service = blob_service or self._default_service(credential)

    def _default_service(self, credential):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise EvidenceUploadFailed(
                f"{exc}. Evidence upload needs the extra: "
                "pip install ./packages/producer-sdk[evidence]"
            ) from exc

        return BlobServiceClient(
            self._endpoint, credential=credential or DefaultAzureCredential()
        )

    def upload(self, image: Path | str, blob_name: str | None = None) -> str:
        """Store one image and return the URI to put on an incident.

        `blob_name` defaults to the file's own name. Existing blobs are **not**
        overwritten: evidence is the record of what a camera saw, so a name
        collision is a mistake to surface rather than a thing to resolve by
        discarding one of them.
        """
        path = Path(image)

        if not path.is_file():
            raise EvidenceUploadFailed(f"no such file: {path}")

        name = blob_name or path.name

        if name.startswith("/") or ".." in Path(name).parts:
            raise EvidenceUploadFailed(f"unsafe blob name: {name!r}")

        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

        try:
            from azure.core.exceptions import AzureError
            from azure.storage.blob import ContentSettings
        except ImportError as exc:
            raise EvidenceUploadFailed(
                f"{exc}. Evidence upload needs the extra: "
                "pip install ./packages/producer-sdk[evidence]"
            ) from exc

        blob = self._blob_service.get_blob_client(self._container, name)

        try:
            with path.open("rb") as data:
                blob.upload_blob(
                    data,
                    overwrite=False,
                    content_settings=ContentSettings(content_type=content_type),
                )
        except AzureError as exc:
            raise EvidenceUploadFailed(f"could not upload {name}: {exc}") from exc

        logger.info("uploaded evidence %s (%s)", name, content_type)

        return self.uri_for(name)

    def uri_for(self, blob_name: str) -> str:
        """The `evidence_uri` for a blob, whether or not this uploaded it.

        The name is percent-encoded, because consumers validate the URI by parsing
        it and a raw space or `#` in a filename would change where it points.
        """
        return f"{self._endpoint}/{self._container}/{quote(blob_name)}"
