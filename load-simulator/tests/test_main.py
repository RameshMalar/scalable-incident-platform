import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from app.auth import (
    ClientCredentialsTokenProvider,
    ManagedIdentityTokenProvider,
    StaticTokenProvider,
)
from app.main import (
    CLIENT_CREDENTIALS,
    INCIDENTS_PATH,
    MANAGED_IDENTITY,
    STATIC,
    SUMMARY_MARKER,
    ConfigurationError,
    auth_mode,
    build_producers,
    build_token_provider,
    main,
    parse_args,
    report,
    resolve,
)
from app.metrics import Recorder, Sample

ENV_KEYS = (
    "TARGET_URL",
    "LOAD_STAGES",
    "LOAD_PRODUCERS",
    "AUTH_AUDIENCE",
    "AUTH_TENANT_ID",
    "STATIC_TOKEN",
    "OUT_DIR",
    "MANAGED_IDENTITY",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "DUPLICATE_RATE",
    "SEED",
    "MAX_INFLIGHT",
    "REQUEST_TIMEOUT",
    "LOAD_SIM_SECRET_ALPHA",
    "LOAD_SIM_SECRET_BETA",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test inherits the operator's own run configuration."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def args(*argv):
    return parse_args(list(argv))


def test_the_incidents_endpoint_is_appended_to_the_service_url():
    endpoint, _, _ = resolve(
        args(
            "--target-url",
            "https://ingestion.example/",
            "--stage",
            "10:1",
            "--producer",
            "alpha:TENANT-01",
            "--token",
            "a-token",
        )
    )

    assert endpoint == "https://ingestion.example" + INCIDENTS_PATH


def test_the_run_refuses_to_start_without_a_target():
    with pytest.raises(ConfigurationError, match="TARGET_URL"):
        resolve(args("--stage", "10:1", "--producer", "alpha:TENANT-01"))


def test_the_run_refuses_to_start_without_a_profile():
    with pytest.raises(ConfigurationError, match="LOAD_STAGES"):
        resolve(
            args("--target-url", "https://x.invalid", "--producer", "alpha:TENANT-01")
        )


def test_the_run_refuses_to_start_without_a_producer():
    with pytest.raises(ConfigurationError, match="LOAD_PRODUCERS"):
        resolve(args("--target-url", "https://x.invalid", "--stage", "10:1"))


def test_client_credentials_need_a_directory_and_an_audience():
    with pytest.raises(ConfigurationError, match="AUTH_AUDIENCE"):
        resolve(
            args(
                "--target-url",
                "https://x.invalid",
                "--stage",
                "10:1",
                "--producer",
                "alpha:TENANT-01:client-a",
            )
        )


def test_client_credentials_need_the_directory_named_separately(monkeypatch):
    monkeypatch.setenv("AUTH_AUDIENCE", "api://platform")
    monkeypatch.setenv("LOAD_SIM_SECRET_ALPHA", "s3cret")

    with pytest.raises(ConfigurationError, match="AUTH_TENANT_ID"):
        resolve(
            args(
                "--target-url",
                "https://x.invalid",
                "--stage",
                "10:1",
                "--producer",
                "alpha:TENANT-01:client-a",
            )
        )


def cloud(monkeypatch):
    """Stand in for the environment Container Apps provides."""
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://169.254.0.2/identity/oauth2/token")
    monkeypatch.setenv("IDENTITY_HEADER", "a-header")


def test_the_identity_endpoint_is_preferred_where_one_exists(monkeypatch):
    cloud(monkeypatch)

    assert (
        auth_mode(args("--target-url", "https://x.invalid"), dict(os.environ))
        == MANAGED_IDENTITY
    )


def test_a_supplied_token_wins_over_the_identity_endpoint(monkeypatch):
    cloud(monkeypatch)

    assert auth_mode(args("--token", "a-token"), dict(os.environ)) == STATIC


def test_without_an_identity_endpoint_a_secret_is_expected():
    assert auth_mode(args(), {}) == CLIENT_CREDENTIALS


def test_a_managed_identity_run_needs_no_secret_and_no_directory(monkeypatch):
    cloud(monkeypatch)
    monkeypatch.setenv("AUTH_AUDIENCE", "api://platform")

    _, _, producers = resolve(
        args(
            "--target-url",
            "https://x.invalid",
            "--stage",
            "10:1",
            "--producer",
            "alpha:TENANT-01:mi-client-a",
        )
    )

    assert producers[0].client_secret == ""


def test_asking_for_a_managed_identity_where_there_is_none_says_so():
    with pytest.raises(ConfigurationError, match="IDENTITY_ENDPOINT"):
        resolve(
            args(
                "--managed-identity",
                "--target-url",
                "https://x.invalid",
                "--stage",
                "10:1",
                "--producer",
                "alpha:TENANT-01:mi-client-a",
                "--audience",
                "api://platform",
            )
        )


def test_an_identity_run_still_needs_each_producer_identified(monkeypatch):
    cloud(monkeypatch)
    monkeypatch.setenv("AUTH_AUDIENCE", "api://platform")

    with pytest.raises(ConfigurationError, match="no client id for: alpha"):
        resolve(
            args(
                "--target-url",
                "https://x.invalid",
                "--stage",
                "10:1",
                "--producer",
                "alpha:TENANT-01",
            )
        )


@pytest.mark.parametrize(
    "extra_env, argv, expected",
    [
        ({}, ("--token", "a-token"), StaticTokenProvider),
        (
            {
                "IDENTITY_ENDPOINT": "http://169.254.0.2/identity/oauth2/token",
                "IDENTITY_HEADER": "a-header",
            },
            (),
            ManagedIdentityTokenProvider,
        ),
        ({}, (), ClientCredentialsTokenProvider),
    ],
)
def test_each_mode_builds_its_own_provider(monkeypatch, extra_env, argv, expected):
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)

    provider = build_token_provider(
        args("--audience", "api://platform", "--directory-tenant", "dir", *argv),
        httpx.AsyncClient(),
    )

    assert isinstance(provider, expected)


