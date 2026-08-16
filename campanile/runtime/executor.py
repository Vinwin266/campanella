"""Running a single task body.

The executor is the seam between the scheduler's bookkeeping and the arbitrary
Python a workflow author wrote.  It is responsible for exactly three things:
calling the body the right way, catching whatever comes out of it, and measuring
how long it took.

Two implementations ship.  :class:`InlineExecutor` is the default and runs the
body on the calling thread, which is what makes a run reproducible.
:class:`RecordingExecutor` wraps another executor and remembers every invocation,
which is what tests assert against.

Timeouts are *observed*, not enforced.  There is no safe way to interrupt an
arbitrary callable mid-flight, so a task that overruns is reported as timed out
once it returns.  A task that wants to stop early is expected to call
:meth:`TaskContext.check`.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..model.task import TaskSpec
from .context import TaskContext

__all__ = [
    "Invocation",
    "Completion",
    "Executor",
    "InlineExecutor",
    "RecordingExecutor",
    "accepts_context",
    "call_body",
]


@dataclass(frozen=True)
class Invocation:
    """One attempt at one task."""

    spec: TaskSpec
    context: TaskContext

    @property
    def task(self) -> str:
        return self.spec.name

    @property
    def attempt(self) -> int:
        return self.context.attempt

    def describe(self) -> str:
        return f"{self.task}#{self.attempt}"


@dataclass(frozen=True)
class Completion:
    """What came back from one attempt."""

    task: str
    attempt: int
    ok: bool
    value: Any = None
    error: BaseException | None = None
    elapsed: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        return not self.ok

    def describe(self) -> str:
        outcome = "ok" if self.ok else "failed"
        if self.timed_out:
            outcome = "timed out"
        return f"{self.task}#{self.attempt} {outcome} in {self.elapsed:.3f}s"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "attempt": self.attempt,
            "ok": self.ok,
            "elapsed": self.elapsed,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "notes": list(self.notes),
        }


@runtime_checkable
class Executor(Protocol):
    """Anything that can run an :class:`Invocation`."""

    def run(self, invocation: Invocation) -> Completion:
        """Run the body and report what happened.  Must not raise."""

    @property
    def max_in_flight(self) -> int:
        """How many invocations may be admitted in one dispatch round."""


def accepts_context(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` should be called with a context argument.

    A body taking one or more positional parameters gets the context; a body
    taking none is called bare.  Anything whose signature cannot be read -- a
    builtin, a C callable -- is assumed to want the context, because that is
    what a workflow author writing a real task does.
    """

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return True

    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
    return False


def call_body(fn: Callable[..., Any], context: TaskContext) -> Any:
    """Call ``fn`` with or without the context, as its signature dictates."""

    if accepts_context(fn):
        return fn(context)
    return fn()


class InlineExecutor:
    """Runs bodies on the calling thread, one at a time.

    ``max_in_flight`` does not make this executor concurrent -- nothing here
    overlaps -- but it does bound how many tasks the scheduler admits into a
    single dispatch round, which is what makes resource pools and parallelism
    limits observable in the event log.
    """

    __slots__ = ("_max_in_flight",)

    def __init__(self, max_in_flight: int = 8) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        self._max_in_flight = max_in_flight

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    def run(self, invocation: Invocation) -> Completion:
        context = invocation.context
        spec = invocation.spec
        started = context.clock.monotonic()

        if spec.fn is None:
            return Completion(
                task=spec.name,
                attempt=context.attempt,
                ok=True,
                value=None,
                elapsed=0.0,
                notes=context.notes,
            )

        try:
            value = call_body(spec.fn, context)
        except BaseException as exc:  # noqa: BLE001 - the whole point is to catch everything
            elapsed = max(0.0, context.clock.monotonic() - started)
            return Completion(
                task=spec.name,
                attempt=context.attempt,
                ok=False,
                error=exc,
                elapsed=elapsed,
                timed_out=_overran(spec, elapsed),
                cancelled=context.cancelled,
                notes=context.notes,
            )

        elapsed = max(0.0, context.clock.monotonic() - started)
        overran = _overran(spec, elapsed)
        return Completion(
            task=spec.name,
            attempt=context.attempt,
            ok=not overran,
            value=None if overran else value,
            error=None,
            elapsed=elapsed,
            timed_out=overran,
            cancelled=context.cancelled,
            notes=context.notes,
        )

    def __repr__(self) -> str:
        return f"InlineExecutor(max_in_flight={self._max_in_flight})"


def _overran(spec: TaskSpec, elapsed: float) -> bool:
    return spec.timeout is not None and elapsed > spec.timeout


class RecordingExecutor:
    """Wraps another executor and remembers every invocation and completion."""

    __slots__ = ("_inner", "invocations", "completions")

    def __init__(self, inner: Executor | None = None) -> None:
        self._inner: Executor = inner or InlineExecutor()
        self.invocations: list[Invocation] = []
        self.completions: list[Completion] = []

    @property
    def max_in_flight(self) -> int:
        return self._inner.max_in_flight

    def run(self, invocation: Invocation) -> Completion:
        self.invocations.append(invocation)
        completion = self._inner.run(invocation)
        self.completions.append(completion)
        return completion

    @property
    def calls(self) -> tuple[str, ...]:
        """``("extract#1", "load#1")`` -- the call sequence, for assertions."""

        return tuple(invocation.describe() for invocation in self.invocations)

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(invocation.task for invocation in self.invocations)

    def attempts_of(self, task: str) -> int:
        return sum(1 for invocation in self.invocations if invocation.task == task)

    def reset(self) -> None:
        self.invocations.clear()
        self.completions.clear()

    def __repr__(self) -> str:
        return f"RecordingExecutor(calls={len(self.invocations)})"
