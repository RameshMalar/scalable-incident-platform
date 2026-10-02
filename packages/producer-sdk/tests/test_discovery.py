"""Reading a deployment, with the Azure CLI faked.

The hostname check is the reason this has tests. A bearer token is sent to
whatever the discovered endpoint resolves to, and the value comes from outside the
process.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from incident_producer import Deployment, DiscoveryFailed

AUDIENCE = "api://fbbe63e1-1f8f-4d4d-b695-0f9982842081"
FQDN = "incident-ingestion.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io"


def fake_az(monkeypatch, *, env=None, fqdn=FQDN, returncode=0, stderr=""):
    """Answers the two `az containerapp show` calls discovery makes."""
    if env is None:
        env = [{"name": "AUTH_AUDIENCE", "value": AUDIENCE}]

    def run(args, capture_output=None, text=None, check=None):
        wants_json = "json" in args
        stdout = json.dumps(env) if wants_json else fqdn

        return subprocess.CompletedProcess(
            args, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("incident_producer.discovery.subprocess.run", run)


def test_a_deployment_is_read_into_an_https_endpoint(monkeypatch):
    fake_az(monkeypatch)

    deployment = Deployment.discover()

    assert deployment.endpoint == f"https://{FQDN}"
    assert deployment.audience == AUDIENCE


@pytest.mark.parametrize(
    "fqdn",
    [
        "host/../path",
        "host:8080",
        "evil.example.com/incident-ingestion",
    ],
)
def test_an_fqdn_that_is_not_a_bare_hostname_is_refused(monkeypatch, fqdn):
    # Without this, a scheme, a port or a path could be substituted into the URL a
    # bearer token is then sent to.
    fake_az(monkeypatch, fqdn=fqdn)

    with pytest.raises(DiscoveryFailed, match="bare hostname"):
        Deployment.discover()


def test_a_deployment_without_an_audience_is_refused(monkeypatch):
    fake_az(monkeypatch, env=[{"name": "SOMETHING_ELSE", "value": "x"}])

    with pytest.raises(DiscoveryFailed, match="AUTH_AUDIENCE"):
        Deployment.discover()


def test_a_deployment_without_ingress_is_refused(monkeypatch):
    fake_az(monkeypatch, fqdn="")

    with pytest.raises(DiscoveryFailed, match="ingress"):
        Deployment.discover()


def test_a_failing_cli_reports_what_it_said(monkeypatch):
    fake_az(monkeypatch, returncode=1, stderr="ERROR: not logged in")

    with pytest.raises(DiscoveryFailed, match="not logged in"):
        Deployment.discover()
