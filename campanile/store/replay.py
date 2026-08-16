"""Rebuilding run state from events.

The scheduler keeps state in memory while a run is in flight, but that state is
derived: everything it knows was written to the log first.  This module proves
it by reconstructing the same picture from the log alone, which is what makes
``campanile show <run>`` work on a run this process never executed.

Replay is strict about ordering -- a ``task_succeeded`` for a task that never
started, or a second terminal event for the same task, means the log is corrupt
and says so rather than producing a plausible-looking lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..errors import EventLogCorrupt
from ..model.state import RunState, TaskState, derive_run_state, summarise_states
from ..util.ids import format_iso
from .events import Event, EventKind

__all__ = ["TaskSnapshot", "RunSnapshot", "replay", "replay_log"]


@dataclass
class TaskSnapshot:
    """What the log says about one task."""

    name: str
    state: TaskState = TaskState.PENDING
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: Mapping[str, Any] | None = None
    retries: int = 0
    timed_out: bool = False
    skip_reason: str | None = None
    resource_waits: int = 0
    running_seconds: float = 0.0

    @property
    def duration(self) -> float | None:
        """Wall-clock seconds from first start to terminal state."""

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
    def ran(self) -> bool:
        return self.attempts > 0

    def describe(self) -> str:
        bits = [f"{self.name}: {self.state}"]
        if self.attempts > 1:
            bits.append(f"{self.attempts} attempts")
        if self.duration is not None:
            bits.append(f"{self.duration:.3f}s")
        if self.error:
            bits.append(str(self.error.get("message", "failed")))
        if self.skip_reason:
            bits.append(self.skip_reason)
        return " | ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "attempts": self.attempts,
            "retries": self.retries,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "result": self.result,
            "error": dict(self.error) if self.error else None,
            "timed_out": self.timed_out,
            "skip_reason": self.skip_reason,
            "resource_waits": self.resource_waits,
        }


@dataclass
class RunSnapshot:
    """What the log says about a whole run."""

    run_id: str
    workflow: str = ""
    state: RunState = RunState.PENDING
    params: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    tasks: dict[str, TaskSnapshot] = field(default_factory=dict)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0

    # -- queries -------------------------------------------------------------

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    @property
    def succeeded(self) -> bool:
        return self.state is RunState.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.state is RunState.FAILED

    @property
    def finished(self) -> bool:
        return self.state.is_terminal

    def task(self, name: str) -> TaskSnapshot:
        try:
            return self.tasks[name]
        except KeyError:
            raise EventLogCorrupt(f"run {self.run_id} has no task {name!r}") from None

    def task_names(self) -> tuple[str, ...]:
        return tuple(self.tasks)

    def states(self) -> dict[str, TaskState]:
        return {name: snapshot.state for name, snapshot in self.tasks.items()}

    def counts(self) -> dict[str, int]:
        return summarise_states(snapshot.state for snapshot in self.tasks.values())

    def results(self) -> dict[str, Any]:
        """Results of every task that succeeded, in declaration order."""

        return {
            name: snapshot.result
            for name, snapshot in self.tasks.items()
            if snapshot.succeeded
        }

    def failures(self) -> tuple[TaskSnapshot, ...]:
        return tuple(
            snapshot for snapshot in self.tasks.values() if snapshot.state.is_failure
        )

    def in_state(self, state: TaskState) -> tuple[TaskSnapshot, ...]:
        return tuple(
            snapshot for snapshot in self.tasks.values() if snapshot.state is state
        )

    def total_attempts(self) -> int:
        return sum(snapshot.attempts for snapshot in self.tasks.values())

    def describe(self) -> str:
        head = f"run {self.run_id} ({self.workflow}) {self.state}"
        if self.started_at is not None:
            head += f" started {format_iso(self.started_at)}"
        if self.duration is not None:
            head += f" took {self.duration:.3f}s"
        lines = [head]
        lines.extend(f"  {snapshot.describe()}" for snapshot in self.tasks.values())
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
            "event_count": self.event_count,
            "tasks": [snapshot.as_dict() for snapshot in self.tasks.values()],
        }


def replay(events: Iterable[Event]) -> RunSnapshot:
    """Fold ``events`` into a :class:`RunSnapshot`.

    The events must all belong to one run and arrive in sequence order, which is
    what any log this package wrote guarantees.
    """

    materialised: Sequence[Event] = list(events)
    if not materialised:
        raise EventLogCorrupt("cannot replay an empty event stream")

    run_id = materialised[0].run_id
    snapshot = RunSnapshot(run_id=run_id, event_count=len(materialised))
    expected_seq = 0

    for event in materialised:
        if event.run_id != run_id:
            raise EventLogCorrupt(
                f"event {event.seq} belongs to run {event.run_id!r}, not {run_id!r}"
            )
        if event.seq != expected_seq:
            raise EventLogCorrupt(
                f"expected sequence {expected_seq}, found {event.seq}"
            )
        expected_seq += 1
        _apply(snapshot, event)

    if not snapshot.state.is_terminal and snapshot.tasks:
        derived = derive_run_state(
            task.state for task in snapshot.tasks.values()
        )
        if derived.is_terminal:
            snapshot.state = derived

    return snapshot


def replay_log(log: Any) -> RunSnapshot:
    """Replay anything iterable of events -- an :class:`EventLog` or a list."""

    return replay(iter(log))


# --------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------


def _apply(snapshot: RunSnapshot, event: Event) -> None:
    kind = event.kind

    if kind is EventKind.RUN_STARTED:
        snapshot.workflow = str(event.get("workflow", ""))
        snapshot.params = dict(event.get("params", {}) or {})
        snapshot.started_at = event.at
        snapshot.state = RunState.RUNNING
        for name in event.get("tasks", ()) or ():
            snapshot.tasks.setdefault(str(name), TaskSnapshot(name=str(name)))
        return

    if kind is EventKind.RUN_FINISHED:
        snapshot.finished_at = event.at
        state = event.get("state")
        snapshot.state = RunState(state) if state else derive_run_state(
            task.state for task in snapshot.tasks.values()
        )
        return

    if kind is EventKind.RUN_CANCELLED:
        snapshot.finished_at = event.at
        snapshot.state = RunState.CANCELLED
        return

    if event.task is None:
        raise EventLogCorrupt(
            f"event {event.seq} of kind {kind} has no task", line=event.seq
        )

    task = snapshot.tasks.setdefault(event.task, TaskSnapshot(name=event.task))

    if kind is EventKind.TASK_SCHEDULED:
        task.state = TaskState.READY
        return

    if kind is EventKind.TASK_STARTED:
        attempt = event.attempt or task.attempts + 1
        if attempt != task.attempts + 1:
            raise EventLogCorrupt(
                f"task {task.name!r} started attempt {attempt} after {task.attempts}",
                line=event.seq,
            )
        task.attempts = attempt
        task.state = TaskState.RUNNING
        if task.started_at is None:
            task.started_at = event.at
        return

    if kind is EventKind.TASK_SUCCEEDED:
        _require_running(task, event)
        task.state = TaskState.SUCCEEDED
        task.finished_at = event.at
        task.result = event.get("result")
        task.running_seconds += float(event.get("elapsed", 0.0) or 0.0)
        return

    if kind is EventKind.TASK_FAILED:
        _require_running(task, event)
        task.state = TaskState.FAILED
        task.finished_at = event.at
        task.error = dict(event.get("error", {}) or {})
        task.running_seconds += float(event.get("elapsed", 0.0) or 0.0)
        return

    if kind is EventKind.TASK_TIMED_OUT:
        task.timed_out = True
        return

    if kind is EventKind.TASK_RETRY_SCHEDULED:
        _require_running(task, event)
        task.state = TaskState.RETRY_WAIT
        task.retries += 1
        task.error = dict(event.get("error", {}) or {}) or task.error
        return

    if kind is EventKind.TASK_SKIPPED:
        task.state = TaskState.SKIPPED
        task.finished_at = event.at
        task.skip_reason = event.get("reason")
        return

    if kind is EventKind.TASK_UPSTREAM_FAILED:
        task.state = TaskState.UPSTREAM_FAILED
        task.finished_at = event.at
        task.skip_reason = event.get("reason")
        return

    if kind is EventKind.TASK_CANCELLED:
        task.state = TaskState.CANCELLED
        task.finished_at = event.at
        task.skip_reason = event.get("reason")
        return

    if kind is EventKind.CONDITION_EVALUATED:
        snapshot.conditions.append(
            {
                "upstream": event.get("upstream"),
                "downstream": event.task,
                "condition": event.get("condition"),
                "result": event.get("result"),
            }
        )
        return

    if kind is EventKind.RESOURCE_BLOCKED:
        task.resource_waits += 1
        return

    if kind in (EventKind.RESOURCE_ACQUIRED, EventKind.RESOURCE_RELEASED):
        return

    raise EventLogCorrupt(f"unhandled event kind {kind}", line=event.seq)


def _require_running(task: TaskSnapshot, event: Event) -> None:
    if task.state is not TaskState.RUNNING:
        raise EventLogCorrupt(
            f"task {task.name!r} received {event.kind} while {task.state}",
            line=event.seq,
        )
