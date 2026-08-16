"""Task and run states, and the transitions between them.

The state machine is small enough to state completely, and doing so is what lets
the scheduler stay honest: every state change goes through :func:`check_task_transition`,
so an ordering bug shows up as a loud :class:`StateTransitionError` rather than a
run that quietly reports the wrong outcome.

Task lifecycle::

    PENDING ---> READY ---> RUNNING ---> SUCCEEDED
       |           |           |    \\
       |           |           |     `-> RETRY_WAIT ---> READY
       |           |           `-------> FAILED
       |           `-------------------> CANCELLED
       `-> SKIPPED | UPSTREAM_FAILED | CANCELLED
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping

from ..errors import StateTransitionError

__all__ = [
    "TaskState",
    "RunState",
    "TASK_TRANSITIONS",
    "RUN_TRANSITIONS",
    "check_task_transition",
    "check_run_transition",
    "derive_run_state",
    "summarise_states",
]


class TaskState(Enum):
    """Where one task inside one run currently is."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UPSTREAM_FAILED = "upstream_failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True once the task will not change state again in this run."""

        return self in _TERMINAL_TASK_STATES

    @property
    def is_success(self) -> bool:
        return self is TaskState.SUCCEEDED

    @property
    def is_failure(self) -> bool:
        """A failure the run should be judged by.

        ``SKIPPED`` is not a failure -- a skipped branch is a normal outcome of
        a condition evaluating false.  ``UPSTREAM_FAILED`` is not counted either;
        it is the shadow of some other task's failure, and counting both would
        double-report a single fault.
        """

        return self is TaskState.FAILED

    @property
    def ran(self) -> bool:
        """True if the task's callable was actually invoked at least once."""

        return self in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.RUNNING)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class RunState(Enum):
    """Where a whole run currently is."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


_TERMINAL_TASK_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.SKIPPED,
        TaskState.UPSTREAM_FAILED,
        TaskState.CANCELLED,
    }
)


#: Legal successors for every task state.  Terminal states have no successors.
TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {
            TaskState.READY,
            TaskState.SKIPPED,
            TaskState.UPSTREAM_FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.SKIPPED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.RETRY_WAIT,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRY_WAIT: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.SKIPPED: frozenset(),
    TaskState.UPSTREAM_FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def check_task_transition(task: str, current: TaskState, target: TaskState) -> TaskState:
    """Return ``target`` if the move is legal, otherwise raise.

    A transition to the current state is always legal and is a no-op; that makes
    idempotent bookkeeping ("mark everything cancelled") safe to run twice.
    """

    if current is target:
        return target
    if target not in TASK_TRANSITIONS[current]:
        raise StateTransitionError(f"task {task!r}", current, target)
    return target


def check_run_transition(run_id: str, current: RunState, target: RunState) -> RunState:
    """Run-level counterpart of :func:`check_task_transition`."""

    if current is target:
        return target
    if target not in RUN_TRANSITIONS[current]:
        raise StateTransitionError(f"run {run_id!r}", current, target)
    return target


def derive_run_state(states: Iterable[TaskState]) -> RunState:
    """Fold every task state into the state of the run as a whole.

    The rules, in priority order:

    * any task still non-terminal        -> ``RUNNING``
    * any task ``FAILED`` or ``UPSTREAM_FAILED`` -> ``FAILED``
    * any task ``CANCELLED``             -> ``CANCELLED``
    * otherwise                          -> ``SUCCEEDED``

    A run with no tasks at all succeeds; there was nothing to go wrong.
    """

    materialised = list(states)
    if not materialised:
        return RunState.SUCCEEDED
    if any(not state.is_terminal for state in materialised):
        return RunState.RUNNING
    if any(
        state in (TaskState.FAILED, TaskState.UPSTREAM_FAILED)
        for state in materialised
    ):
        return RunState.FAILED
    if any(state is TaskState.CANCELLED for state in materialised):
        return RunState.CANCELLED
    return RunState.SUCCEEDED


def summarise_states(states: Iterable[TaskState]) -> dict[str, int]:
    """Count states by name, including zero entries, in declaration order."""

    counts = {state.value: 0 for state in TaskState}
    for state in states:
        counts[state.value] += 1
    return counts