def test_a_producer_without_a_secret_is_named_before_the_run_starts(monkeypatch):
    monkeypatch.setenv("AUTH_AUDIENCE", "api://platform")
    monkeypatch.setenv("AUTH_TENANT_ID", "dir-tenant")

    with pytest.raises(ConfigurationError, match="beta"):
        resolve(
            args(
                "--target-url",
                "https://x.invalid",
                "--stage",
                "10:1",
                "--producer",
                "beta:TENANT-02:client-b",
            )
        )


def test_a_static_token_run_needs_no_directory_at_all():
    _, profile, producers = resolve(
        args(
            "--target-url",
            "https://x.invalid",
            "--stage",
            "10:1",
            "--stage",
            "20:1",
            "--producer",
            "alpha:TENANT-01",
            "--token",
            "a-token",
        )
    )

    assert [stage.rate for stage in profile.stages] == [10.0, 20.0]
    assert [producer.tenant_id for producer in producers] == ["TENANT-01"]


def test_the_profile_and_producers_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TARGET_URL", "https://x.invalid")
    monkeypatch.setenv("LOAD_STAGES", "10:1, 50:2")
    monkeypatch.setenv("LOAD_PRODUCERS", "alpha:TENANT-01")
    monkeypatch.setenv("STATIC_TOKEN", "a-token")

    _, profile, producers = resolve(parse_args([]))

    assert [stage.rate for stage in profile.stages] == [10.0, 50.0]
    assert [producer.name for producer in producers] == ["alpha"]


def test_a_secret_is_taken_from_the_environment_and_never_the_command_line():
    producers = build_producers(
        ["alpha:TENANT-01:client-a"], {"LOAD_SIM_SECRET_ALPHA": "s3cret"}
    )

    assert producers[0].client_secret == "s3cret"
    assert producers[0].client_id == "client-a"


def test_a_producer_may_omit_its_client_id_for_a_static_token_run():
    (producer,) = build_producers(["alpha:TENANT-01"], {})

    assert producer.client_id == ""
    assert producer.tenant_id == "TENANT-01"


@pytest.mark.parametrize("spec", ["alpha", "alpha:", ":TENANT-01", ""])
def test_a_malformed_producer_is_refused(spec):
    with pytest.raises(ConfigurationError):
        build_producers([spec], {})


def reported(out_dir):
    recorder = Recorder()
    recorder.add(
        Sample(
            stage="stage1-10rps",
            producer="alpha",
            tenant_id="TENANT-01",
            event_id="EVT-1",
            duplicate=False,
            status=202,
            latency_ms=5.0,
            sent_at=0.0,
            lag_ms=0.0,
        )
    )
    return recorder, report(
        recorder,
        args(
            "--target-url",
            "https://x.invalid",
            "--stage",
            "10:1",
            "--producer",
            "alpha:TENANT-01",
            "--token",
            "a-token",
            "--out-dir",
            str(out_dir),
        ),
        datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def test_the_summary_is_printed_as_one_json_line_for_the_job_log(capsys, tmp_path):
    _, code = reported(tmp_path / "results")

    line = [
        printed
        for printed in capsys.readouterr().out.splitlines()
        if printed.startswith(SUMMARY_MARKER)
    ]

    assert code == 0
    assert len(line) == 1
    summary = json.loads(line[0][len(SUMMARY_MARKER) :])
    assert summary["overall"]["accepted"] == 1
    assert summary["run"]["write_ceiling_rps"]


def test_a_run_that_cannot_write_its_results_still_reports_them(capsys, tmp_path):
    # An out-dir whose parent is a file, which is what a read-only or
    # misconfigured mount looks like from here.
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")

    _, code = reported(blocked / "results")

    printed = capsys.readouterr()
    assert code == 0
    assert SUMMARY_MARKER in printed.out
    assert "could not write results" in printed.err


def test_missing_configuration_exits_two_so_a_smoke_test_can_tell_them_apart(capsys):
    assert main([]) == 2
    assert "TARGET_URL is required" in capsys.readouterr().err


def test_the_run_shape_can_be_set_from_the_environment(monkeypatch):
    # The same image runs as a job in the cloud, where there is no command
    # line to type: an option only settable by flag cannot be set at all
    # there.
    monkeypatch.setenv("DUPLICATE_RATE", "0.05")
    monkeypatch.setenv("SEED", "7")
    monkeypatch.setenv("MAX_INFLIGHT", "2000")
    monkeypatch.setenv("REQUEST_TIMEOUT", "30")

    parsed = parse_args([])

    assert parsed.duplicate_rate == 0.05
    assert parsed.seed == 7
    assert parsed.max_inflight == 2000
    assert parsed.timeout == 30.0


def test_a_flag_still_beats_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_INFLIGHT", "2000")

    assert parse_args(["--max-inflight", "50"]).max_inflight == 50


def test_a_malformed_duplicate_rate_is_a_configuration_fault_not_a_traceback(
    monkeypatch, capsys
):
    monkeypatch.setenv("DUPLICATE_RATE", "every-other-one")

    assert main([]) == 2
    assert "every-other-one" in capsys.readouterr().err


def test_a_malformed_profile_also_exits_two(capsys):
    code = main(
        [
            "--target-url",
            "https://x.invalid",
            "--stage",
            "not-a-rate",
            "--producer",
            "alpha:TENANT-01",
            "--token",
            "a-token",
        ]
    )

    assert code == 2
    assert "rate" in capsys.readouterr().err
