"""Clock abstractions.

Nothing below the CLI layer is allowed to call :func:`time.time` directly.  Every
component that needs to know what time it is takes a :class:`Clock`, which makes
the whole engine reproducible: hand it a :class:`ManualClock` and a run produces
byte-identical events every time.

A clock exposes two readings:

``now()``
    Wall-clock seconds since the Unix epoch.  Used for anything a human will
    read back -- event timestamps, schedule computations, report headers.

``monotonic()``
    A non-decreasing reading used for measuring elapsed time.  On the system
    clock this is independent of wall-clock adjustments; on a manual clock the
    two advance together.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator, Protocol, runtime_checkable

__all__ = [
    "Clock",
    "SystemClock",
    "ManualClock",
    "OffsetClock",
    "Deadline",
    "EPOCH_2026",
]

#: 2026-01-01T00:00:00Z.  Used as the default origin for manual clocks so that
#: rendered timestamps in tests look like plausible dates rather than 1970.
EPOCH_2026 = 1767225600.0


@runtime_checkable
class Clock(Protocol):
    """The reading surface every component depends on."""

    def now(self) -> float:
        """Seconds since the Unix epoch."""

    def monotonic(self) -> float:
        """A non-decreasing reading, in seconds, with an arbitrary origin."""


class SystemClock:
    """The real clock.  The only place in the package that reads the host time."""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "SystemClock()"


class ManualClock:
    """A clock that only moves when told to.

    The engine's scheduler never sleeps; when it needs to wait for a retry
    backoff or the next trigger fire it advances the clock instead.  With a
    manual clock a run therefore completes instantly while still producing the
    timestamps it would have produced in real time.
    """

    __slots__ = ("_now", "_origin", "_advances")

    def __init__(self, start: float = EPOCH_2026) -> None:
        self._now = float(start)
        self._origin = float(start)
        self._advances = 0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now - self._origin

    @property
    def advances(self) -> int:
        """How many times the clock has been moved forward."""

        return self._advances

    def advance(self, seconds: float) -> float:
        """Move forward by ``seconds`` and return the new reading."""

        if seconds < 0:
            raise ValueError("a clock cannot advance by a negative amount")
        if seconds:
            self._now += float(seconds)
            self._advances += 1
        return self._now

    def advance_to(self, timestamp: float) -> float:
        """Move forward to ``timestamp``; moving backwards is an error."""

        target = float(timestamp)
        if target < self._now:
            raise ValueError(
                f"cannot move a clock backwards from {self._now} to {target}"
            )
        if target > self._now:
            self._now = target
            self._advances += 1
        return self._now

    def reset(self, start: float | None = None) -> None:
        """Return the clock to its origin, or to ``start`` if given."""

        origin = self._origin if start is None else float(start)
        self._origin = origin
        self._now = origin
        self._advances = 0

    def ticking(self, step: float) -> Callable[[], float]:
        """Return a callable that advances by ``step`` each time it is called.

        Useful for simulating a task whose every observation of the clock costs
        a fixed amount of virtual time.
        """

        def tick() -> float:
            return self.advance(step)

        return tick

    def __repr__(self) -> str:
        return f"ManualClock(now={self._now!r})"


class OffsetClock:
    """A clock that reads another clock and shifts it by a fixed offset.

    Used to model workers whose wall clocks disagree, without giving up
    determinism: the offset is a constant, not a random skew.
    """

    __slots__ = ("_inner", "_offset")

    def __init__(self, inner: Clock, offset: float) -> None:
        self._inner = inner
        self._offset = float(offset)

    @property
    def offset(self) -> float:
        return self._offset

    def now(self) -> float:
        return self._inner.now() + self._offset

    def monotonic(self) -> float:
        return self._inner.monotonic()

    def __repr__(self) -> str:
        return f"OffsetClock({self._inner!r}, offset={self._offset!r})"


class Deadline:
    """A point in the future measured against a clock.

    >>> clock = ManualClock(0.0)
    >>> deadline = Deadline(clock, 10.0)
    >>> deadline.expired()
    False
    >>> clock.advance(10.0)
    10.0
    >>> deadline.expired()
    True

    A deadline built from ``None`` never expires, which lets callers treat "no
    timeout" and "some timeout" uniformly.
    """

    __slots__ = ("_clock", "_at", "_budget")

    def __init__(self, clock: Clock, budget: float | None) -> None:
        self._clock = clock
        self._budget = budget
        self._at = None if budget is None else clock.monotonic() + float(budget)

    @property
    def budget(self) -> float | None:
        return self._budget

    @property
    def infinite(self) -> bool:
        return self._at is None

    def remaining(self) -> float | None:
        """Seconds left, or ``None`` if the deadline is infinite."""

        if self._at is None:
            return None
        return self._at - self._clock.monotonic()

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0.0

    def elapsed(self) -> float:
        if self._at is None or self._budget is None:
            return 0.0
        return self._budget - (self._at - self._clock.monotonic())

    def __repr__(self) -> str:
        return f"Deadline(budget={self._budget!r}, remaining={self.remaining()!r})"


def elapsed_since(clock: Clock, mark: float) -> float:
    """Monotonic seconds between ``mark`` and now, never negative."""

    return max(0.0, clock.monotonic() - mark)


def iter_ticks(clock: ManualClock, step: float, count: int) -> Iterator[float]:
    """Yield ``count`` successive readings, advancing by ``step`` between each."""

    if count < 0:
        raise ValueError("count must not be negative")
    for index in range(count):
        if index:
            clock.advance(step)
        yield clock.now()
