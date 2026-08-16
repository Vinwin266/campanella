"""What a run hands back.

:class:`RunResult` is the value the engine returns.  It carries the outcome of
every task, the parameters the run was started with, and the event log that
produced it -- so a caller can assert on the summary, dig into one task, or
replay the whole thing, without reaching into the scheduler.

Results are read-only snapshots.  Mutating one cannot change what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ..errors import CampanileError, ExecutionError, UnknownTaskError
from ..model.state import RunState, TaskState, summarise_states
from ..store.log import EventLog
from ..store.replay import RunSnapshot, replay
from ..util.duration import format_duration
from ..util.ids import format_iso

__all__ = ["TaskResult", "RunResult"]


@dataclass(frozen=True)
class TaskResult:
    """The outcome of one task within one run."""

    name: str
    state: TaskState
    attempts: int = 0
    value: Any = None
    error: BaseException | None = None
    started_at: float | None = None
    finished_at: float | None = None
    retries: int = 0
    timed_out: bool = False
    reason: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    @property
    def succeeded(self) -> bool:
        return self.state is TaskState.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.state is TaskState.FAILED

    @property
    def skipped(self) -> bool:
        return self.state in (TaskState.SKIPPED, TaskState.UPSTREAM_FAILED)

    @property
    def ran(self) -> bool:
        return self.attempts > 0

    @property
    def error_message(self) -> str | None:
        if self.error is None:
            return None
        return f"{type(self.error).__name__}: {self.error}"

    def describe(self) -> str:
        bits = [f"{self.name}", str(self.state)]
        if self.attempts > 1:
            bits.append(f"{self.attempts} attempts")
        if self.duration is not None:
            bits.append(format_duration(self.duration))
        if self.timed_out:
            bits.append("timed out")
        if self.error is not None:
            bits.append(self.error_message or "")
        elif self.reason:
            bits.append(self.reason)
        return " | ".join(bit for bit in bits if bit)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "attempts": self.attempts,
            "retries": self.retries,
            "value": self.value,
            "error": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RunResult:
    """Everything a finished (or abandoned) run produced."""

    run_id: str
    workflow: str
    state: RunState
    params: Mapping[str, Any] = field(default_factory=dict)
    tasks: Mapping[str, TaskResult] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    log: EventLog | None = None

    # -- outcome -------------------------------------------------------------

    @property
    def succeeded(self) -> bool:
        return self.state is RunState.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.state is RunState.FAILED

    @property
    def cancelled(self) -> bool:
        return self.state is RunState.CANCELLED

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    # -- task access ---------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self.tasks

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self) -> Iterator[TaskResult]:
        return iter(self.tasks.values())

    def __getitem__(self, name: str) -> TaskResult:
        return self.task(name)

    def task(self, name: str) -> TaskResult:
        try:
            return self.tasks[name]
        except KeyError:
            raise UnknownTaskError(name, workflow=self.workflow, known=self.tasks) from None

    def value(self, name: str) -> Any:
        """The value a task returned, raising if it did not succeed."""

        result = self.task(name)
        if not result.succeeded:
            raise ExecutionError(
                f"task {name!r} did not succeed (state {result.state})",
                task=name,
                state=result.state.value,
            )
        return result.value

    def values(self) -> dict[str, Any]:
        """Every successful task's value, in declaration order."""

        return {
            name: result.value
            for name, result in self.tasks.items()
            if result.succeeded
        }

    def states(self) -> dict[str, TaskState]:
        return {name: result.state for name, result in self.tasks.items()}

    def counts(self) -> dict[str, int]:
        return summarise_states(result.state for result in self.tasks.values())

    def failures(self) -> tuple[TaskResult, ...]:
        return tuple(result for result in self.tasks.values() if result.failed)

    def in_state(self, state: TaskState) -> tuple[TaskResult, ...]:
        return tuple(result for result in self.tasks.values() if result.state is state)

    def total_attempts(self) -> int:
        return sum(result.attempts for result in self.tasks.values())

    def first_error(self) -> BaseException | None:
        """The error of the earliest-declared failed task, if any."""

        for result in self.tasks.values():
            if result.error is not None:
                return result.error
        return None

    # -- integration ---------------------------------------------------------

    def raise_for_status(self) -> "RunResult":
        """Raise if the run did not succeed, otherwise return ``self``.

        The raised error is the first task error when there was one, so a caller
        that wraps ``engine.run(...).raise_for_status()`` sees the real cause
        rather than a generic wrapper.
        """

        if self.succeeded:
            return self
        error = self.first_error()
        if isinstance(error, CampanileError):
            raise error
        if error is not None:
            raise ExecutionError(
                f"run {self.run_id} failed: {type(error).__name__}: {error}",
                run_id=self.run_id,
            ) from error
        raise ExecutionError(
            f"run {self.run_id} ended {self.state}", run_id=self.run_id
        )

    def snapshot(self) -> RunSnapshot:
        """Replay this run's log; useful for asserting the two agree."""

        if self.log is None or not len(self.log):
            raise ExecutionError(
                f"run {self.run_id} has no event log to replay", run_id=self.run_id
            )
        return replay(self.log)

    def events(self) -> tuple[Any, ...]:
        return self.log.events if self.log is not None else ()

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        head = f"run {self.run_id} ({self.workflow}) {self.state}"
        if self.started_at is not None:
            head += f" started {format_iso(self.started_at)}"
        if self.duration is not None:
            head += f" took {format_duration(self.duration)}"
        lines = [head]
        lines.extend(f"  {result.describe()}" for result in self.tasks.values())
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "state": self.state.value,
            "params": dict(self.params),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "tasks": [result.as_dict() for result in self.tasks.values()],
        }

    def __repr__(self) -> str:
        return f"RunResult({self.run_id!r}, {self.state}, tasks={len(self.tasks)})"
