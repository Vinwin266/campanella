"""The scheduling loop.

One :class:`Scheduler` executes one workflow once.  The loop is small enough to
state in full:

1. release any task whose retry backoff has elapsed,
2. resolve every pending task against its trigger rule and edge conditions,
   repeatedly, until nothing more changes,
3. take the tasks that became ready, order them, and admit as many as the
   resource pools and the parallelism limit allow,
4. run the admitted batch, recording every outcome as an event,
5. if nothing is ready but something is waiting to retry, move the clock to the
   earliest retry and go round again.

The loop ends when no task is ready and none is waiting, which -- because the
graph is acyclic and every task reaches a terminal state -- always happens.

Every state change is written to the event log *before* it is believed, so the
log is a complete account of the run rather than a summary of it.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..errors import ExecutionError, TaskTimeout
from ..model.policy import FailurePolicy
from ..model.state import (
    RunState,
    TaskState,
    check_run_transition,
    check_task_transition,
    derive_run_state,
)
from ..model.workflow import Workflow
from ..store.events import EventKind
from ..store.log import EventLog
from ..store.memory import InMemoryStore, RunStore
from ..util.clock import Clock, Deadline, ManualClock, SystemClock
from ..util.ids import IdFactory
from .cancel import CancellationToken, cancellable_tasks
from .context import TaskContext
from .dispatch import Dispatcher, EdgeEvaluation, TaskDecision, Verdict, decide_task
from .executor import Executor, InlineExecutor, Invocation
from .resources import ResourceLedger
from .result import RunResult, TaskResult
from .retry import decide_retry

__all__ = ["Scheduler", "MAX_ITERATIONS"]

#: Safety net.  A well-formed workflow needs at most one iteration per task per
#: attempt; this bound is far above that and exists only so a bug in the loop
#: surfaces as an error instead of a hang.
MAX_ITERATIONS = 100_000


class Scheduler:
    """Runs one workflow to completion."""

    def __init__(
        self,
        workflow: Workflow,
        *,
        clock: Clock | None = None,
        executor: Executor | None = None,
        store: RunStore | None = None,
        ids: IdFactory | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        self.workflow = workflow
        self.clock: Clock = clock or ManualClock()
        self.executor: Executor = executor or InlineExecutor()
        # ``or`` would be wrong for both of these: a store with no runs in it is
        # falsy because it has a length, and a token that has not been cancelled
        # is falsy by design.  Either would be silently swapped for a fresh one.
        self.store: RunStore = InMemoryStore() if store is None else store
        self.ids = ids or IdFactory()
        self.token = CancellationToken() if token is None else token

        self.dispatcher = Dispatcher(workflow)
        self.ledger = ResourceLedger(workflow.pools)

        self._order = workflow.topological_order()
        self._downstream = workflow.downstream_map()
        self._states: dict[str, TaskState] = {}
        self._attempts: dict[str, int] = {}
        self._retries: dict[str, int] = {}
        self._results: dict[str, Any] = {}
        self._errors: dict[str, BaseException] = {}
        self._retry_at: dict[str, float] = {}
        self._started_at: dict[str, float] = {}
        self._finished_at: dict[str, float] = {}
        self._timed_out: dict[str, bool] = {}
        self._reasons: dict[str, str] = {}
        self._notes: dict[str, tuple[str, ...]] = {}
        self._conditions: dict[tuple[str, str], EdgeEvaluation] = {}
        self._logged_conditions: set[tuple[str, str]] = set()
        self._params: dict[str, Any] = {}
        self._run_id = ""
        self._log: EventLog | None = None
        self._run_state = RunState.PENDING

    # -- public API ----------------------------------------------------------

    def run(
        self, params: Mapping[str, Any] | None = None, *, run_id: str | None = None
    ) -> RunResult:
        """Execute the workflow once and return the result."""

        self._reset()
        self._params = self.workflow.params.bind(params)
        self._run_id = run_id or self.ids.run_id(self.workflow.name, self.clock.now())
        self._log = self.store.create(self._run_id)

        self._emit(
            EventKind.RUN_STARTED,
            workflow=self.workflow.name,
            params=dict(self._params),
            tasks=list(self._order),
        )
        self._run_state = check_run_transition(
            self._run_id, self._run_state, RunState.RUNNING
        )

        try:
            self._loop()
        finally:
            self.ledger.release_all()

        return self._finish()

    # -- the loop ------------------------------------------------------------

    def _loop(self) -> None:
        for _ in range(MAX_ITERATIONS):
            if self.token.cancelled:
                self._cancel_remaining(self.token.reason)
                return

            self._release_due_retries()
            self._resolve_pending()

            ready = [
                name for name in self._order if self._states[name] is TaskState.READY
            ]
            if ready:
                self._dispatch(ready)
                continue

            waiting = self._earliest_retry()
            if waiting is not None:
                self._wait_until(waiting)
                continue

            return

        raise ExecutionError(
            f"run {self._run_id} exceeded {MAX_ITERATIONS} scheduling iterations",
            run_id=self._run_id,
        )

    def _dispatch(self, ready: list[str]) -> None:
        limit = self._parallelism_limit()
        admission = self.dispatcher.admit(ready, self.ledger, max_in_flight=limit)

        for name, blockers in admission.blocked:
            self._emit(
                EventKind.RESOURCE_BLOCKED,
                task=name,
                pools=list(blockers),
                available=self.ledger.describe(),
            )

        if not admission.admitted:
            raise ExecutionError(
                f"run {self._run_id} cannot admit any of {', '.join(ready)}; "
                f"pools are {self.ledger.describe()}",
                run_id=self._run_id,
            )

        for name in admission.admitted:
            spec = self.workflow.task(name)
            if spec.resources:
                self._emit(
                    EventKind.RESOURCE_ACQUIRED,
                    task=name,
                    pools=dict(spec.resources),
                )

        try:
            for name in admission.admitted:
                # A fail-fast failure earlier in this very wave must stop the
                # rest of it; the tasks stay READY and the loop cancels them.
                if self.token.cancelled:
                    break
                self._execute(name)
        finally:
            for name in admission.admitted:
                if self.ledger.is_held_by(name):
                    self.ledger.release(name)
                    spec = self.workflow.task(name)
                    if spec.resources:
                        self._emit(
                            EventKind.RESOURCE_RELEASED,
                            task=name,
                            pools=dict(spec.resources),
                        )

    def _execute(self, name: str) -> None:
        spec = self.workflow.task(name)
        attempt = self._attempts[name] + 1
        self._attempts[name] = attempt
        self._transition(name, TaskState.RUNNING)
        self._started_at.setdefault(name, self.clock.now())
        self._emit(EventKind.TASK_STARTED, task=name, attempt=attempt)

        context = TaskContext(
            run_id=self._run_id,
            workflow=self.workflow.name,
            spec=spec,
            attempt=attempt,
            params=self._params,
            results=self._results,
            upstreams=self.workflow.upstreams_of(name),
            downstreams=self.workflow.downstreams_of(name),
            clock=self.clock,
            deadline=Deadline(self.clock, spec.timeout),
        )
        completion = self.executor.run(Invocation(spec=spec, context=context))
        self._notes[name] = completion.notes

        if completion.timed_out:
            self._timed_out[name] = True
            self._emit(
                EventKind.TASK_TIMED_OUT,
                task=name,
                attempt=attempt,
                timeout=spec.timeout,
                elapsed=completion.elapsed,
            )

        if completion.ok:
            self._succeed(name, attempt, completion.value, completion.elapsed, completion.notes)
        else:
            error = completion.error or TaskTimeout(
                name, spec.timeout or 0.0, attempt=attempt
            )
            self._fail(name, attempt, error, completion)

        if completion.cancelled and self.token.cancel(
            f"task {name!r} requested cancellation", at=self.clock.now()
        ):
            self._emit(EventKind.RUN_CANCELLED, reason=self.token.reason)

    def _succeed(
        self,
        name: str,
        attempt: int,
        value: Any,
        elapsed: float,
        notes: tuple[str, ...],
    ) -> None:
        self._transition(name, TaskState.SUCCEEDED)
        self._results[name] = value
        # An earlier attempt may have recorded an error; a success erases it, so
        # the result reports what finally happened rather than what went wrong
        # on the way there.  ``retries`` still records that it took more than one.
        self._errors.pop(name, None)
        self._finished_at[name] = self.clock.now()
        self._emit(
            EventKind.TASK_SUCCEEDED,
            task=name,
            attempt=attempt,
            result=value,
            elapsed=elapsed,
            notes=list(notes) or None,
        )

    def _fail(self, name: str, attempt: int, error: BaseException, completion: Any) -> None:
        spec = self.workflow.task(name)
        decision = decide_retry(
            spec.retry,
            task=name,
            attempt=attempt,
            now=self.clock.now(),
            error=error,
            timed_out=completion.timed_out,
            cancelled=completion.cancelled,
        )

        if decision.retry and decision.at is not None:
            self._transition(name, TaskState.RETRY_WAIT)
            self._retries[name] += 1
            self._retry_at[name] = decision.at
            self._errors[name] = error
            self._emit(
                EventKind.TASK_RETRY_SCHEDULED,
                task=name,
                attempt=attempt,
                delay=decision.delay,
                retry_at=decision.at,
                reason=decision.reason,
                error=_error_payload(error),
            )
            return

        self._transition(name, TaskState.FAILED)
        self._errors[name] = error
        self._finished_at[name] = self.clock.now()
        self._emit(
            EventKind.TASK_FAILED,
            task=name,
            attempt=attempt,
            elapsed=completion.elapsed,
            reason=decision.reason,
            error=_error_payload(error),
        )

        if self.workflow.failure_policy is FailurePolicy.FAIL_FAST:
            if self.token.cancel(f"task {name!r} failed", at=self.clock.now()):
                self._emit(EventKind.RUN_CANCELLED, reason=self.token.reason)

    # -- resolution ----------------------------------------------------------

    def _resolve_pending(self) -> None:
        """Move pending tasks forward until nothing more changes."""

        for _ in range(len(self._order) + 1):
            changed = False
            for name in self._order:
                if self._states[name] is not TaskState.PENDING:
                    continue
                decision = decide_task(
                    self.workflow,
                    name,
                    self._states,
                    results=self._results,
                    params=self._params,
                    run_id=self._run_id,
                    condition_cache=self._conditions,
                )
                self._log_conditions(decision)
                if decision.verdict is Verdict.WAIT:
                    continue
                changed = True
                if decision.verdict is Verdict.RUN:
                    self._transition(name, TaskState.READY)
                    self._emit(EventKind.TASK_SCHEDULED, task=name, reason=decision.reason)
                elif decision.verdict is Verdict.SKIP:
                    self._terminate(name, TaskState.SKIPPED, decision.reason, EventKind.TASK_SKIPPED)
                else:
                    self._terminate(
                        name,
                        TaskState.UPSTREAM_FAILED,
                        decision.reason,
                        EventKind.TASK_UPSTREAM_FAILED,
                    )
            if not changed:
                return

    def _log_conditions(self, decision: TaskDecision) -> None:
        for evaluation in decision.edges:
            if not evaluation.was_evaluated:
                continue
            key = (evaluation.edge.upstream, evaluation.edge.downstream)
            if key in self._logged_conditions:
                continue
            self._logged_conditions.add(key)
            self._emit(
                EventKind.CONDITION_EVALUATED,
                task=evaluation.edge.downstream,
                upstream=evaluation.edge.upstream,
                condition=evaluation.edge.condition,
                result=evaluation.condition_result,
                error=evaluation.error,
            )

    def _release_due_retries(self) -> None:
        now = self.clock.now()
        for name in self._order:
            if self._states[name] is not TaskState.RETRY_WAIT:
                continue
            if self._retry_at.get(name, 0.0) <= now:
                self._transition(name, TaskState.READY)
                self._emit(
                    EventKind.TASK_SCHEDULED,
                    task=name,
                    reason=f"retry {self._retries[name]}",
                )

    def _earliest_retry(self) -> float | None:
        moments = [
            self._retry_at[name]
            for name in self._order
            if self._states[name] is TaskState.RETRY_WAIT and name in self._retry_at
        ]
        return min(moments) if moments else None

    def _cancel_remaining(self, reason: str) -> None:
        for name in cancellable_tasks(self._states, self._order):
            self._terminate(name, TaskState.CANCELLED, reason, EventKind.TASK_CANCELLED)

    # -- bookkeeping ---------------------------------------------------------

    def _reset(self) -> None:
        self._states = {name: TaskState.PENDING for name in self._order}
        self._attempts = {name: 0 for name in self._order}
        self._retries = {name: 0 for name in self._order}
        self._results.clear()
        self._errors.clear()
        self._retry_at.clear()
        self._started_at.clear()
        self._finished_at.clear()
        self._timed_out.clear()
        self._reasons.clear()
        self._notes.clear()
        self._conditions.clear()
        self._logged_conditions.clear()
        self.ledger.reset()
        self._run_state = RunState.PENDING

    def _transition(self, name: str, target: TaskState) -> None:
        current = self._states[name]
        self._states[name] = check_task_transition(name, current, target)

    def _terminate(
        self, name: str, state: TaskState, reason: str, kind: EventKind
    ) -> None:
        self._transition(name, state)
        self._reasons[name] = reason
        self._finished_at[name] = self.clock.now()
        self._emit(kind, task=name, reason=reason)

    def _emit(self, kind: EventKind, **payload: Any) -> None:
        if self._log is None:  # pragma: no cover - defensive
            raise ExecutionError("no event log for this run", run_id=self._run_id)
        task = payload.pop("task", None)
        attempt = payload.pop("attempt", None)
        self._log.append(kind, self.clock.now(), task=task, attempt=attempt, **payload)

    def _parallelism_limit(self) -> int:
        limit = self.executor.max_in_flight
        if self.workflow.max_parallelism is not None:
            limit = min(limit, self.workflow.max_parallelism)
        return max(1, limit)

    def _wait_until(self, timestamp: float) -> None:
        """Move the run's clock to ``timestamp``.

        A manual clock jumps, which is what makes a retry-heavy workflow finish
        instantly under test.  A real clock sleeps, because there is nothing else
        it can do.
        """

        now = self.clock.now()
        if timestamp <= now:
            return
        if isinstance(self.clock, ManualClock):
            self.clock.advance_to(timestamp)
            return
        if isinstance(self.clock, SystemClock):  # pragma: no cover - timing dependent
            time.sleep(timestamp - now)
            return
        raise ExecutionError(  # pragma: no cover - custom clocks must advance
            f"clock {type(self.clock).__name__} cannot wait until {timestamp}",
            run_id=self._run_id,
        )

    def _finish(self) -> RunResult:
        state = derive_run_state(self._states[name] for name in self._order)
        if (
            state is RunState.FAILED
            and self.workflow.failure_policy is FailurePolicy.ISOLATE
        ):
            state = RunState.SUCCEEDED

        self._run_state = check_run_transition(self._run_id, self._run_state, state)
        self._emit(EventKind.RUN_FINISHED, state=state.value, tasks=len(self._order))
        self.store.flush(self._run_id)

        started, finished = self._run_span()
        return RunResult(
            run_id=self._run_id,
            workflow=self.workflow.name,
            state=self._run_state,
            params=dict(self._params),
            tasks={name: self._task_result(name) for name in self._order},
            started_at=started,
            finished_at=finished,
            log=self._log,
        )

    def _run_span(self) -> tuple[float | None, float | None]:
        if self._log is None or not len(self._log):  # pragma: no cover - defensive
            return (None, None)
        span = self._log.span()
        return span if span is not None else (None, None)

    def _task_result(self, name: str) -> TaskResult:
        return TaskResult(
            name=name,
            state=self._states[name],
            attempts=self._attempts[name],
            value=self._results.get(name),
            error=self._errors.get(name),
            started_at=self._started_at.get(name),
            finished_at=self._finished_at.get(name),
            retries=self._retries[name],
            timed_out=self._timed_out.get(name, False),
            reason=self._reasons.get(name),
            notes=self._notes.get(name, ()),
        )

    def __repr__(self) -> str:
        return f"Scheduler({self.workflow.name!r})"


def _error_payload(error: BaseException) -> dict[str, Any]:
    payload = {"type": type(error).__name__, "message": str(error)}
    code = getattr(error, "code", None)
    if isinstance(code, str):
        payload["code"] = code
    return payload
