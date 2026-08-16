"""Interval and one-shot triggers.

A cron expression says "at these wall-clock moments".  An interval says "every
so often from this anchor", which is the right shape for a job that should run
every ninety seconds regardless of where the minute boundaries fall.

Both trigger kinds expose the same two methods -- :meth:`next_after` and
:meth:`describe` -- which is all :mod:`campanile.schedule.timetable` needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable

from ..errors import ScheduleError
from ..util.duration import coerce_duration, format_duration
from ..util.ids import format_iso
from .cron import CronExpression, parse_cron

__all__ = [
    "Trigger",
    "IntervalTrigger",
    "OnceTrigger",
    "CronTrigger",
    "NeverTrigger",
    "make_trigger",
]

#: Guards against floating point drift when an interval lands exactly on the
#: query time.  Anything within this many seconds counts as "the same moment".
_EPSILON = 1e-9


@runtime_checkable
class Trigger(Protocol):
    """Anything that can say when it next fires."""

    def next_after(self, timestamp: float) -> float | None:
        """First fire strictly after ``timestamp``, or ``None`` if never again."""

    def describe(self) -> str:
        """One-line human description."""


@dataclass(frozen=True)
class IntervalTrigger:
    """Fires every ``every`` seconds, counted from ``anchor``.

    >>> trigger = IntervalTrigger(every=60.0, anchor=0.0)
    >>> trigger.next_after(0.0)
    60.0
    >>> trigger.next_after(59.0)
    60.0
    >>> trigger.next_after(60.0)
    120.0

    The anchor matters: two workflows on a five-minute interval with different
    anchors stay offset from each other for ever, which is how you keep them
    from contending for the same pool every time.
    """

    every: float
    anchor: float = 0.0
    limit: int | None = None

    def __post_init__(self) -> None:
        every = coerce_duration(self.every, None)
        if every is None or every <= 0:
            raise ScheduleError(f"interval must be positive, got {self.every!r}")
        object.__setattr__(self, "every", float(every))
        object.__setattr__(self, "anchor", float(self.anchor))
        if self.limit is not None and self.limit < 1:
            raise ScheduleError("limit must be at least 1 when given")

    def next_after(self, timestamp: float) -> float | None:
        moment = float(timestamp)
        if moment < self.anchor - _EPSILON:
            return self.anchor if self._within_limit(0) else None
        elapsed = moment - self.anchor
        index = math.floor(elapsed / self.every + _EPSILON) + 1
        if not self._within_limit(index):
            return None
        return self.anchor + index * self.every

    def _within_limit(self, index: int) -> bool:
        return self.limit is None or index < self.limit

    def occurrence_index(self, timestamp: float) -> int | None:
        """Which occurrence ``timestamp`` is, or ``None`` if it is not one."""

        offset = (float(timestamp) - self.anchor) / self.every
        rounded = round(offset)
        if abs(offset - rounded) > 1e-6 or rounded < 0:
            return None
        if self.limit is not None and rounded >= self.limit:
            return None
        return int(rounded)

    def describe(self) -> str:
        text = f"every {format_duration(self.every)}"
        if self.anchor:
            text += f" from {format_iso(self.anchor)}"
        if self.limit is not None:
            text += f" (at most {self.limit} times)"
        return text

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "interval",
            "every": self.every,
            "anchor": self.anchor,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class OnceTrigger:
    """Fires exactly once, at ``at``."""

    at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", float(self.at))

    def next_after(self, timestamp: float) -> float | None:
        return self.at if float(timestamp) < self.at - _EPSILON else None

    def describe(self) -> str:
        return f"once at {format_iso(self.at)}"

    def as_dict(self) -> dict[str, object]:
        return {"kind": "once", "at": self.at}


@dataclass(frozen=True)
class CronTrigger:
    """Adapts a :class:`CronExpression` to the trigger protocol."""

    expression: CronExpression

    @classmethod
    def parse(cls, text: str) -> "CronTrigger":
        return cls(parse_cron(text))

    def next_after(self, timestamp: float) -> float | None:
        return self.expression.next_after(timestamp)

    def previous_before(self, timestamp: float) -> float | None:
        return self.expression.previous_before(timestamp)

    def describe(self) -> str:
        return f"cron {self.expression.source}"

    def as_dict(self) -> dict[str, object]:
        return {"kind": "cron", "expression": self.expression.source}


@dataclass(frozen=True)
class NeverTrigger:
    """Never fires.  The explicit way to say "manual runs only"."""

    def next_after(self, timestamp: float) -> float | None:
        return None

    def describe(self) -> str:
        return "manual only"

    def as_dict(self) -> dict[str, object]:
        return {"kind": "never"}


def make_trigger(spec: object) -> Trigger:
    """Build a trigger from the loose forms a definition may use.

    ============================  ==================================
    ``"*/5 * * * *"``             cron
    ``"@daily"``                  cron macro
    ``"every 30s"`` / ``"30s"``   interval
    ``"never"`` / ``None``        never
    a number                      interval of that many seconds
    a trigger                     itself
    ============================  ==================================
    """

    if spec is None:
        return NeverTrigger()
    if isinstance(spec, (IntervalTrigger, OnceTrigger, CronTrigger, NeverTrigger)):
        return spec
    if isinstance(spec, CronExpression):
        return CronTrigger(spec)
    if isinstance(spec, bool):
        raise ScheduleError("cannot build a trigger from a boolean")
    if isinstance(spec, (int, float)):
        return IntervalTrigger(every=float(spec))
    if isinstance(spec, str):
        text = spec.strip()
        lowered = text.lower()
        if lowered in ("never", "manual", "none", ""):
            return NeverTrigger()
        if lowered.startswith("@") or len(text.split()) == 5:
            return CronTrigger.parse(text)
        if lowered.startswith("every "):
            text = text[len("every ") :].strip()
        try:
            seconds = coerce_duration(text, None)
        except ValueError as exc:
            raise ScheduleError(f"cannot read a trigger from {spec!r}: {exc}") from exc
        if seconds is None:
            raise ScheduleError(f"cannot read a trigger from {spec!r}")
        return IntervalTrigger(every=seconds)
    raise ScheduleError(f"cannot build a trigger from {type(spec).__name__}")


def iter_fires(trigger: Trigger, start: float, count: int) -> Iterator[float]:
    """Yield up to ``count`` fires of ``trigger`` after ``start``."""

    if count < 0:
        raise ValueError("count must not be negative")
    current = float(start)
    for _ in range(count):
        following = trigger.next_after(current)
        if following is None:
            return
        yield following
        current = following
