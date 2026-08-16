"""Retry, failure and trigger policies.

These three knobs decide almost everything about how a run behaves when things
go wrong:

:class:`RetryPolicy`
    How many times to try a task, and how long to wait between tries.

:class:`FailurePolicy`
    What a *run* does once a task has failed for the last time.

:class:`TriggerRule`
    What a task requires of its upstreams before it is allowed to start.

All three are plain values with no behaviour that touches the outside world, so
the scheduler's decisions can be unit-tested without running anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ConfigurationError
from ..util.duration import coerce_duration, format_duration
from .state import TaskState

__all__ = [
    "Backoff",
    "RetryPolicy",
    "NO_RETRY",
    "FailurePolicy",
    "TriggerRule",
    "TriggerDecision",
    "evaluate_trigger_rule",
]


class Backoff(Enum):
    """How the delay between attempts grows."""

    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry a task, and how long to wait in between.

    ``max_attempts`` counts the first try, so ``max_attempts=1`` means "no
    retries" and ``max_attempts=3`` means "up to two retries".

    Delay for the wait *after* attempt ``n`` (1-based):

    ==============  ==========================================
    ``FIXED``       ``delay``
    ``LINEAR``      ``delay * n``
    ``EXPONENTIAL`` ``delay * multiplier ** (n - 1)``
    ==============  ==========================================

    and the result is then clamped to ``max_delay`` and, if ``jitter`` is
    non-zero, spread deterministically over ``[d * (1 - jitter), d]``.  The
    spread comes from a hash of the task name and attempt number rather than a
    random source, so a retry schedule is reproducible.
    """

    max_attempts: int = 1
    backoff: Backoff = Backoff.EXPONENTIAL
    delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float | None = 300.0
    jitter: float = 0.0
    retry_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigurationError("max_attempts must be at least 1")
        if self.delay < 0:
            raise ConfigurationError("retry delay must not be negative")
        if self.multiplier <= 0:
            raise ConfigurationError("retry multiplier must be positive")
        if self.max_delay is not None and self.max_delay < 0:
            raise ConfigurationError("max_delay must not be negative")
        if not 0.0 <= self.jitter <= 1.0:
            raise ConfigurationError("jitter must be between 0 and 1")

    # -- construction --------------------------------------------------------

    @classmethod
    def of(
        cls,
        max_attempts: int = 1,
        *,
        backoff: Backoff | str = Backoff.EXPONENTIAL,
        delay: float | str = 1.0,
        multiplier: float = 2.0,
        max_delay: float | str | None = 300.0,
        jitter: float = 0.0,
        retry_on: Iterable[str] = (),
    ) -> "RetryPolicy":
        """Build a policy, accepting durations as strings and backoff by name."""

        kind = backoff if isinstance(backoff, Backoff) else Backoff(str(backoff))
        return cls(
            max_attempts=int(max_attempts),
            backoff=kind,
            delay=float(coerce_duration(delay, 0.0) or 0.0),
            multiplier=float(multiplier),
            max_delay=coerce_duration(max_delay, None),
            jitter=float(jitter),
            retry_on=tuple(retry_on),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetryPolicy":
        known = {
            "max_attempts",
            "backoff",
            "delay",
            "multiplier",
            "max_delay",
            "jitter",
            "retry_on",
        }
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ConfigurationError(
                f"unknown retry policy field(s): {', '.join(unknown)}"
            )
        return cls.of(**payload)  # type: ignore[arg-type]

    def with_attempts(self, max_attempts: int) -> "RetryPolicy":
        return replace(self, max_attempts=int(max_attempts))

    # -- behaviour -----------------------------------------------------------

    @property
    def retries(self) -> int:
        """Number of retries, i.e. attempts after the first."""

        return self.max_attempts - 1

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 1

    def should_retry(self, attempt: int, error: BaseException | None = None) -> bool:
        """Whether attempt ``attempt`` (1-based) may be followed by another.

        When ``retry_on`` is non-empty it acts as an allow-list of exception
        class names; anything outside it fails immediately even if attempts
        remain.  The class's own name and every base class name are considered,
        so listing a base class covers its subclasses.
        """

        if attempt < 1:
            raise ValueError("attempt numbers start at 1")
        if attempt >= self.max_attempts:
            return False
        if not self.retry_on or error is None:
            return True
        names = {klass.__name__ for klass in type(error).__mro__}
        return bool(names & set(self.retry_on))

    def base_delay_for(self, attempt: int) -> float:
        """The delay after attempt ``attempt``, before jitter and clamping."""

        if attempt < 1:
            raise ValueError("attempt numbers start at 1")
        if self.backoff is Backoff.FIXED:
            return self.delay
        if self.backoff is Backoff.LINEAR:
            return self.delay * attempt
        return self.delay * (self.multiplier ** (attempt - 1))

    def delay_for(self, attempt: int, *, key: str = "") -> float:
        """Seconds to wait after attempt ``attempt`` before the next one.

        >>> policy = RetryPolicy(max_attempts=4, delay=1.0, multiplier=2.0)
        >>> [policy.delay_for(n) for n in (1, 2, 3)]
        [1.0, 2.0, 4.0]
        """

        delay = self.base_delay_for(attempt)
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        if self.jitter:
            delay *= 1.0 - self.jitter * _jitter_fraction(key, attempt)
        return max(0.0, delay)

    def schedule(self, *, key: str = "") -> list[float]:
        """Every delay this policy will ever produce, in order."""

        return [self.delay_for(attempt, key=key) for attempt in range(1, self.max_attempts)]

    def describe(self) -> str:
        if not self.enabled:
            return "no retries"
        delays = ", ".join(format_duration(value) for value in self.schedule())
        return f"up to {self.max_attempts} attempts, {self.backoff} backoff ({delays})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff": self.backoff.value,
            "delay": self.delay,
            "multiplier": self.multiplier,
            "max_delay": self.max_delay,
            "jitter": self.jitter,
            "retry_on": list(self.retry_on),
        }


