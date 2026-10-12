"""Local infrastructure wiring guards for the opt-in queue migration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_queue_is_additive_opt_in_with_scoped_identity_roles():
    text = (ROOT / "infra/notification-dedup.bicep").read_text()
    assert "param provisionQueue bool = false" in text
    assert text.count("= if (provisionQueue)") == 4
    assert "requiresDuplicateDetection: true" in text
    assert "duplicateDetectionHistoryTimeWindow: 'P1D'" in text
    assert "enablePartitioning: false" in text
    allowed = text.split("@allowed([", 1)[1].split("])", 1)[0]
    assert "'notification-events'" not in allowed
    assert text.count("scope: queue") == 3
    assert "scope: namespace" not in text
    assert "listKeys" not in text
    assert "principalType: 'ServicePrincipal'" in text


def test_existing_queue_and_deployments_remain_default():
    core = (ROOT / "infra/core.bicep").read_text()
    queue = core.split("resource notificationQueue ", 1)[1].split("// Service Bus RBAC", 1)[0]
    assert "requiresDuplicateDetection" not in queue
    assert "var notificationQueueName = 'notification-events'" in core
    assert "disableLocalAuth: true" in core
    workflow = (ROOT / ".github/workflows/cd.yml").read_text()
    assert "notification-dedup.bicep" not in workflow  # provisioning remains separate
    assert "python3 scripts/verify-notification-queue.py" in workflow
    assert "serviceBusQueue=\'${{ steps.queue.outputs.name }}\'" in workflow
    assert "notificationQueue=\'${{ needs.prepare.outputs.notification_queue }}\'" in workflow
    worker = (ROOT / "infra/worker.bicep").read_text()
    notification = (ROOT / "infra/notification.bicep").read_text()
    assert "param notificationQueue string = 'notification-events'" in worker
    assert "name: 'NOTIFICATION_QUEUE'\n              value: notificationQueue" in worker
    assert "param serviceBusQueue string = 'notification-events'" in notification
    assert "name: 'SERVICE_BUS_QUEUE'\n              value: serviceBusQueue" in notification
    assert "queueName: serviceBusQueue" in notification
    assert "identity: scalerIdentityId" in notification
