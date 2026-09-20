import httpx
import pytest

from app.auth import (
    ClientCredentialsTokenProvider,
    ManagedIdentityTokenProvider,
    Producer,
    StaticTokenProvider,
    TokenError,
)

ALPHA = Producer(
    name="alpha", tenant_id="TENANT-01", client_id="client-a", client_secret="secret-a"
)
BETA = Producer(
    name="beta", tenant_id="TENANT-02", client_id="client-b", client_secret="secret-b"
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def directory(handler):
    """A client whose only route is the token endpoint."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_static_token_is_handed_to_every_producer():
    provider = StaticTokenProvider("a-token")

    assert await provider.token_for(ALPHA) == "a-token"
    assert await provider.token_for(BETA) == "a-token"


def test_a_static_provider_needs_a_token():
    with pytest.raises(ValueError):
        StaticTokenProvider("")


async def test_the_token_is_requested_once_and_then_reused():
    calls = []

    def handler(request):
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "t1", "expires_in": 3600})

    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=FakeClock()
        )

        tokens = [await provider.token_for(ALPHA) for _ in range(5)]

    assert tokens == ["t1"] * 5
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "client_credentials"
    assert calls[0]["client_id"] == "client-a"
    assert calls[0]["scope"] == "api://platform/.default"


async def test_each_producer_gets_its_own_token():
    def handler(request):
        form = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(
            200, json={"access_token": f"token-for-{form['client_id']}", "expires_in": 3600}
        )

    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=FakeClock()
        )

        assert await provider.token_for(ALPHA) == "token-for-client-a"
        assert await provider.token_for(BETA) == "token-for-client-b"


async def test_the_token_is_renewed_before_it_expires():
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(
            200, json={"access_token": f"t{len(issued)}", "expires_in": 300}
        )

    clock = FakeClock()
    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=clock
        )

        assert await provider.token_for(ALPHA) == "t1"

        # Still inside the validity window, minus the renewal margin.
        clock.now = 200
        assert await provider.token_for(ALPHA) == "t1"

        # Past it, but before the token itself would have expired: the run
        # must not be the one to discover the expiry.
        clock.now = 250
        assert await provider.token_for(ALPHA) == "t2"


async def test_a_refusal_from_the_directory_says_what_it_said():
    def handler(request):
        return httpx.Response(400, text="AADSTS7000215: invalid client secret")

    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=FakeClock()
        )

        with pytest.raises(TokenError, match="AADSTS7000215"):
            await provider.token_for(ALPHA)


async def test_a_response_without_a_token_is_an_error_rather_than_an_empty_header():
    def handler(request):
        return httpx.Response(200, json={"expires_in": 3600})

    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=FakeClock()
        )

        with pytest.raises(TokenError):
            await provider.token_for(ALPHA)


async def test_a_producer_with_no_credentials_is_named_in_the_error():
    def handler(request):  # pragma: no cover - never reached
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    async with directory(handler) as client:
        provider = ClientCredentialsTokenProvider(
            client, "dir-tenant", "api://platform", clock=FakeClock()
        )

        with pytest.raises(TokenError, match="gamma"):
            await provider.token_for(Producer(name="gamma", tenant_id="TENANT-03"))


IDENTITY_ENDPOINT = "http://169.254.0.2/identity/oauth2/token"


def managed_identity(handler, clock=None, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ManagedIdentityTokenProvider(
        client=client,
        identity_endpoint=IDENTITY_ENDPOINT,
        identity_header="a-header",
        audience="api://platform",
        clock=clock or FakeClock(),
        **kwargs,
    )
    return client, provider


async def test_the_identity_endpoint_is_asked_for_a_token_for_one_identity():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"access_token": "mi-token", "expires_on": "600"}
        )

    client, provider = managed_identity(handler)
    async with client:
        assert await provider.token_for(ALPHA) == "mi-token"

    request = seen[0]
    assert request.url.params["resource"] == "api://platform"
    assert request.url.params["client_id"] == "client-a"
    assert request.url.params["api-version"]
    # The secret is the environment's: this header is what proves the caller
    # is inside the container the identities are attached to.
    assert request.headers["x-identity-header"] == "a-header"


async def test_each_identity_gets_its_own_token_and_it_is_cached():
    calls = []

    def handler(request):
        calls.append(request.url.params["client_id"])
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{request.url.params['client_id']}",
                "expires_on": "600",
            },
        )

    client, provider = managed_identity(handler)
    async with client:
        assert await provider.token_for(ALPHA) == "token-client-a"
        assert await provider.token_for(BETA) == "token-client-b"
        assert await provider.token_for(ALPHA) == "token-client-a"

    assert calls == ["client-a", "client-b"]


async def test_an_absolute_expiry_is_read_as_a_time_not_a_duration():
    """`expires_on` is epoch seconds here.

    Read as a lifetime it would cache the token for sixty thousand years, and
    the run would keep presenting it long after the platform stopped accepting
    it.
    """
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(
            200, json={"access_token": f"t{len(issued)}", "expires_on": "1790000000"}
        )

    clock = FakeClock()
    # 1,000 seconds of validity left at the moment of the request.
    client, provider = managed_identity(
        handler, clock=clock, wall_clock=lambda: 1789999000.0
    )
    async with client:
        assert await provider.token_for(ALPHA) == "t1"

        clock.now = 900
        assert await provider.token_for(ALPHA) == "t1"

        clock.now = 950
        assert await provider.token_for(ALPHA) == "t2"


async def test_an_unparseable_expiry_falls_back_to_a_nominal_lifetime():
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(
            200,
            json={
                "access_token": f"t{len(issued)}",
                "expires_on": "09/20/2026 12:00:00 +00:00",
            },
        )

    clock = FakeClock()
    client, provider = managed_identity(handler, clock=clock)
    async with client:
        assert await provider.token_for(ALPHA) == "t1"

        clock.now = 3500
        assert await provider.token_for(ALPHA) == "t1"

        clock.now = 3550
        assert await provider.token_for(ALPHA) == "t2"


async def test_a_refusal_from_the_identity_endpoint_carries_its_body():
    def handler(request):
        return httpx.Response(400, text="identity not found on this resource")

    client, provider = managed_identity(handler)
    async with client:
        with pytest.raises(TokenError, match="identity not found"):
            await provider.token_for(ALPHA)


async def test_an_unreachable_identity_endpoint_is_a_token_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    client, provider = managed_identity(handler)
    async with client:
        with pytest.raises(TokenError, match="could not be reached"):
            await provider.token_for(ALPHA)


async def test_an_identity_without_a_client_id_cannot_be_selected():
    def handler(request):  # pragma: no cover - never reached
        return httpx.Response(200, json={"access_token": "t"})

    client, provider = managed_identity(handler)
    async with client:
        with pytest.raises(TokenError, match="gamma"):
            await provider.token_for(Producer(name="gamma", tenant_id="TENANT-03"))
