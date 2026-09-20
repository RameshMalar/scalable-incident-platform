"""The shape of the load, as a sequence of constant-rate stages.

A single flat rate answers one question and hides the interesting one. The
platform scales on two different signals -- the ingestion service on HTTP
concurrency, the worker on queue depth -- and neither reacts instantly. Holding
a rate long enough for the autoscaler to respond, then stepping to the next, is
what makes the replica count legible against the load rather than lagging it by
an unknown amount.

The Cosmos free tier sustains roughly 101 incident writes per second (9.9 RU
each against a 1,000 RU/s grant), so a useful profile approaches that ceiling
in steps and then crosses it deliberately. Throttling that was planned for is
a result; throttling that was stumbled into is noise.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    """A constant request rate, held for a fixed wall-clock duration."""

    rate: float
    duration: float
    index: int = 0

    @property
    def label(self) -> str:
        return f"stage{self.index + 1}-{self.rate:g}rps"

    @property
    def requests(self) -> int:
        return max(1, round(self.rate * self.duration))


@dataclass(frozen=True)
class Profile:
    stages: tuple[Stage, ...]

    @property
    def total_requests(self) -> int:
        return sum(stage.requests for stage in self.stages)

    @property
    def total_duration(self) -> float:
        return sum(stage.duration for stage in self.stages)

    @classmethod
    def from_specs(cls, specs: list[str]) -> "Profile":
        """Read `rate:seconds` strings into stages, in the order given."""
        if not specs:
            raise ValueError("a load profile needs at least one stage")

        stages = []
        for index, spec in enumerate(specs):
            rate, separator, duration = spec.partition(":")
            if not separator:
                raise ValueError(
                    f"stage {spec!r} is not in rate:seconds form, e.g. 100:120"
                )
            try:
                parsed_rate = float(rate)
                parsed_duration = float(duration)
            except ValueError:
                raise ValueError(
                    f"stage {spec!r} has a non-numeric rate or duration"
                ) from None
            if parsed_rate <= 0 or parsed_duration <= 0:
                raise ValueError(
                    f"stage {spec!r} must have a positive rate and duration"
                )
            stages.append(
                Stage(rate=parsed_rate, duration=parsed_duration, index=index)
            )

        return cls(stages=tuple(stages))
