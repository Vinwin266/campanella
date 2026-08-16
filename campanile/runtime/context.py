"""What a task callable is handed.

A task body takes a :class:`TaskContext`, or nothing at all -- the executor
inspects the signature and calls it the right way, so trivial tasks stay
trivial::

    def ping():
        return "pong"

    def load(ctx):
        rows = ctx.result("extract")["rows"]
        ctx.note(f"loading {rows} rows")
        return rows

The context is the *only* channel between a task and the engine.  It exposes
the run's parameters, the results of upstream tasks, the clock, the deadline and
a cancellation flag; it deliberately does not expose the scheduler, the
workflow, or any way to mutate another task's state.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Sequence

from ..errors import ExecutionError, UnknownTaskError
from ..model.task import TaskSpec
from ..util.clock import Clock, Deadline
from ..util.duration import format_duration

__all__ = ["TaskContext"]

#: Sentinel distinguishing "no default given" from an explicit ``None``.
_MISSING = object()


class TaskContext:
    """The handle a running task has on its run."""

    __slots__ = (
        "run_id",
        "workflow",
        "spec",
        "attempt",
        "params",
        "clock",
        "deadline",
        "_results",
        "_upstreams",
        "_downstreams",
        "_notes",
        "_cancelled",
        "_started_at",
    )

    def __init__(
        self,
        *,
        run_id: str,
        workflow: str,
        spec: TaskSpec,
        attempt: int,
        params: Mapping[str, Any],
        results: Mapping[str, Any],
        upstreams: Sequence[str] = (),
        downstreams: Sequence[str] = (),
        clock: Clock,
        deadline: Deadline | None = None,
    ) -> None:
        self.run_id = run_id
        self.workflow = workflow
        self.spec = spec
        self.attempt = attempt
        self.params = dict(params)
        self.clock = clock
        self.deadline = deadline or Deadline(clock, None)
        self._results = dict(results)
        self._upstreams = tuple(upstreams)
        self._downstreams = tuple(downstreams)
        self._notes: list[str] = []
        self._cancelled = False
        self._started_at = clock.monotonic()

    # -- identity ------------------------------------------------------------

    @property
    def task(self) -> str:
        """The name of the task being run."""

        return self.spec.name

    @property
    def max_attempts(self) -> int:
        return self.spec.retry.max_attempts

    @property
    def is_retry(self) -> bool:
        return self.attempt > 1

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts

    @property
    def task_params(self) -> Mapping[str, Any]:
        """Static parameters declared on the task itself."""

        return dict(self.spec.params)

    @property
    def tags(self) -> tuple[str, ...]:
        return self.spec.tags

    # -- upstream results ----------------------------------------------------

    @property
    def upstreams(self) -> tuple[str, ...]:
        return self._upstreams

    @property
    def downstreams(self) -> tuple[str, ...]:
        return self._downstreams

    @property
    def results(self) -> Mapping[str, Any]:
        """Every result available so far, as a read-only mapping."""

        return dict(self._results)

    def result(self, name: str, default: Any = _MISSING) -> Any:
        """The value an upstream task returned.

        With no default, asking for a task that has not succeeded is an error --
        reading a missing result silently as ``None`` is how a pipeline ends up
        writing empty tables.
        """

        if name in self._results:
            return self._results[name]
        if default is not _MISSING:
            return default
        raise UnknownTaskError(name, workflow=self.workflow, known=self._results)

    def has_result(self, name: str) -> bool:
        return name in self._results

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    # -- notes ---------------------------------------------------------------

    def note(self, message: str, **fields: Any) -> str:
        """Attach a line to this attempt, recorded on the success event.

        Notes are for humans reading a run afterwards.  They are not logging --
        there is no level, no handler and no output stream -- which is what keeps
        a task's behaviour independent of how the process was configured.
        """

        rendered = message
        if fields:
            rendered += " " + " ".join(
                f"{key}={value!r}" for key, value in sorted(fields.items())
            )
        self._notes.append(rendered)
        return rendered

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(self._notes)

    # -- time and cancellation ----------------------------------------------

    def elapsed(self) -> float:
        """Seconds this attempt has been running, by the run's clock."""

        return max(0.0, self.clock.monotonic() - self._started_at)

    def remaining(self) -> float | None:
        """Seconds left before the timeout, or ``None`` if there is none."""

        return self.deadline.remaining()

    def expired(self) -> bool:
        return self.deadline.expired()

    def cancel(self, reason: str = "cancelled by task") -> None:
        """Mark this attempt cancelled; the scheduler stops the run after it."""

        self._cancelled = True
        self.note(f"cancellation requested: {reason}")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        """Raise if the attempt is out of time.

        Long-running tasks are expected to call this between units of work; the
        engine cannot interrupt an arbitrary callable, so a task that never
        checks runs to completion and is only *then* reported as timed out.
        """

        if self.deadline.expired():
            raise ExecutionError(
                f"task {self.task!r} exceeded its deadline of "
                f"{format_duration(self.deadline.budget or 0.0)}",
                task=self.task,
            )

    # -- rendering -----------------------------------------------------------

    def __iter__(self) -> Iterator[str]:
        return iter(self._results)

    def describe(self) -> str:
        return (
            f"{self.workflow}::{self.task} attempt {self.attempt}/{self.max_attempts} "
            f"in run {self.run_id}"
        )

    def __repr__(self) -> str:
        return f"TaskContext({self.task!r}, attempt={self.attempt})"
