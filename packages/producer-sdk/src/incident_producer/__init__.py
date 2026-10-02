"""Builds, validates, authenticates and delivers incidents to the platform.

The smallest useful producer:

    from incident_producer import AzureCliToken, Deployment, IncidentProducer, build

    deployment = Deployment.discover()
    producer = IncidentProducer(
        deployment.endpoint, token_source=AzureCliToken(deployment.audience)
    )
    producer.wake()

    receipt = producer.send(
        build(
            tenant_id="TENANT-01",
            site_id="SITE-01",
            source_id="CAM-07",
            source_name="Loading bay camera CAM-07",
            analytics_type="intrusion_detection",
            event_type="intrusion_detected",
            severity="high",
        )
    )

`Deployment.discover()` is for our own scripts; a third-party producer is given a
URL and an audience and does not read our deployment.
"""

from .auth import AuthenticationFailed, AzureCliToken, TokenSource
from .delivery import DeliveryFailed, IncidentProducer, Receipt
from .discovery import Deployment, DiscoveryFailed

# Safe to export without the `evidence` extra installed: this module imports no
# Azure SDK until something actually uploads.
from .evidence import EvidenceUploader, EvidenceUploadFailed
from .event import InvalidEvent, build, to_json, validate

__all__ = [
    "AuthenticationFailed",
    "AzureCliToken",
    "DeliveryFailed",
    "Deployment",
    "DiscoveryFailed",
    "EvidenceUploadFailed",
    "EvidenceUploader",
    "IncidentProducer",
    "InvalidEvent",
    "Receipt",
    "TokenSource",
    "build",
    "to_json",
    "validate",
]
