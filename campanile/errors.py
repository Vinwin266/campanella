"""Exception hierarchy for campanile.

Every exception raised on purpose by this package derives from :class:`CampanileError`
and carries a stable machine-readable ``code``.  Codes are part of the public
surface: the CLI prints them, the event log stores them, and callers are expected
to branch on them rather than on message text.

The hierarchy is deliberately shallow.  Three families exist:

``definition``
    Something is wrong with a workflow *before* it runs -- unknown tasks, cycles,
    bad cron strings, malformed expressions.  These are raised while building or
    validating, never while executing.

``execution``
    Something went wrong *during* a run -- a task raised, a timeout elapsed, a
    run was cancelled.  These are recorded on the run and usually not re-raised.

``storage``
    An event log could not be read back, or a run id does not exist.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = [
    "CampanileError",
    "ConfigurationError",
    "WorkflowDefinitionError",
    "DuplicateTaskError",
    "UnknownTaskError",
    "CycleError",
    "ValidationError",
    "Issue",
    "PoolError",
    "UnknownPoolError",
    "ResourceCapacityError",
    "ParameterError",
    "MissingParameterError",
    "ParameterTypeError",
    "ExpressionError",
    "ExpressionSyntaxError",
    "ExpressionEvaluationError",
    "UnknownFunctionError",
    "UnknownVariableError",
    "ScheduleError",
    "CronSyntaxError",
    "ExecutionError",
    "TaskFailed",
    "TaskTimeout",
    "RunCancelled",
    "StateTransitionError",
    "StoreError",
    "UnknownRunError",
    "EventLogCorrupt",
]


class CampanileError(Exception):
    """Base class for every error this package raises deliberately."""

    #: Stable identifier, snake_case, unique per concrete subclass.
    code = "campanile_error"

    #: Coarse family used by the CLI to decide an exit status.
    family = "definition"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view used by the event log and the CLI."""

        payload: dict[str, Any] = {
            "code": self.code,
            "family": self.family,
            "message": self.message,
        }
        if self.details:
            payload["details"] = dict(sorted(self.details.items()))
        return payload


# --------------------------------------------------------------------------
# definition-time errors
# --------------------------------------------------------------------------


class ConfigurationError(CampanileError):
    """A workflow, task, or pool was declared incorrectly."""

    code = "configuration_error"


class WorkflowDefinitionError(ConfigurationError):
    """The shape of a workflow definition is invalid."""

    code = "workflow_definition_error"

    def __init__(self, message: str, *, workflow: str | None = None, **details: Any) -> None:
        super().__init__(message, workflow=workflow, **details)
        self.workflow = workflow


class DuplicateTaskError(WorkflowDefinitionError):
    """Two tasks in one workflow share a name."""

    code = "duplicate_task"

    def __init__(self, task: str, *, workflow: str | None = None) -> None:
        super().__init__(f"task {task!r} is declared more than once", workflow=workflow, task=task)
        self.task = task


class UnknownTaskError(WorkflowDefinitionError):
    """An edge, trigger, or lookup referenced a task that does not exist."""

    code = "unknown_task"

    def __init__(self, task: str, *, workflow: str | None = None, known: Iterable[str] = ()) -> None:
        known_list = sorted(known)
        message = f"unknown task {task!r}"
        if known_list:
            message += f" (known tasks: {', '.join(known_list)})"
        super().__init__(message, workflow=workflow, task=task)
        self.task = task
        self.known = tuple(known_list)


class CycleError(WorkflowDefinitionError):
    """The dependency graph contains a cycle."""

    code = "dependency_cycle"

    def __init__(self, cycle: Sequence[str], *, workflow: str | None = None) -> None:
        rendered = " -> ".join(cycle)
        super().__init__(f"dependency cycle: {rendered}", workflow=workflow, cycle=list(cycle))
        self.cycle = tuple(cycle)


