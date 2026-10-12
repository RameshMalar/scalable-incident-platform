"""Run one existing service suite with external sockets prohibited."""
import ipaddress
import os
from pathlib import Path
import socket
import sys

import pytest


def guard(original):
    def guarded(sock, address, *args, **kwargs):
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError):
            allowed = False
        if not allowed:
            raise AssertionError("External network prohibited in local verification")
        return original(sock, address, *args, **kwargs)
    return guarded


if __name__ == "__main__":
    service = sys.argv[1]
    if service not in {"worker-service", "notification-service", "subscription-service"}:
        raise SystemExit("Expected a Phase 3 service directory")
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "packages/common-event-model/src"))
    os.chdir(root / service)
    sys.path.insert(0, str(root / service))
    for key in list(os.environ):
        if key.startswith(("AZURE_", "COSMOS_", "SERVICE_BUS_", "TELEGRAM_", "EVIDENCE_")) or key in {"AUTH_MODE", "AUTH_AUDIENCE", "TENANT_MAP", "SUBSCRIPTION_STORE", "INCIDENT_STORE"}:
            del os.environ[key]
    socket.socket.connect = guard(socket.socket.connect)
    socket.socket.connect_ex = guard(socket.socket.connect_ex)
    print(f"Python {sys.version.split()[0]} | {service} | external network blocked", flush=True)
    raise SystemExit(pytest.main(["tests", "-q", "--junitxml=" + str(root / "tests" / "local_integration" / (service + "-results.xml"))]))
