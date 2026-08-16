"""Shared fixtures for the test suite.

Two things live here: task bodies with predictable behaviour (fail twice then
succeed, take exactly three seconds, record that they ran) and a base test case
that hands out a manual clock and a recording executor.

Nothing here reads the wall clock or the file system except through
:meth:`CampanileTestCase.temp_dir`, which cleans up after itself.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Sequence

from campanile.dsl.builder import WorkflowBuilder
from campanile.model.workflow import Workflow
from campanile.runtime.engine import Engine
from campanile.runtime.executor import InlineExecutor, RecordingExecutor
from campanile.runtime.result import RunResult
from campanile.util.clock import ManualClock

__all__ = [
    "CampanileTestCase",
    "ORIGIN",
    "counting_task",
    "failing_task",
    "flaky_task",
    "slow_task",
    "returns",
    "linear_workflow",
    "diamond_workflow",
]

#: 2026-03-01T00:00:00Z.  Every test that needs a starting instant uses this
#: one, so rendered timestamps are comparable across test modules.
ORIGIN = 1772323200.0


def returns(value: Any) -> Callable[..., Any]:
    """A task body that ignores its context and returns ``value``."""

    def body(ctx: Any) -> Any:
        return value

    return body


def counting_task(log: list[str], name: str, value: Any = None) -> Callable[..., Any]:
    """A body that appends ``name`` to ``log`` each time it runs."""

    def body(ctx: Any) -> Any:
        log.append(name)
        return value if value is not None else {"task": name}

    return body


def failing_task(message: str = "boom", kind: type[BaseException] = RuntimeError):
    """A body that always raises."""

    def body(ctx: Any) -> Any:
        raise kind(message)

    return body


def flaky_task(failures: int, value: Any = "ok", kind: type[BaseException] = RuntimeError):
    """A body that fails ``failures`` times and then succeeds."""

    state = {"calls": 0}

    def body(ctx: Any) -> Any:
        state["calls"] += 1
        if state["calls"] <= failures:
            raise kind(f"attempt {state['calls']} failed")
        return value

    return body


def slow_task(seconds: float, value: Any = "done"):
    """A body that advances the run's manual clock by ``seconds``."""

    def body(ctx: Any) -> Any:
        ctx.clock.advance(seconds)
        return value

    return body


def linear_workflow(name: str = "linear", length: int = 3) -> Workflow:
    """``t0 -> t1 -> ... -> tn`` with bodies that return their own index."""

    builder = WorkflowBuilder(name)
    previous: str | None = None
    for index in range(length):
        task_name = f"t{index}"
        builder.task(task_name, returns(index), after=previous or ())
        previous = task_name
    return builder.build()


def diamond_workflow(name: str = "diamond") -> Workflow:
    """``root -> {left, right} -> join``."""

    builder = WorkflowBuilder(name)
    root = builder.task("root", returns("root"))
    left = builder.task("left", returns("left"))
    right = builder.task("right", returns("right"))
    join = builder.task("join", returns("join"))
    root >> [left, right] >> join
    return builder.build()


class CampanileTestCase(unittest.TestCase):
    """Base case with a manual clock, a recording executor and temp dirs."""

    def setUp(self) -> None:  # noqa: N802 - unittest naming
        super().setUp()
        self.clock = ManualClock(ORIGIN)
        self.executor = RecordingExecutor(InlineExecutor(max_in_flight=8))
        self._temp_dirs: list[Path] = []

    def tearDown(self) -> None:  # noqa: N802 - unittest naming
        for path in self._temp_dirs:
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    # -- helpers -------------------------------------------------------------

    def temp_dir(self) -> Path:
        """A directory removed when the test finishes."""

        path = Path(tempfile.mkdtemp(prefix="campanile-test-"))
        self._temp_dirs.append(path)
        return path

    def engine(self, **kwargs: Any) -> Engine:
        """An engine wired to this test's clock and recording executor."""

        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("executor", self.executor)
        return Engine(**kwargs)

    def run_workflow(
        self, workflow: Workflow, params: Any = None, **kwargs: Any
    ) -> RunResult:
        """Run ``workflow`` on this test's engine."""

        return self.engine(**kwargs).run(workflow, params)

    # -- assertions ----------------------------------------------------------

    def assertStates(self, result: RunResult, expected: dict[str, str]) -> None:
        """Assert each named task ended in the named state."""

        actual = {name: str(state) for name, state in result.states().items()}
        for task, state in expected.items():
            self.assertIn(task, actual, f"no task {task!r} in the run")
            self.assertEqual(
                actual[task],
                state,
                f"task {task!r} ended {actual[task]}, expected {state}",
            )

    def assertCalls(self, expected: Sequence[str]) -> None:
        """Assert the recording executor saw exactly these invocations."""

        self.assertEqual(list(self.executor.calls), list(expected))

    def assertEventKinds(self, result: RunResult, *kinds: str) -> None:
        """Assert these event kinds appear, in this relative order."""

        seen = [event.kind.value for event in result.log or ()]
        position = -1
        for kind in kinds:
            self.assertIn(kind, seen[position + 1 :], f"{kind} not found after {position}")
            position = seen.index(kind, position + 1)
