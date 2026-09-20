import pytest

from app.profile import Profile


def test_stages_keep_the_order_they_were_given():
    profile = Profile.from_specs(["20:60", "100:120", "140:30"])

    assert [stage.rate for stage in profile.stages] == [20.0, 100.0, 140.0]
    assert [stage.index for stage in profile.stages] == [0, 1, 2]


def test_a_stage_sends_rate_times_duration_requests():
    profile = Profile.from_specs(["20:60", "100:120"])

    assert [stage.requests for stage in profile.stages] == [1200, 12000]
    assert profile.total_requests == 13200
    assert profile.total_duration == 180


def test_a_stage_shorter_than_one_request_still_sends_one():
    (stage,) = Profile.from_specs(["0.5:1"]).stages

    assert stage.requests == 1


def test_the_label_names_the_stage_and_its_rate():
    profile = Profile.from_specs(["20:60", "100.5:10"])

    assert [stage.label for stage in profile.stages] == [
        "stage1-20rps",
        "stage2-100.5rps",
    ]


@pytest.mark.parametrize(
    "spec",
    [
        "100",  # no duration
        "100:abc",
        "abc:100",
        "0:60",  # a rate of nothing is not a stage
        "100:0",
        "-5:60",
    ],
)
def test_a_malformed_stage_is_refused(spec):
    with pytest.raises(ValueError):
        Profile.from_specs([spec])


def test_a_profile_needs_at_least_one_stage():
    with pytest.raises(ValueError):
        Profile.from_specs([])
