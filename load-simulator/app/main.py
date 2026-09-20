"""Command line entry point for the load simulator.

Every option can also come from an environment variable, because the same
image runs as a Container Apps job in-region -- where there is no command line
to type -- and locally against a deployed URL.

Producer secrets are read from the environment only, never from an argument. A
client secret on the command line is recorded in shell history and is visible
in the process list to anyone on the host.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .auth import (
    IDENTITY_ENDPOINT_ENV,
    IDENTITY_HEADER_ENV,
    ClientCredentialsTokenProvider,
    ManagedIdentityTokenProvider,
    Producer,
    StaticTokenProvider,
)
from .contract import IncidentGenerator
from .metrics import Recorder
from .profile import Profile
from .sender import LoadDriver

# The contract endpoint, not /api/v1/simulate/intrusion: the simulate route
# answers 200 with an error body when the publish fails, so a broken platform
# would look like a fully accepted run.
INCIDENTS_PATH = "/api/v1/incidents"

SECRET_ENV_PREFIX = "LOAD_SIM_SECRET_"

# The summary is printed as one line of JSON as well as written to disk. Run as
# a Container Apps job there is no volume to read afterwards and no exit code
# rich enough to carry a result, so the log stream has to be able to carry the
# whole thing. The marker is what makes it greppable out of everything else the
# run logs.
SUMMARY_MARKER = "LOAD_SUMMARY_JSON"

STATIC = "static"
CLIENT_CREDENTIALS = "client-credentials"
MANAGED_IDENTITY = "managed-identity"

# Cosmos free tier: 1,000 RU/s against a measured 9.9 RU per incident write.
# Printed with the results so a rate above it is read as the planned overshoot
# it is, rather than as an unexplained wall of 429s.
WRITE_CEILING_RPS = 101


class ConfigurationError(Exception):
    """Something the run cannot be started without is missing."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="load-simulator",
        description="Drives synthetic incidents at the incident platform.",
    )
    parser.add_argument(
        "--target-url",
        default=os.environ.get("TARGET_URL", ""),
        help="Base URL of the deployed ingestion service. Env: TARGET_URL",
    )
    parser.add_argument(
        "--stage",
        action="append",
        default=[],
        metavar="RATE:SECONDS",
        help=(
            "A constant-rate stage, repeatable and applied in order, "
            "e.g. --stage 20:60 --stage 100:120. Env: LOAD_STAGES (comma separated)"
        ),
    )
    parser.add_argument(
        "--producer",
        action="append",
        default=[],
        metavar="NAME:TENANT_ID[:CLIENT_ID]",
        help=(
            "A simulated producer and the tenant it is registered for. "
            f"Its secret is read from {SECRET_ENV_PREFIX}<NAME>. "
            "Env: LOAD_PRODUCERS (comma separated)"
        ),
    )
    parser.add_argument(
        "--audience",
        default=os.environ.get("AUTH_AUDIENCE", ""),
        help="The API's Application ID URI, api://<appId>. Env: AUTH_AUDIENCE",
    )
    parser.add_argument(
        "--directory-tenant",
        default=os.environ.get("AUTH_TENANT_ID", ""),
        help="Entra directory the producers are registered in. Env: AUTH_TENANT_ID",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("STATIC_TOKEN", ""),
        help=(
            "Use one pre-issued token for every request instead of client "
            "credentials. For first local runs; cannot demonstrate tenant "
            "isolation. Env: STATIC_TOKEN"
        ),
    )
    parser.add_argument(
        "--managed-identity",
        action="store_true",
        default=bool(os.environ.get("MANAGED_IDENTITY")),
        help=(
            "Take each producer's token from the platform's identity endpoint "
            "instead of a client secret. Inferred when running somewhere that "
            "provides one. Env: MANAGED_IDENTITY"
        ),
    )
    parser.add_argument(
        "--duplicate-rate",
        type=float,
        default=float(os.environ.get("DUPLICATE_RATE", 0.0)),
        help="Fraction of incidents re-sent under an id already used. Env: DUPLICATE_RATE",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ.get("SEED", 0)),
        help="Makes the run reproducible. Env: SEED",
    )
    parser.add_argument("--max-inflight", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("OUT_DIR", "results"),
        help="Where summary.json and samples.csv are written. Env: OUT_DIR",
    )
    return parser.parse_args(argv)


