"""Deciding what runs next.

Two separate questions, kept separate on purpose:

*Eligibility* -- given what the upstreams did, may this task run at all?  That is
:func:`decide_task`, and it is a pure function of the states, the edge
conditions and the trigger rule.

*Admission* -- of the tasks that may run, which fit right now?  That is
:class:`Dispatcher`, and it depends on the resource ledger and the parallelism
limit.

Keeping them apart means a workflow's control flow can be tested without any
resources in the picture, and resource behaviour can be tested without any
conditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from ..errors import ExpressionError
from ..expr.eval import Environment, compile_expression, is_truthy
from ..model.policy import TriggerDecision, evaluate_trigger_rule
from ..model.state import TaskState
from ..model.workflow import Edge, Workflow
from ..util.ordered import index_map
from .resources import ResourceLedger

__all__ = [
    "Verdict",
    "TaskDecision",
    "EdgeEvaluation",
    "Admission",
    "Dispatcher",
    "decide_task",
    "condition_environment",
]


class Verdict(Enum):
    """What the scheduler should do with a pending task."""

    RUN = "run"
    WAIT = "wait"
    SKIP = "skip"
    UPSTREAM_FAILED = "upstream_failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class EdgeEvaluation:
    """The outcome of one incoming edge."""

    edge: Edge
    upstream_state: TaskState
    effective_state: TaskState
    condition_result: bool | None = None
    error: str | None = None

    @property
    def was_evaluated(self) -> bool:
        return self.condition_result is not None or self.error is not None

    def describe(self) -> str:
        text = f"{self.edge.upstream}: {self.upstream_state}"
        if self.condition_result is not None:
            text += f" condition={'true' if self.condition_result else 'false'}"
        if self.error:
            text += f" condition error: {self.error}"
        if self.effective_state is not self.upstream_state:
            text += f" -> {self.effective_state}"
        return text


@dataclass(frozen=True)
class TaskDecision:
    """Whether a task may start, and why."""

    task: str
    verdict: Verdict
    reason: str = ""
    edges: tuple[EdgeEvaluation, ...] = field(default_factory=tuple)

    @property
    def runnable(self) -> bool:
        return self.verdict is Verdict.RUN

    @property
    def terminal(self) -> bool:
        return self.verdict in (Verdict.SKIP, Verdict.UPSTREAM_FAILED)

    def describe(self) -> str:
        text = f"{self.task}: {self.verdict}"
        if self.reason:
            text += f" ({self.reason})"
        return text


def condition_environment(
    *,
    run_id: str,
    workflow: str,
    params: Mapping[str, Any],
    results: Mapping[str, Any],
    upstream: str,
    downstream: str,
) -> Environment:
    """Build the bindings an edge condition is evaluated against.

    Four names are bound and no more: ``params``, ``results``, ``run`` and
    ``task``.  Anything else in a condition is a typo, and the validator says so
    before the run ever starts.
    """

    return Environment.of(
        params=dict(params),
        results=dict(results),
        run={"id": run_id, "workflow": workflow},
        task={"name": downstream, "upstream": upstream},
    )


def decide_task(
    workflow: Workflow,
    task: str,
    states: Mapping[str, TaskState],
    *,
    results: Mapping[str, Any],
    params: Mapping[str, Any],
    run_id: str,
    condition_cache: dict[tuple[str, str], EdgeEvaluation] | None = None,
) -> TaskDecision:
    """Decide whether ``task`` may run, given what its upstreams have done.

    An edge with a condition contributes its upstream's state unchanged unless
    that upstream *succeeded*, in which case the condition decides: true leaves
    the state as ``SUCCEEDED``, false downgrades it to ``SKIPPED``.  Conditions
    are only evaluated for successful upstreams, because a condition reading the
    result of a task that failed has nothing to read.

    A condition that raises downgrades the edge to ``UPSTREAM_FAILED``: a broken
    condition is a real fault and must not be mistaken for "the branch was not
    taken".
    """

    spec = workflow.task(task)
    incoming = workflow.edges_into(task)

    if not incoming:
        return TaskDecision(task, Verdict.RUN, reason="no upstreams")

    evaluations: list[EdgeEvaluation] = []
    for edge in incoming:
        upstream_state = states.get(edge.upstream, TaskState.PENDING)
        evaluations.append(
            _evaluate_edge(
                edge,
                upstream_state,
                results=results,
                params=params,
                run_id=run_id,
                workflow_name=workflow.name,
                cache=condition_cache,
            )
        )

    effective = [evaluation.effective_state for evaluation in evaluations]
    decision = evaluate_trigger_rule(spec.trigger_rule, effective)

    if decision is TriggerDecision.RUN:
        return TaskDecision(task, Verdict.RUN, reason=str(spec.trigger_rule), edges=tuple(evaluations))

    if decision is TriggerDecision.WAIT:
        pending = [
            evaluation.edge.upstream
            for evaluation in evaluations
            if not evaluation.effective_state.is_terminal
        ]
        return TaskDecision(
            task,
            Verdict.WAIT,
            reason="waiting for " + ", ".join(pending) if pending else "waiting",
            edges=tuple(evaluations),
        )

    blamed = [
        evaluation
        for evaluation in evaluations
        if evaluation.effective_state
        in (TaskState.FAILED, TaskState.UPSTREAM_FAILED)
    ]
    if blamed:
        names = ", ".join(evaluation.edge.upstream for evaluation in blamed)
        errors = [evaluation.error for evaluation in blamed if evaluation.error]
        reason = f"upstream {names} did not succeed"
        if errors:
            reason = f"condition on edge from {names} failed: {errors[0]}"
        return TaskDecision(task, Verdict.UPSTREAM_FAILED, reason=reason, edges=tuple(evaluations))

    unsatisfied = [
        evaluation.edge.upstream
        for evaluation in evaluations
        if evaluation.effective_state is not TaskState.SUCCEEDED
    ]
    reason = f"trigger rule {spec.trigger_rule} not satisfied"
    if unsatisfied:
        reason += " (" + ", ".join(unsatisfied) + ")"
    return TaskDecision(task, Verdict.SKIP, reason=reason, edges=tuple(evaluations))


def _evaluate_edge(
    edge: Edge,
    upstream_state: TaskState,
    *,
    results: Mapping[str, Any],
    params: Mapping[str, Any],
    run_id: str,
    workflow_name: str,
    cache: dict[tuple[str, str], EdgeEvaluation] | None,
) -> EdgeEvaluation:
    key = (edge.upstream, edge.downstream)
    if cache is not None and key in cache:
        return cache[key]

    if edge.condition is None or upstream_state is not TaskState.SUCCEEDED:
        evaluation = EdgeEvaluation(
            edge=edge,
            upstream_state=upstream_state,
            effective_state=upstream_state,
        )
        if cache is not None and upstream_state.is_terminal:
            cache[key] = evaluation
        return evaluation

    environment = condition_environment(
        run_id=run_id,
        workflow=workflow_name,
        params=params,
        results=results,
        upstream=edge.upstream,
        downstream=edge.downstream,
    )
    try:
        value = compile_expression(edge.condition).evaluate(environment)
    except ExpressionError as exc:
        evaluation = EdgeEvaluation(
            edge=edge,
            upstream_state=upstream_state,
            effective_state=TaskState.UPSTREAM_FAILED,
            error=exc.message,
        )
    else:
        satisfied = is_truthy(value)
        evaluation = EdgeEvaluation(
            edge=edge,
            upstream_state=upstream_state,
            effective_state=TaskState.SUCCEEDED if satisfied else TaskState.SKIPPED,
            condition_result=satisfied,
        )

    if cache is not None:
        cache[key] = evaluation
    return evaluation


@dataclass(frozen=True)
class Admission:
    """Which of the eligible tasks fit right now, and which did not."""

    admitted: tuple[str, ...] = ()
    blocked: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def any_admitted(self) -> bool:
        return bool(self.admitted)

    def blockers_for(self, task: str) -> tuple[str, ...]:
        for name, pools in self.blocked:
            if name == task:
                return pools
        return ()

    def describe(self) -> str:
        parts = []
        if self.admitted:
            parts.append("admitted " + ", ".join(self.admitted))
        for name, pools in self.blocked:
            parts.append(f"{name} blocked on {', '.join(pools)}")
        return "; ".join(parts) or "nothing to admit"


#: Pseudo-pool name reported when the workflow-level parallelism cap is what
#: stopped a task, rather than any declared pool.
PARALLELISM = "@parallelism"


class Dispatcher:
    """Orders eligible tasks and decides how many of them start now."""

    __slots__ = ("workflow", "_positions")

    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow
        self._positions = index_map(workflow.task_names)

    def order(self, tasks: Sequence[str]) -> tuple[str, ...]:
        """Sort by priority, then declaration order, then name.

        Priority is "lower runs first".  The declaration-order tie-break is what
        makes a run reproducible: two tasks at the same priority always start in
        the order the workflow declared them.
        """

        return tuple(
            sorted(
                tasks,
                key=lambda name: self.workflow.task(name).sort_key(
                    self._positions.get(name, 0)
                ),
            )
        )

    def admit(
        self,
        eligible: Sequence[str],
        ledger: ResourceLedger,
        *,
        in_flight: int = 0,
        max_in_flight: int | None = None,
    ) -> Admission:
        """Greedily admit eligible tasks in priority order.

        Admission is greedy rather than optimal: a task that does not fit is
        skipped and the next one is considered, so a small task never waits
        behind a large one it could have run alongside.  What it does *not* do is
        reorder across priorities, so a low-priority task cannot jump the queue
        just because it happens to fit.
        """

        admitted: list[str] = []
        blocked: list[tuple[str, tuple[str, ...]]] = []
        running = in_flight

        for name in self.order(eligible):
            spec = self.workflow.task(name)
            if max_in_flight is not None and running >= max_in_flight:
                blocked.append((name, (PARALLELISM,)))
                continue
            blockers = ledger.blocking_pools(spec.resources)
            if blockers:
                blocked.append((name, blockers))
                continue
            ledger.acquire(name, spec.resources)
            admitted.append(name)
            running += 1

        return Admission(admitted=tuple(admitted), blocked=tuple(blocked))

    def __repr__(self) -> str:
        return f"Dispatcher({self.workflow.name!r})"
