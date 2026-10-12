"""Image-local compatibility gate. No Azure, Blob or Telegram transport is used."""
import asyncio
import json
from pathlib import Path
import socket
import sys

# The image WORKDIR is /app; the script is mounted at /verify.py.
sys.path.insert(0, str(Path.cwd()))

from common_event_model import IncidentEvent
from app.notifier import MockNotifier
from app.processor import InvalidIncident, NotificationProcessor


async def verify():
    event = IncidentEvent(
        event_id="EVT-COMPATIBILITY", tenant_id="TENANT-LOCAL",
        site_id="SITE-LOCAL", source_id="CAM-LOCAL", source_name="Synthetic",
        source_type="video_analytics", analytics_type="intrusion_detection",
        event_type="intrusion_detected", severity="low",
        timestamp="2026-01-01T00:00:00Z", event_purpose="e2e_test",
        evidence_uri="https://synthetic.invalid/evidence/local.jpg",
    )
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier)
    if not processor.accept_legacy:
        raise RuntimeError("Legacy compatibility disabled")
    await processor.process(event.model_dump_json())
    envelope = {
        "schema_version": 1, "subscription_id": "SUB-LOCAL",
        "channel": "telegram", "telegram_chat_id": "-100000001",
        "incident": event.model_dump(mode="json"),
    }
    await processor.process(json.dumps(envelope))
    if notifier.sent_destinations != [None, envelope["telegram_chat_id"]]:
        raise RuntimeError("Flat/global and routed destination selection changed")
    if any(item.model_dump() != event.model_dump() for item in notifier.sent_events):
        raise RuntimeError("Incident content was changed")
    for invalid in [envelope | {"schema_version": 2}, envelope | {"telegram_chat_id": "invalid"}]:
        try:
            await processor.process(json.dumps(invalid))
        except InvalidIncident:
            pass
        else:
            raise RuntimeError("Invalid routed message was accepted")
    if len(notifier.sent_events) != 2:
        raise RuntimeError("Invalid routed message fell back to a notification")
    print("Legacy flat and v1 routed messages accepted; invalid envelopes rejected; external delivery mocked")


if __name__ == "__main__":
    def blocked(*args, **kwargs):
        raise RuntimeError("External network prohibited in image compatibility check")
    # Create the loop before blocking connections: Windows uses an internal
    # loopback socketpair for its event loop. No external client is instantiated.
    with asyncio.Runner() as runner:
        runner.get_loop()
        socket.socket.connect = blocked
        socket.socket.connect_ex = blocked
        runner.run(verify())