def _from_env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_producers(specs: list[str], environ: dict[str, str]) -> list[Producer]:
    producers = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigurationError(
                f"producer {spec!r} is not in name:tenant_id[:client_id] form"
            )
        name = parts[0].strip()
        producers.append(
            Producer(
                name=name,
                tenant_id=parts[1].strip(),
                client_id=parts[2].strip() if len(parts) > 2 else "",
                client_secret=environ.get(f"{SECRET_ENV_PREFIX}{name.upper()}", ""),
            )
        )
    return producers


def auth_mode(args: argparse.Namespace, environ: dict[str, str]) -> str:
    """Decide where tokens come from.

    A pre-issued token wins, because someone who supplied one means to use it.
    Otherwise the platform's identity endpoint is preferred when there is one:
    the deployed job has identities attached and no secret to offer, and
    guessing wrong there would send it looking for a secret it should not have.
    """
    if args.token:
        return STATIC
    if args.managed_identity or (
        environ.get(IDENTITY_ENDPOINT_ENV) and environ.get(IDENTITY_HEADER_ENV)
    ):
        return MANAGED_IDENTITY
    return CLIENT_CREDENTIALS


def resolve(args: argparse.Namespace) -> tuple[str, Profile, list[Producer]]:
    environ = dict(os.environ)

    if not args.target_url:
        raise ConfigurationError("TARGET_URL is required")

    stages = args.stage or _from_env_list("LOAD_STAGES")
    if not stages:
        raise ConfigurationError("LOAD_STAGES is required, e.g. 20:60,100:120")

    producer_specs = args.producer or _from_env_list("LOAD_PRODUCERS")
    if not producer_specs:
        raise ConfigurationError(
            "LOAD_PRODUCERS is required, e.g. alpha:TENANT-01:<client id>"
        )

    producers = build_producers(producer_specs, environ)
    mode = auth_mode(args, environ)

    if mode != STATIC:
        if not args.audience:
            raise ConfigurationError(
                "AUTH_AUDIENCE is required unless STATIC_TOKEN is set"
            )

        nameless = [p.name for p in producers if not p.client_id]
        if nameless:
            raise ConfigurationError(
                f"no client id for: {', '.join(nameless)}"
            )

    if mode == MANAGED_IDENTITY:
        if not (
            environ.get(IDENTITY_ENDPOINT_ENV) and environ.get(IDENTITY_HEADER_ENV)
        ):
            raise ConfigurationError(
                f"{IDENTITY_ENDPOINT_ENV} and {IDENTITY_HEADER_ENV} are required "
                "for managed identity; nothing here provides one"
            )

    if mode == CLIENT_CREDENTIALS:
        if not args.directory_tenant:
            raise ConfigurationError(
                "AUTH_TENANT_ID is required for client credentials"
            )
        without_secret = [p.name for p in producers if not p.client_secret]
        if without_secret:
            raise ConfigurationError(
                f"no {SECRET_ENV_PREFIX}<NAME> secret for: {', '.join(without_secret)}"
            )

    return (
        args.target_url.rstrip("/") + INCIDENTS_PATH,
        Profile.from_specs(stages),
        producers,
    )


