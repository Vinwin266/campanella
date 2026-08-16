"""Execution.

The layer that actually runs things.  It reads a :class:`~campanile.model.Workflow`,
writes to a :class:`~campanile.store.log.EventLog`, and hands back a
:class:`RunResult`.

The pieces are deliberately separable: eligibility (``dispatch``), admission
(``dispatch`` + ``resources``), invocation (``executor``), retry arithmetic
(``retry``) and the loop that ties them together (``scheduler``).  Each can be
exercised without the others.
"""

from __future__ import annotations

from .cancel import CancellationToken, cancellable_tasks, downstream_of_failure
from .context import TaskContext
from .dispatch import (
    Admission,
    Dispatcher,
    EdgeEvaluation,
    TaskDecision,
    Verdict,
    condition_environment,
    decide_task,
)
from .engine import Engine
from .executor import (
    Completion,
    Executor,
    InlineExecutor,
    Invocation,
    RecordingExecutor,
    accepts_context,
    call_body,
)
from .resources import Reservation, ResourceLedger
from .result import RunResult, TaskResult
from .retry import RetryDecision, attempt_schedule, decide_retry
from .scheduler import MAX_ITERATIONS, Scheduler

__all__ = [
    "MAX_ITERATIONS",
    "Admission",
    "CancellationToken",
    "Completion",
    "Dispatcher",
    "EdgeEvaluation",
    "Engine",
    "Executor",
    "InlineExecutor",
    "Invocation",
    "RecordingExecutor",
    "Reservation",
    "ResourceLedger",
    "RetryDecision",
    "RunResult",
    "Scheduler",
    "TaskContext",
    "TaskDecision",
    "TaskResult",
    "Verdict",
    "accepts_context",
    "attempt_schedule",
    "call_body",
    "cancellable_tasks",
    "condition_environment",
    "decide_retry",
    "decide_task",
    "downstream_of_failure",
]
