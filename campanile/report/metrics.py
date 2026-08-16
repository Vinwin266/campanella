"""Counters and histograms folded out of a run.

Metrics here are computed, never accumulated live: you hand :func:`collect` a
replayed run and get numbers back.  That keeps the scheduler free of metric
plumbing and means the same numbers come out of a run that finished a month ago
as out of one that just ended.

Histogram buckets are fixed rather than derived from the data, so two runs'
histograms can be compared directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..model.state import TaskState
from ..store.events import EventKind
from ..store.replay import RunSnapshot
from ..util.duration import format_duration

__all__ = ["Counter", "Histogram", "RunMetrics", "collect", "aggregate", "DURATION_BUCKETS"]

#: Upper bounds, in seconds, of the duration histogram buckets.  The last bucket
#: is open-ended.
DURATION_BUCKETS: tuple[float, ...] = (0.1, 1.0, 10.0, 60.0, 300.0, 3600.0)


@dataclass
class Counter:
    """A named tally, iterated in insertion order."""

    name: str
    values: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, amount: int = 1) -> int:
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    def get(self, key: str) -> int:
        return self.values.get(key, 0)

    @property
    def total(self) -> int:
        return sum(self.values.values())

    def top(self, count: int = 5) -> list[tuple[str, int]]:
        """The ``count`` largest entries, ties broken by key for stability."""

        return sorted(self.values.items(), key=lambda item: (-item[1], item[0]))[:count]

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.values.items()))

    def describe(self) -> str:
        if not self.values:
            return f"{self.name}: none"
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(self.values.items()))
        return f"{self.name}: {rendered}"


@dataclass
class Histogram:
    """Counts of observations falling into fixed buckets."""

    name: str
    bounds: tuple[float, ...] = DURATION_BUCKETS
    counts: list[int] = field(default_factory=list)
    observations: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.bounds) + 1)
        if len(self.counts) != len(self.bounds) + 1:
            raise ValueError("counts must have one more entry than bounds")

    def observe(self, value: float) -> None:
        self.observations.append(float(value))
        for index, bound in enumerate(self.bounds):
            if value <= bound:
                self.counts[index] += 1
                return
        self.counts[-1] += 1

    @property
    def count(self) -> int:
        return len(self.observations)

    @property
    def total(self) -> float:
        return sum(self.observations)

    @property
    def mean(self) -> float:
        return self.total / self.count if self.observations else 0.0

    @property
    def maximum(self) -> float:
        return max(self.observations, default=0.0)

    @property
    def minimum(self) -> float:
        return min(self.observations, default=0.0)

    def quantile(self, fraction: float) -> float:
        """Nearest-rank quantile; ``quantile(0.5)`` is the median.

        Nearest-rank rather than interpolated, so the answer is always a value
        that was actually observed.
        """

        if not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be between 0 and 1")
        if not self.observations:
            return 0.0
        ordered = sorted(self.observations)
        index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    def buckets(self) -> list[tuple[str, int]]:
        """Bucket labels paired with their counts."""

        labels: list[tuple[str, int]] = []
        previous = 0.0
        for index, bound in enumerate(self.bounds):
            labels.append(
                (f"{format_duration(previous)}-{format_duration(bound)}", self.counts[index])
            )
            previous = bound
        labels.append((f">{format_duration(self.bounds[-1])}", self.counts[-1]))
        return labels

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "total": self.total,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "p50": self.quantile(0.5),
            "p95": self.quantile(0.95),
            "buckets": dict(self.buckets()),
        }

    def describe(self) -> str:
        if not self.observations:
            return f"{self.name}: no observations"
        return (
            f"{self.name}: n={self.count} "
            f"mean={format_duration(self.mean)} "
            f"p50={format_duration(self.quantile(0.5))} "
            f"max={format_duration(self.maximum)}"
        )


@dataclass
class RunMetrics:
    """Everything countable about one or more runs."""

    runs: int = 0
    states: Counter = field(default_factory=lambda: Counter("run states"))
    task_states: Counter = field(default_factory=lambda: Counter("task states"))
    attempts: Counter = field(default_factory=lambda: Counter("attempts per task"))
    retries: Counter = field(default_factory=lambda: Counter("retries per task"))
    resource_waits: Counter = field(default_factory=lambda: Counter("resource waits"))
    task_durations: Histogram = field(
        default_factory=lambda: Histogram("task duration")
    )
    run_durations: Histogram = field(default_factory=lambda: Histogram("run duration"))
    #: Cumulative running time per task, worst first.  Filled in by :func:`collect`.
    slowest_tasks: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total_attempts(self) -> int:
        return self.attempts.total

    @property
    def total_retries(self) -> int:
        return self.retries.total

    @property
    def failed_runs(self) -> int:
        return self.states.get(TaskState.FAILED.value)

    def success_rate(self) -> float:
        """Fraction of runs that succeeded, zero when there are none."""

        if not self.runs:
            return 0.0
        return self.states.get("succeeded") / self.runs

    def flakiest(self, count: int = 5) -> list[tuple[str, int]]:
        """Tasks that retried most, worst first."""

        return self.retries.top(count)

    def slowest(self, count: int = 5) -> list[tuple[str, float]]:
        """The ``count`` tasks that spent the most time running."""

        return self.slowest_tasks[:count]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "success_rate": self.success_rate(),
            "run_states": self.states.as_dict(),
            "task_states": self.task_states.as_dict(),
            "attempts": self.attempts.as_dict(),
            "retries": self.retries.as_dict(),
            "resource_waits": self.resource_waits.as_dict(),
            "task_duration": self.task_durations.as_dict(),
            "run_duration": self.run_durations.as_dict(),
        }

    def describe(self) -> str:
        lines = [
            f"runs: {self.runs} (success rate {self.success_rate():.0%})",
            self.states.describe(),
            self.task_states.describe(),
            self.task_durations.describe(),
            self.run_durations.describe(),
        ]
        if self.total_retries:
            lines.append(self.retries.describe())
        if self.resource_waits.total:
            lines.append(self.resource_waits.describe())
        return "\n".join(lines)


def collect(snapshots: RunSnapshot | Iterable[RunSnapshot]) -> RunMetrics:
    """Fold one or more replayed runs into a :class:`RunMetrics`."""

    if isinstance(snapshots, RunSnapshot):
        snapshots = [snapshots]

    metrics = RunMetrics()
    durations: dict[str, float] = {}

    for snapshot in snapshots:
        metrics.runs += 1
        metrics.states.add(snapshot.state.value)
        if snapshot.duration is not None:
            metrics.run_durations.observe(snapshot.duration)

        for task in snapshot.tasks.values():
            metrics.task_states.add(task.state.value)
            if task.attempts:
                metrics.attempts.add(task.name, task.attempts)
            if task.retries:
                metrics.retries.add(task.name, task.retries)
            if task.resource_waits:
                metrics.resource_waits.add(task.name, task.resource_waits)
            if task.duration is not None and task.ran:
                metrics.task_durations.observe(task.duration)
                durations[task.name] = durations.get(task.name, 0.0) + task.duration

    metrics.slowest_tasks = sorted(durations.items(), key=lambda item: (-item[1], item[0]))
    return metrics


def aggregate(collections: Sequence[RunMetrics]) -> RunMetrics:
    """Merge several metric sets into one."""

    merged = RunMetrics()
    for source in collections:
        merged.runs += source.runs
        for counter_name in (
            "states",
            "task_states",
            "attempts",
            "retries",
            "resource_waits",
        ):
            target: Counter = getattr(merged, counter_name)
            for key, value in getattr(source, counter_name).values.items():
                target.add(key, value)
        for observation in source.task_durations.observations:
            merged.task_durations.observe(observation)
        for observation in source.run_durations.observations:
            merged.run_durations.observe(observation)

    totals: dict[str, float] = {}
    for source in collections:
        for name, value in source.slowest_tasks:
            totals[name] = totals.get(name, 0.0) + value
    merged.slowest_tasks = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return merged


def count_events(events: Iterable[Any]) -> Counter:
    """Tally events by kind.  Handy when debugging a log directly."""

    counter = Counter("events")
    for event in events:
        kind = event.kind if isinstance(event.kind, EventKind) else EventKind(event.kind)
        counter.add(kind.value)
    return counter


def state_shares(counter: Counter) -> Mapping[str, float]:
    """Turn a state counter into fractions of the total."""

    total = counter.total
    if not total:
        return {}
    return {key: value / total for key, value in sorted(counter.values.items())}