class Issue:
    """One problem found by the static validator.

    Issues are values, not exceptions; a validation pass collects many of them
    and only raises once, so a caller sees every problem at the same time.
    """

    __slots__ = ("code", "message", "location", "severity")

    def __init__(
        self,
        code: str,
        message: str,
        *,
        location: str = "",
        severity: str = "error",
    ) -> None:
        if severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {severity!r}")
        self.code = code
        self.message = message
        self.location = location
        self.severity = severity

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Issue):
            return NotImplemented
        return (self.code, self.message, self.location, self.severity) == (
            other.code,
            other.message,
            other.location,
            other.severity,
        )

    def __hash__(self) -> int:
        return hash((self.code, self.message, self.location, self.severity))

    def __repr__(self) -> str:
        return f"Issue({self.severity}, {self.code!r}, {self.location!r}, {self.message!r})"

    def render(self) -> str:
        """Render as ``severity: location: message [code]``."""

        head = f"{self.severity}: "
        if self.location:
            head += f"{self.location}: "
        return f"{head}{self.message} [{self.code}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "severity": self.severity,
        }


class ValidationError(ConfigurationError):
    """Raised once for a batch of :class:`Issue` values."""

    code = "validation_failed"

    def __init__(self, issues: Sequence[Issue], *, workflow: str | None = None) -> None:
        errors = [issue for issue in issues if issue.is_error]
        head = f"{len(errors)} validation error(s)"
        if workflow:
            head += f" in workflow {workflow!r}"
        body = "\n".join("  " + issue.render() for issue in issues)
        super().__init__(f"{head}:\n{body}" if body else head, workflow=workflow)
        self.issues = tuple(issues)
        self.workflow = workflow

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.is_error)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if not issue.is_error)


class PoolError(ConfigurationError):
    """Base class for resource pool problems."""

    code = "pool_error"


class UnknownPoolError(PoolError):
    """A task asked for a pool the workflow does not declare."""

    code = "unknown_pool"

    def __init__(self, pool: str, *, task: str | None = None, known: Iterable[str] = ()) -> None:
        known_list = sorted(known)
        message = f"unknown resource pool {pool!r}"
        if task:
            message += f" requested by task {task!r}"
        if known_list:
            message += f" (declared pools: {', '.join(known_list)})"
        super().__init__(message, pool=pool, task=task)
        self.pool = pool
        self.task = task


class ResourceCapacityError(PoolError):
    """A task requests more of a pool than the pool will ever have."""

    code = "resource_capacity"

    def __init__(self, pool: str, requested: int, capacity: int, *, task: str | None = None) -> None:
        super().__init__(
            f"task {task!r} requests {requested} slot(s) of pool {pool!r} "
            f"which only has capacity {capacity}",
            pool=pool,
            requested=requested,
            capacity=capacity,
            task=task,
        )
        self.pool = pool
        self.requested = requested
        self.capacity = capacity
        self.task = task


class ParameterError(ConfigurationError):
    """A run parameter is missing or of the wrong type."""

    code = "parameter_error"


class MissingParameterError(ParameterError):
    code = "missing_parameter"

    def __init__(self, name: str) -> None:
        super().__init__(f"required parameter {name!r} was not supplied", parameter=name)
        self.name = name


class ParameterTypeError(ParameterError):
    code = "parameter_type"

    def __init__(self, name: str, expected: str, got: Any) -> None:
        super().__init__(
            f"parameter {name!r} expects {expected}, got {type(got).__name__}",
            parameter=name,
            expected=expected,
        )
        self.name = name
        self.expected = expected


# --------------------------------------------------------------------------
# expression errors
# --------------------------------------------------------------------------


class ExpressionError(ConfigurationError):
    """Base class for the edge-condition expression language."""

    code = "expression_error"

    def __init__(self, message: str, *, source: str | None = None, position: int | None = None, **details: Any) -> None:
        super().__init__(message, source=source, position=position, **details)
        self.source = source
        self.position = position

    def annotate(self) -> str:
        """Return the message with a caret pointing at :attr:`position`."""

        if self.source is None or self.position is None:
            return self.message
        pointer = " " * self.position + "^"
        return f"{self.message}\n  {self.source}\n  {pointer}"


class ExpressionSyntaxError(ExpressionError):
    code = "expression_syntax"


