import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth import TokenVerifier, build_verifier, parse_tenant_map

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
DIRECTORY = "directory-id"
AUDIENCE = "api://api-id"


class FakeKeys:
    def get_signing_key_from_jwt(self, token):
        return type("SigningKey", (), {"key": KEY.public_key()})()


def token(**overrides):
    claims = {"iss": f"https://login.microsoftonline.com/{DIRECTORY}/v2.0", "aud": AUDIENCE, "exp": int(time.time()) + 600, "azp": "client-a", "roles": ["Subscriptions.Write"]}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def verifier():
    return TokenVerifier(DIRECTORY, AUDIENCE, {"client-a": "TENANT-A"}, FakeKeys())


def test_signed_token_resolves_trusted_tenant():
    context = verifier().verify(token(tenant_id="TENANT-B"))
    assert context.tenant_id == "TENANT-A"
    assert context.permissions == frozenset({"Subscriptions.Write"})


def test_v1_and_delegated_permissions():
    context = verifier().verify(token(iss=f"https://sts.windows.net/{DIRECTORY}/", azp=None, appid="client-a", roles=None, scp="Subscriptions.Read"))
    assert context.tenant_id == "TENANT-A"
    assert "Subscriptions.Read" in context.permissions


def test_bare_v2_audience():
    assert verifier().verify(token(aud="api-id")).tenant_id == "TENANT-A"


@pytest.mark.parametrize("overrides", [{"exp": 1}, {"exp": None}, {"aud": "other"}, {"iss": "https://other-directory"}])
def test_invalid_claims(overrides):
    with pytest.raises(HTTPException) as error:
        verifier().verify(token(**overrides))
    assert error.value.status_code == 401


def test_wrong_signature():
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token(), options={"verify_signature": False})
    with pytest.raises(HTTPException) as error:
        verifier().verify(jwt.encode(claims, wrong, algorithm="RS256"))
    assert error.value.status_code == 401


@pytest.mark.parametrize("overrides", [{"azp": "unknown"}, {"azp": None}])
def test_unmapped_client(overrides):
    with pytest.raises(HTTPException) as error:
        verifier().verify(token(**overrides))
    assert error.value.status_code == 403


def test_production_is_default_and_fails_without_configuration(monkeypatch):
    for name in ["AUTH_MODE", "AUTH_TENANT_ID", "AUTH_AUDIENCE", "TENANT_MAP"]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        build_verifier()


@pytest.mark.parametrize("mapping", ["malformed", "a:", ":tenant", "a:T,a:OTHER"])
def test_invalid_mapping(mapping):
    with pytest.raises(ValueError):
        parse_tenant_map(mapping)


def test_explicit_development_tenant(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.delenv("DEV_TENANT_ID", raising=False)
    with pytest.raises(ValueError):
        build_verifier()
    monkeypatch.setenv("DEV_TENANT_ID", "TENANT-DEV")
    assert build_verifier().context.tenant_id == "TENANT-DEV"


def test_deeply_nested_untrusted_payload_returns_401():
    # Match main's JWT hardening without making any network request.
    import json
    deep = (
        jwt.utils.base64url_encode(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "x"}).encode())
        + b"." + jwt.utils.base64url_encode(b"[" * 20000 + b"]" * 20000)
        + b"." + jwt.utils.base64url_encode(b"forged")
    ).decode()
    actual = TokenVerifier(DIRECTORY, AUDIENCE, {"client-a": "TENANT-A"},
                           jwt.PyJWKClient("https://127.0.0.1:1/jwks", cache_keys=True))
    with pytest.raises(HTTPException) as error:
        actual.verify(deep)
    assert error.value.status_code == 401