#: The default policy: try once, never retry.
NO_RETRY = RetryPolicy(max_attempts=1)


def _jitter_fraction(key: str, attempt: int) -> float:
    """A stable pseudo-random number in ``[0, 1)`` derived from key and attempt."""

    digest = hashlib.sha256(f"{key}#{attempt}".encode("utf-8")).digest()
    raw = int.from_bytes(digest[:4], "big")
    return raw / 2**32


class FailurePolicy(Enum):
    """What a run does once a task has failed for the last time.

    ``CONTINUE``
        Only the failed task's descendants are affected; unrelated branches keep
        running.  The run still ends ``FAILED``.

    ``FAIL_FAST``
        Everything not already running is cancelled immediately.  Tasks already
        in flight are allowed to finish, because there is no safe way to
        interrupt an arbitrary callable.

    ``ISOLATE``
        Descendants are marked ``UPSTREAM_FAILED`` as with ``CONTINUE``, but the
        run itself is reported as succeeded if every *other* branch succeeded.
        For workflows where one optional branch is allowed to break.
    """

    CONTINUE = "continue"
    FAIL_FAST = "fail_fast"
    ISOLATE = "isolate"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TriggerRule(Enum):
    """What a task requires of its upstreams before it may run."""

    ALL_SUCCESS = "all_success"
    ALL_DONE = "all_done"
    ALL_FAILED = "all_failed"
    ANY_SUCCESS = "any_success"
    ANY_FAILED = "any_failed"
    NONE_FAILED = "none_failed"
    ALWAYS = "always"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TriggerDecision(Enum):
    """The three answers :func:`evaluate_trigger_rule` can give."""

    RUN = "run"
    SKIP = "skip"
    WAIT = "wait"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def evaluate_trigger_rule(
    rule: TriggerRule, upstream_states: Sequence[TaskState]
) -> TriggerDecision:
    """Decide whether a task may start, must wait, or will never start.

    ``WAIT`` means "not yet, ask again"; ``SKIP`` means the rule can no longer be
    satisfied no matter what happens next, so the task is finished before it
    began.  A task with no upstreams always runs.

    For the purposes of these rules ``SKIPPED``, ``UPSTREAM_FAILED`` and
    ``CANCELLED`` upstreams count as *not succeeded* and *not failed*: they never
    ran, so they cannot satisfy ``ANY_SUCCESS`` and they do not trip
    ``NONE_FAILED``.  ``ALL_DONE`` and ``ALWAYS`` accept them.
    """

    states = list(upstream_states)
    if not states:
        return TriggerDecision.RUN

    if rule is TriggerRule.ALWAYS:
        return TriggerDecision.RUN

    done = all(state.is_terminal for state in states)
    succeeded = sum(1 for state in states if state.is_success)
    failed = sum(1 for state in states if state.is_failure)
    terminal_count = sum(1 for state in states if state.is_terminal)
    remaining = len(states) - terminal_count

    if rule is TriggerRule.ALL_DONE:
        return TriggerDecision.RUN if done else TriggerDecision.WAIT

    if rule is TriggerRule.ALL_SUCCESS:
        if failed or _blocked(states):
            return TriggerDecision.SKIP
        return TriggerDecision.RUN if succeeded == len(states) else TriggerDecision.WAIT

    if rule is TriggerRule.NONE_FAILED:
        if failed:
            return TriggerDecision.SKIP
        return TriggerDecision.RUN if done else TriggerDecision.WAIT

    if rule is TriggerRule.ANY_SUCCESS:
        if succeeded:
            return TriggerDecision.RUN
        return TriggerDecision.WAIT if remaining else TriggerDecision.SKIP

    if rule is TriggerRule.ANY_FAILED:
        if failed:
            return TriggerDecision.RUN
        return TriggerDecision.WAIT if remaining else TriggerDecision.SKIP

    if rule is TriggerRule.ALL_FAILED:
        if succeeded or _blocked(states):
            return TriggerDecision.SKIP
        return TriggerDecision.RUN if failed == len(states) else TriggerDecision.WAIT

    raise AssertionError(f"unhandled trigger rule {rule!r}")  # pragma: no cover


def _blocked(states: Sequence[TaskState]) -> bool:
    """True if some upstream reached a terminal state without running."""

    return any(
        state
        in (TaskState.SKIPPED, TaskState.UPSTREAM_FAILED, TaskState.CANCELLED)
        for state in states
    )
