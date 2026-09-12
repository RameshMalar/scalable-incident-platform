"""Authentication and tenant isolation.

Tokens here are signed with a throwaway RSA key generated in-process, so the
whole validation path -- signature, issuer, audience, expiry, role, tenant
binding -- is exercised without an Entra tenant or a network call.
"""

import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import (
    AuthError,
    OpenAccess,
    Producer,
    TokenVerifier,
    _parse_tenant_map,
    auth_mode,
    enforce_tenant,
)

TENANT = "5ba5ef5e-3109-4e77-85bd-cfeb0d347e82"
AUDIENCE = "api://fbbe63e1-1f8f-4d4d-b695-0f9982842081"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
PRODUCER_01 = "0c1ab600-9868-4b9f-8e60-b2e04c3bd9e8"
PRODUCER_02 = "11111111-2222-3333-4444-555555555555"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)


class FakeJWKClient:
    """Stands in for Entra's JWKS endpoint, serving the throwaway public key."""

    def __init__(self, key=None):
        self._key = key or _key.public_key()
        self.fetches = 0

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self._key})()

    def fetch_data(self):
        self.fetches += 1


def make_token(**overrides):
    claims = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "azp": PRODUCER_01,
        "roles": ["Incidents.Write"],
        "tid": TENANT,
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is None]:
        del claims[key]
    return jwt.encode(claims, _private_pem, algorithm="RS256")


def build_verifier(tenant_map=None, jwk_client=None):
    return TokenVerifier(
        tenant_id=TENANT,
        audience=AUDIENCE,
        required_role="Incidents.Write",
        tenant_map=tenant_map if tenant_map is not None else {PRODUCER_01: "TENANT-01"},
        jwk_client=jwk_client or FakeJWKClient(),
    )


# --- authentication --------------------------------------------------------


def test_a_valid_token_resolves_to_its_producer():
    producer = build_verifier().verify(make_token())

    assert producer == Producer(client_id=PRODUCER_01, tenant_id="TENANT-01")


def test_an_expired_token_is_rejected():
    token = make_token(exp=int(time.time()) - 60)

    with pytest.raises(AuthError) as exc:
        build_verifier().verify(token)

    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_a_token_for_another_api_is_rejected():
    # A token the caller legitimately holds for some other service must not
    # open this one.
    token = make_token(aud="api://some-other-service")

    with pytest.raises(AuthError) as exc:
        build_verifier().verify(token)

    assert "different API" in exc.value.detail


def test_a_token_from_another_directory_is_rejected():
    token = make_token(iss="https://login.microsoftonline.com/someone-else/v2.0")

    with pytest.raises(AuthError) as exc:
        build_verifier().verify(token)

    assert "different tenant" in exc.value.detail


def test_a_token_signed_by_the_wrong_key_is_rejected():
    # The whole point of checking the signature: a self-issued token with
    # perfect claims must still fail.
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(AuthError):
        build_verifier(jwk_client=FakeJWKClient(impostor.public_key())).verify(
            make_token()
        )


def test_a_token_without_the_role_is_forbidden():
    token = make_token(roles=["Incidents.Read"])

    with pytest.raises(AuthError) as exc:
        build_verifier().verify(token)

    assert exc.value.status_code == 403


def test_a_delegated_scope_is_accepted_as_well_as_an_app_role():
    # Producers are services and carry an app role; a human operator or a test
    # client presents the same right as a delegated scope.
    token = make_token(roles=None, scp="Incidents.Write User.Read")

    assert build_verifier().verify(token).tenant_id == "TENANT-01"


def test_an_unregistered_client_is_forbidden():
    # A valid token from a client nobody has mapped to a tenant cannot publish,
    # because there is no correct tenant to attribute the incident to.
    token = make_token(azp=PRODUCER_02)

    with pytest.raises(AuthError) as exc:
        build_verifier().verify(token)

    assert exc.value.status_code == 403
    assert "not registered" in exc.value.detail


def test_appid_is_read_when_azp_is_absent():
    # v1.0 tokens carry appid where v2.0 carries azp.
    token = make_token(azp=None, appid=PRODUCER_01)

    assert build_verifier().verify(token).client_id == PRODUCER_01


# --- tenant isolation ------------------------------------------------------


def test_a_producer_may_publish_for_its_own_tenant():
    enforce_tenant(Producer(PRODUCER_01, "TENANT-01"), "TENANT-01")


def test_a_producer_may_not_publish_for_another_tenant():
    # The control that authentication alone does not provide: tenant_id is an
    # ordinary field of the event, so a valid producer could otherwise attribute
    # incidents to another customer.
    with pytest.raises(AuthError) as exc:
        enforce_tenant(Producer(PRODUCER_01, "TENANT-01"), "TENANT-02")

    assert exc.value.status_code == 403
    assert "may not publish" in exc.value.detail


def test_no_tenant_check_when_authentication_is_disabled():
    enforce_tenant(None, "ANY-TENANT")


# --- configuration ---------------------------------------------------------


def test_the_tenant_map_is_read_from_configuration():
    mapping = _parse_tenant_map(f" {PRODUCER_01}:TENANT-01 , {PRODUCER_02}:TENANT-02 ")

    assert mapping == {PRODUCER_01: "TENANT-01", PRODUCER_02: "TENANT-02"}


def test_a_malformed_mapping_entry_costs_only_that_producer():
    # A typo in one entry must not take the ingestion service down at startup.
    mapping = _parse_tenant_map(f"{PRODUCER_01}:TENANT-01,broken-entry,:,{PRODUCER_02}:")

    assert mapping == {PRODUCER_01: "TENANT-01"}


def test_entra_mode_requires_a_tenant_and_an_audience(monkeypatch):
    monkeypatch.delenv("AUTH_TENANT_ID", raising=False)
    monkeypatch.delenv("AUTH_AUDIENCE", raising=False)

    with pytest.raises(ValueError):
        TokenVerifier()


def test_auth_mode_names_what_is_actually_running():
    assert auth_mode(build_verifier()) == "entra"
    assert auth_mode(OpenAccess()) == "disabled"
