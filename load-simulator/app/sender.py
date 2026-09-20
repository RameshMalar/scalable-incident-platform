"""Drives the load: open-loop, at the rate the profile asks for.

Open-loop means a request is issued when its slot comes due, not when the
previous one came back. A closed-loop generator -- send, await, send -- cannot
exceed `concurrency / latency` requests per second, so the moment the platform
slows down the generator quietly slows with it. The queue never builds, the
autoscaler never sees a backlog, and the run reports a system that coped.
Backlog is the thing being tested here, so the schedule has to be independent
of the responses.

Requests are spread across producers round-robin, each with its own token and
its own tenant. Incidents for several tenants are therefore in flight at once,
which is the condition tenant isolation has to hold under and cannot be shown
by sending one at a time.
"""

import asyncio
import time

import httpx

from .auth import Producer
from .contract import IncidentGenerator
from .metrics import Recorder, Sample
from .profile import Profile, Stage


class LoadDriver:
    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens,
        producers: list[Producer],
        generator: IncidentGenerator,
        recorder: Recorder,
        target_url: str,
        max_inflight: int = 200,
        clock=time.perf_counter,
    ):
        if not producers:
            raise ValueError("the driver needs at least one producer")

        self._client = client
        self._tokens = tokens
        self._producers = producers
        self._generator = generator
        self._recorder = recorder
        self._target_url = target_url
        self._inflight = asyncio.Semaphore(max_inflight)
        self._clock = clock

    async def run(self, profile: Profile) -> None:
        run_start = self._clock()
        pending: set[asyncio.Task] = set()
        sequence = 0

        for stage in profile.stages:
            sequence = await self._run_stage(stage, run_start, pending, sequence)

        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    async def _run_stage(
        self,
        stage: Stage,
        run_start: float,
        pending: set[asyncio.Task],
        sequence: int,
    ) -> int:
        stage_start = self._clock()
        interval = 1.0 / stage.rate

        for slot in range(stage.requests):
            due = stage_start + slot * interval
            now = self._clock()
            if due > now:
                await asyncio.sleep(due - now)

            # Acquired before the request is created rather than inside it, so
            # running out of slots shows up as schedule lag -- which is
            # reported -- instead of as unbounded memory and a latency figure
            # that silently includes queueing inside the generator.
            await self._inflight.acquire()
            lag_ms = max(0.0, (self._clock() - due) * 1000.0)

            producer = self._producers[sequence % len(self._producers)]
            sequence += 1
            generated = self._generator.next_event(producer.tenant_id)

            task = asyncio.create_task(
                self._send(
                    stage=stage.label,
                    producer=producer,
                    generated=generated,
                    sent_at=self._clock() - run_start,
                    lag_ms=lag_ms,
                )
            )
            pending.add(task)
            task.add_done_callback(pending.discard)

        return sequence

    async def _send(self, stage, producer, generated, sent_at, lag_ms) -> None:
        payload = generated.payload

        def record(status: int, latency_ms: float, error: str | None = None) -> None:
            self._recorder.add(
                Sample(
                    stage=stage,
                    producer=producer.name,
                    tenant_id=payload["tenant_id"],
                    event_id=payload["event_id"],
                    duplicate=generated.duplicate,
                    status=status,
                    latency_ms=latency_ms,
                    sent_at=sent_at,
                    lag_ms=lag_ms,
                    error=error,
                )
            )

        try:
            try:
                token = await self._tokens.token_for(producer)
            except Exception as exc:
                # A directory failure is not a platform result, so it is
                # recorded as its own error rather than as a failed incident.
                record(0, 0.0, f"token: {type(exc).__name__}")
                return

            started = self._clock()
            try:
                response = await self._client.post(
                    self._target_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "content-type": "application/json",
                    },
                )
            except Exception as exc:
                record(0, (self._clock() - started) * 1000.0, type(exc).__name__)
                return

            record(response.status_code, (self._clock() - started) * 1000.0)
        finally:
            self._inflight.release()
