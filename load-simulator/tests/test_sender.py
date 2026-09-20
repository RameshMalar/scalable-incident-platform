import httpx

from app.auth import Producer, StaticTokenProvider
from app.contract import IncidentGenerator
from app.metrics import Recorder
from app.profile import Profile
from app.sender import LoadDriver

ALPHA = Producer(name="alpha", tenant_id="TENANT-01")
BETA = Producer(name="beta", tenant_id="TENANT-02")

TARGET = "https://ingestion.invalid/api/v1/incidents"


class FailingTokenProvider:
    async def token_for(self, producer):
        raise RuntimeError("the directory is unreachable")


def driver(client, recorder, producers=(ALPHA,), tokens=None, **kwargs):
    return LoadDriver(
        client=client,
        tokens=tokens or StaticTokenProvider("a-token"),
        producers=list(producers),
        generator=IncidentGenerator(seed=1, **kwargs),
        recorder=recorder,
        target_url=TARGET,
    )


async def test_every_scheduled_request_is_sent_and_recorded():
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(202, json={"status": "accepted"})

    profile = Profile.from_specs(["1000:0.005", "1000:0.005"])
    recorder = Recorder()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder).run(profile)

    assert len(received) == profile.total_requests
    assert len(recorder.samples) == profile.total_requests
    assert recorder.summary()["overall"]["accepted"] == profile.total_requests


async def test_each_stage_labels_its_own_requests():
    def handler(request):
        return httpx.Response(202)

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder).run(Profile.from_specs(["1000:0.003", "500:0.006"]))

    stages = {stage["stage"]: stage["requests"] for stage in recorder.summary()["stages"]}

    assert stages == {"stage1-1000rps": 3, "stage2-500rps": 3}


async def test_the_request_carries_the_producer_token_and_the_contract_body():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202)

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder).run(Profile.from_specs(["1000:0.002"]))

    request = seen[0]
    assert request.url == TARGET
    assert request.headers["authorization"] == "Bearer a-token"
    assert request.headers["content-type"] == "application/json"


async def test_load_is_spread_across_producers_so_tenants_are_concurrent():
    tenants = []

    def handler(request):
        import json

        tenants.append(json.loads(request.content)["tenant_id"])
        return httpx.Response(202)

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder, producers=(ALPHA, BETA)).run(
            Profile.from_specs(["1000:0.006"])
        )

    assert tenants == ["TENANT-01", "TENANT-02"] * 3
    assert {sample.producer for sample in recorder.samples} == {"alpha", "beta"}


async def test_a_transport_failure_is_recorded_rather_than_raised():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder).run(Profile.from_specs(["1000:0.003"]))

    overall = recorder.summary()["overall"]
    assert overall["requests"] == 3
    assert overall["accepted"] == 0
    assert overall["by_status"] == {"no_response": 3}
    assert overall["errors"] == ["ConnectError"]


async def test_a_token_failure_is_distinguished_from_a_platform_failure():
    def handler(request):  # pragma: no cover - never reached
        return httpx.Response(202)

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder, tokens=FailingTokenProvider()).run(
            Profile.from_specs(["1000:0.002"])
        )

    assert recorder.summary()["overall"]["errors"] == ["token: RuntimeError"]


async def test_rejections_are_kept_as_results_not_swallowed():
    def handler(request):
        return httpx.Response(429, json={"detail": "throttled"})

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder).run(Profile.from_specs(["1000:0.003"]))

    assert recorder.summary()["overall"]["by_status"] == {"429": 3}


async def test_duplicate_ids_reach_the_platform_when_they_are_asked_for():
    ids = []

    def handler(request):
        import json

        ids.append(json.loads(request.content)["event_id"])
        return httpx.Response(202)

    recorder = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await driver(client, recorder, duplicate_rate=1.0).run(
            Profile.from_specs(["1000:0.004"])
        )

    assert len(set(ids)) == 1
    assert recorder.summary()["overall"]["duplicates_sent"] == 3