class ExpressionEvaluationError(ExpressionError):
    code = "expression_evaluation"


class UnknownFunctionError(ExpressionError):
    code = "unknown_function"

    def __init__(self, name: str, *, known: Iterable[str] = (), **kwargs: Any) -> None:
        known_list = sorted(known)
        message = f"unknown function {name!r}"
        if known_list:
            message += f" (available: {', '.join(known_list)})"
        super().__init__(message, function=name, **kwargs)
        self.name = name


class UnknownVariableError(ExpressionError):
    code = "unknown_variable"

    def __init__(self, name: str, *, known: Iterable[str] = (), **kwargs: Any) -> None:
        known_list = sorted(known)
        message = f"unknown variable {name!r}"
        if known_list:
            message += f" (available: {', '.join(known_list)})"
        super().__init__(message, variable=name, **kwargs)
        self.name = name


# --------------------------------------------------------------------------
# schedule errors
# --------------------------------------------------------------------------


class ScheduleError(ConfigurationError):
    code = "schedule_error"


class CronSyntaxError(ScheduleError):
    code = "cron_syntax"

    def __init__(self, expression: str, reason: str, *, field: str | None = None) -> None:
        message = f"invalid cron expression {expression!r}: {reason}"
        if field:
            message = f"invalid cron expression {expression!r} in field {field!r}: {reason}"
        super().__init__(message, expression=expression, field=field)
        self.expression = expression
        self.field = field


# --------------------------------------------------------------------------
# execution-time errors
# --------------------------------------------------------------------------


class ExecutionError(CampanileError):
    """Base class for failures observed while a run is in flight."""

    code = "execution_error"
    family = "execution"


class TaskFailed(ExecutionError):
    """A task callable raised."""

    code = "task_failed"

    def __init__(self, task: str, cause: BaseException | None = None, *, attempt: int = 1) -> None:
        detail = f"{type(cause).__name__}: {cause}" if cause is not None else "no detail"
        super().__init__(
            f"task {task!r} failed on attempt {attempt} ({detail})",
            task=task,
            attempt=attempt,
        )
        self.task = task
        self.cause = cause
        self.attempt = attempt


class TaskTimeout(ExecutionError):
    """A task exceeded its declared timeout."""

    code = "task_timeout"

    def __init__(self, task: str, timeout: float, *, attempt: int = 1) -> None:
        super().__init__(
            f"task {task!r} exceeded its timeout of {timeout}s on attempt {attempt}",
            task=task,
            timeout=timeout,
            attempt=attempt,
        )
        self.task = task
        self.timeout = timeout
        self.attempt = attempt


class RunCancelled(ExecutionError):
    """A run was cancelled before every task reached a terminal state."""

    code = "run_cancelled"

    def __init__(self, run_id: str, reason: str = "cancelled") -> None:
        super().__init__(f"run {run_id} cancelled: {reason}", run_id=run_id, reason=reason)
        self.run_id = run_id
        self.reason = reason


class StateTransitionError(ExecutionError):
    """An illegal state transition was attempted."""

    code = "illegal_transition"

    def __init__(self, subject: str, from_state: Any, to_state: Any) -> None:
        super().__init__(
            f"{subject} cannot move from {from_state} to {to_state}",
            subject=subject,
            from_state=str(from_state),
            to_state=str(to_state),
        )
        self.subject = subject
        self.from_state = from_state
        self.to_state = to_state


# --------------------------------------------------------------------------
# storage errors
# --------------------------------------------------------------------------


class StoreError(CampanileError):
    code = "store_error"
    family = "storage"


class UnknownRunError(StoreError):
    code = "unknown_run"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"no run with id {run_id!r}", run_id=run_id)
        self.run_id = run_id


class EventLogCorrupt(StoreError):
    code = "event_log_corrupt"

    def __init__(self, reason: str, *, line: int | None = None) -> None:
        message = f"event log is corrupt: {reason}"
        if line is not None:
            message = f"event log is corrupt at line {line}: {reason}"
        super().__init__(message, line=line)
        self.line = line
