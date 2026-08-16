"""Deciding whether and when to try again.

:class:`~campanile.model.policy.RetryPolicy` says *what* the rules are.  This module
applies them to a concrete failure and produces a :class:`RetryDecision`: retry
or not, and if so at what moment on the run's clock.

The jitter key is the task name, so two tasks retrying under the same policy get
different -- but still reproducible -- delays, and one flaky dependency does not
cause every task that touches it to hammer it in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import TaskTimeout
from ..model.policy import RetryPolicy
from ..util.duration import format_duration

__all__ = ["RetryDecision", "decide_retry", "attempt_schedule"]


@dataclass(frozen=True)
class RetryDecision:
    """Whether to retry, when, and why."""

    retry: bool
    delay: float = 0.0
    at: float | None = None
    reason: str = ""

    def describe(self) -> str:
        if not self.retry:
            return f"no retry ({self.reason})"
        return f"retry in {format_duration(self.delay)} ({self.reason})"

    def as_dict(self) -> dict[str, object]:
        return {
            "retry": self.retry,
            "delay": self.delay,
            "at": self.at,
            "reason": self.reason,
        }


def decide_retry(
    policy: RetryPolicy,
    *,
    task: str,
    attempt: int,
    now: float,
    error: BaseException | None = None,
    timed_out: bool = False,
    cancelled: bool = False,
) -> RetryDecision:
    """Apply ``policy`` to one failed attempt.

    Cancellation always wins: a task whose context asked to stop is never
    retried, whatever the policy says.  A timeout is retried like any other
    failure -- the policy's ``retry_on`` allow-list, if present, is checked
    against :class:`~campanile.errors.TaskTimeout`.
    """

    if attempt < 1:
        raise ValueError("attempt numbers start at 1")

    if cancelled:
        return RetryDecision(False, reason="attempt was cancelled")

    if attempt >= policy.max_attempts:
        return RetryDecision(
            False,
            reason=f"attempt {attempt} of {policy.max_attempts} was the last",
        )

    effective_error = error
    if timed_out and effective_error is None:
        effective_error = TaskTimeout(task, 0.0, attempt=attempt)

    if not policy.should_retry(attempt, effective_error):
        kind = type(effective_error).__name__ if effective_error else "failure"
        allowed = ", ".join(policy.retry_on) or "any"
        return RetryDecision(
            False, reason=f"{kind} is not retried (policy retries: {allowed})"
        )

    delay = policy.delay_for(attempt, key=task)
    return RetryDecision(
        True,
        delay=delay,
        at=now + delay,
        reason=f"attempt {attempt} of {policy.max_attempts}",
    )


def attempt_schedule(policy: RetryPolicy, *, task: str, start: float) -> list[float]:
    """Every moment this policy would start an attempt, given no successes.

    >>> from campanile.model.policy import RetryPolicy
    >>> attempt_schedule(RetryPolicy(max_attempts=3, delay=1.0), task="t", start=0.0)
    [0.0, 1.0, 3.0]

    Used by reports and by ``campanile explain`` to show what a policy will do
    before anything has run.
    """

    moments = [float(start)]
    current = float(start)
    for attempt in range(1, policy.max_attempts):
        current += policy.delay_for(attempt, key=task)
        moments.append(current)
    return moments
