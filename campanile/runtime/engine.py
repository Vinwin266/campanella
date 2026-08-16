"""The facade most callers use.

An :class:`Engine` bundles the four things a run needs -- a clock, an executor, a
store and an id factory -- so that running a workflow is one line::

    engine = Engine()
    result = engine.run(flow, {"day": "2026-03-11"})

Everything the engine does is also reachable directly through
:class:`~campanile.runtime.scheduler.Scheduler`; the engine adds workflow
registration, validation on the way in, and the small conveniences (``plan``,
``runs``, ``snapshot``) that a command line needs.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..dsl.validate import assert_valid, validate as validate_workflow
from ..errors import Issue, WorkflowDefinitionError
from ..model.workflow import Workflow
from ..store.memory import InMemoryStore, RunStore
from ..store.replay import RunSnapshot, replay
from ..util.clock import Clock, ManualClock
from ..util.ids import IdFactory
from ..util.topology import depth_levels
from .cancel import CancellationToken
from .executor import Executor, InlineExecutor
from .result import RunResult
from .scheduler import Scheduler

__all__ = ["Engine"]


class Engine:
    """Runs workflows and remembers what happened."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        executor: Executor | None = None,
        store: RunStore | None = None,
        ids: IdFactory | None = None,
        validate: bool = True,
        strict: bool = False,
    ) -> None:
        self.clock: Clock = clock or ManualClock()
        self.executor: Executor = executor or InlineExecutor()
        # An empty store is falsy -- it has a length -- so this cannot be ``or``.
        self.store: RunStore = InMemoryStore() if store is None else store
        self.ids = ids or IdFactory()
        self.validate_before_run = validate
        self.strict = strict
        self._workflows: dict[str, Workflow] = {}

    # -- registration --------------------------------------------------------

    def register(self, workflow: Workflow) -> Workflow:
        """Remember a workflow by name so it can be run by name later."""

        if workflow.name in self._workflows:
            raise WorkflowDefinitionError(
                f"workflow {workflow.name!r} is already registered",
                workflow=workflow.name,
            )
        self._workflows[workflow.name] = workflow
        return workflow

    def register_all(self, workflows: Iterable[Workflow]) -> tuple[Workflow, ...]:
        return tuple(self.register(workflow) for workflow in workflows)

    def workflow(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError:
            known = ", ".join(sorted(self._workflows)) or "none"
            raise WorkflowDefinitionError(
                f"no workflow named {name!r} (registered: {known})"
            ) from None

    @property
    def workflow_names(self) -> tuple[str, ...]:
        return tuple(self._workflows)

    def __contains__(self, name: object) -> bool:
        return name in self._workflows

    # -- inspection ----------------------------------------------------------

    def check(self, workflow: Workflow | str) -> list[Issue]:
        """Validate without running.  Returns issues rather than raising."""

        return validate_workflow(self._resolve(workflow))

    def plan(self, workflow: Workflow | str) -> list[list[str]]:
        """The waves a run would go through if nothing blocked or failed.

        Wave *n* holds the tasks whose deepest upstream chain is *n* long.  It is
        an upper bound on parallelism, not a promise: pools, priorities and
        conditions all narrow it at run time.
        """

        flow = self._resolve(workflow)
        levels = depth_levels(flow.task_names, flow.upstream_map())
        if not levels:
            return []
        waves: list[list[str]] = [[] for _ in range(max(levels.values()) + 1)]
        for name in flow.task_names:
            waves[levels[name]].append(name)
        return waves

    def describe(self, workflow: Workflow | str) -> str:
        return self._resolve(workflow).describe()

    # -- running -------------------------------------------------------------

    def run(
        self,
        workflow: Workflow | str,
        params: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        token: CancellationToken | None = None,
        executor: Executor | None = None,
    ) -> RunResult:
        """Execute ``workflow`` once."""

        flow = self._resolve(workflow)
        if self.validate_before_run:
            assert_valid(flow, strict=self.strict)
        else:
            flow.check_references()

        scheduler = Scheduler(
            flow,
            clock=self.clock,
            executor=self.executor if executor is None else executor,
            store=self.store,
            ids=self.ids,
            token=token,
        )
        return scheduler.run(params, run_id=run_id)

    def run_many(
        self,
        workflow: Workflow | str,
        parameter_sets: Sequence[Mapping[str, Any]],
    ) -> list[RunResult]:
        """Run the same workflow once per parameter set, in order.

        Runs are sequential and independent: a failure in one does not stop the
        next, because each parameter set is its own unit of work.
        """

        return [self.run(workflow, params) for params in parameter_sets]

    # -- history -------------------------------------------------------------

    def runs(self) -> tuple[str, ...]:
        return self.store.run_ids()

    def snapshot(self, run_id: str) -> RunSnapshot:
        return replay(self.store.log(run_id))

    def snapshots(self) -> tuple[RunSnapshot, ...]:
        return tuple(
            replay(self.store.log(run_id))
            for run_id in self.store.run_ids()
            if len(self.store.log(run_id))
        )

    def last_run(self) -> RunSnapshot | None:
        ids = self.store.run_ids()
        return self.snapshot(ids[-1]) if ids else None

    # -- internals -----------------------------------------------------------

    def _resolve(self, workflow: Workflow | str) -> Workflow:
        if isinstance(workflow, Workflow):
            return workflow
        return self.workflow(workflow)

    def __repr__(self) -> str:
        return (
            f"Engine(workflows={len(self._workflows)}, "
            f"runs={len(self.store.run_ids())})"
        )