def build_token_provider(args: argparse.Namespace, client: httpx.AsyncClient):
    mode = auth_mode(args, dict(os.environ))
    if mode == STATIC:
        return StaticTokenProvider(args.token)
    if mode == MANAGED_IDENTITY:
        return ManagedIdentityTokenProvider(
            client=client,
            identity_endpoint=os.environ[IDENTITY_ENDPOINT_ENV],
            identity_header=os.environ[IDENTITY_HEADER_ENV],
            audience=args.audience,
        )
    return ClientCredentialsTokenProvider(
        client=client,
        directory_tenant_id=args.directory_tenant,
        audience=args.audience,
    )


async def run(args: argparse.Namespace) -> Recorder:
    endpoint, profile, producers = resolve(args)
    recorder = Recorder()

    limits = httpx.Limits(
        max_connections=args.max_inflight,
        max_keepalive_connections=args.max_inflight,
    )
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        tokens = build_token_provider(args, client)
        driver = LoadDriver(
            client=client,
            tokens=tokens,
            producers=producers,
            generator=IncidentGenerator(
                seed=args.seed, duplicate_rate=args.duplicate_rate
            ),
            recorder=recorder,
            target_url=endpoint,
            max_inflight=args.max_inflight,
        )

        print(
            f"driving {profile.total_requests} incidents over "
            f"{profile.total_duration:g}s at {endpoint} "
            f"as {len(producers)} producer(s) "
            f"authenticating by {auth_mode(args, dict(os.environ))}; "
            f"the store sustains about {WRITE_CEILING_RPS} writes/s"
        )
        await driver.run(profile)

    return recorder


def report(recorder: Recorder, args: argparse.Namespace, started: datetime) -> int:
    summary = recorder.summary()
    run = {
        "started_at": started.isoformat(),
        "target": args.target_url,
        "stages": args.stage or _from_env_list("LOAD_STAGES"),
        "seed": args.seed,
        "duplicate_rate": args.duplicate_rate,
        "max_inflight": args.max_inflight,
        "write_ceiling_rps": WRITE_CEILING_RPS,
    }
    out_dir = Path(args.out_dir)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")

    try:
        recorder.write_json(out_dir / f"summary-{stamp}.json", run=run)
        recorder.write_csv(out_dir / f"samples-{stamp}.csv")
        written = f"{out_dir}/summary-{stamp}.json and samples-{stamp}.csv"
    except OSError as exc:
        # A read-only filesystem must not discard a run that has already been
        # driven: the summary below is on stdout either way.
        written = None
        print(f"WARNING: could not write results to {out_dir}: {exc}", file=sys.stderr)

    for stage in summary["stages"]:
        print(
            f"{stage['stage']}: {stage['requests']} sent, "
            f"{stage['accepted']} accepted, "
            f"{stage['achieved_rps']} rps achieved, "
            f"p95 {stage['latency_ms']['p95']}ms, "
            f"statuses {stage['by_status']}"
        )

    overall = summary["overall"]
    print(f"{SUMMARY_MARKER} {json.dumps({'run': run, **summary})}")
    if written:
        print(f"\nwrote {written}")

    if not overall["generator_kept_up"]:
        print(
            "WARNING: the generator fell behind its schedule "
            f"(max lag {overall['schedule_lag_ms']['max']}ms). "
            "These figures describe the simulator, not the platform: "
            "lower the rate or run more instances.",
            file=sys.stderr,
        )

    if overall["accepted"] == 0:
        print(
            "ERROR: not one incident was accepted. Check the token, the "
            f"tenant mapping and the target URL. Statuses: {overall['by_status']}",
            file=sys.stderr,
        )
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    started = datetime.now(timezone.utc)
    try:
        # Inside the guard: the numeric options default from the environment,
        # so a malformed DUPLICATE_RATE is a configuration fault like any
        # other rather than a traceback.
        args = parse_args(argv)
        recorder = asyncio.run(run(args))
    except ConfigurationError as exc:
        # Exit 2 for configuration, as the worker does, so the container smoke
        # test can tell a misconfigured image from a broken one.
        print(f"{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    return report(recorder, args, started)


if __name__ == "__main__":
    sys.exit(main())
