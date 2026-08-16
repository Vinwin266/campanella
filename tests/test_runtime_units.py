"""The runtime's pieces on their own: ledger, retry, executor, dispatch, cancel."""

from __future__ import annotations

import unittest

from campanile.errors import (
    ExecutionError,
    ResourceCapacityError,
    StateTransitionError,
    UnknownPoolError,
    UnknownTaskError,
)
from campanile.model.resource import ResourcePool
from campanile.model.policy import RetryPolicy, TriggerRule
from campanile.model.state import TaskState
from campanile.model.task import TaskSpec
from campanile.model.workflow import Workflow
from campanile.runtime.cancel import (
    CancellationToken,
    cancellable_tasks,
    downstream_of_failure,
)
from campanile.runtime.context import TaskContext
from campanile.runtime.dispatch import (
    PARALLELISM,
    Dispatcher,
    Verdict,
    condition_environment,
    decide_task,
)
from campanile.runtime.executor import (
    InlineExecutor,
    Invocation,
    RecordingExecutor,
    accepts_context,
    call_body,
)
from campanile.runtime.resources import ResourceLedger
from campanile.runtime.retry import attempt_schedule, decide_retry
from campanile.util.clock import Deadline, ManualClock


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ResourceLedger([ResourcePool("wh", 2), ResourcePool("lic", 1)])

    def test_starts_empty(self) -> None:
        self.assertEqual(self.ledger.used("wh"), 0)
        self.assertEqual(self.ledger.available("wh"), 2)

    def test_acquire_and_release(self) -> None:
        self.ledger.acquire("a", {"wh": 2})
        self.assertEqual(self.ledger.available("wh"), 0)
        self.assertTrue(self.ledger.is_held_by("a"))
        self.ledger.release("a")
        self.assertEqual(self.ledger.available("wh"), 2)
        self.assertFalse(self.ledger.is_held_by("a"))

    def test_acquisition_is_all_or_nothing(self) -> None:
        self.ledger.acquire("a", {"wh": 2})
        with self.assertRaises(ResourceCapacityError):
            self.ledger.acquire("b", {"lic": 1, "wh": 1})
        self.assertEqual(self.ledger.available("lic"), 1)

    def test_over_capacity_request_is_rejected(self) -> None:
        with self.assertRaises(ResourceCapacityError):
            self.ledger.acquire("a", {"wh": 3})

    def test_double_acquire_is_rejected(self) -> None:
        self.ledger.acquire("a", {"wh": 1})
        with self.assertRaises(StateTransitionError):
            self.ledger.acquire("a", {"wh": 1})

    def test_release_without_acquire_is_rejected(self) -> None:
        with self.assertRaises(StateTransitionError):
            self.ledger.release("a")

    def test_unknown_pool(self) -> None:
        with self.assertRaises(UnknownPoolError):
            self.ledger.acquire("a", {"ghost": 1})

    def test_can_admit_and_blocking_pools(self) -> None:
        self.ledger.acquire("a", {"wh": 2})
        self.assertFalse(self.ledger.can_admit({"wh": 1}))
        self.assertEqual(self.ledger.blocking_pools({"wh": 1, "lic": 1}), ("wh",))
        self.assertTrue(self.ledger.can_admit({"lic": 1}))

    def test_peak_is_remembered_after_release(self) -> None:
        self.ledger.acquire("a", {"wh": 2})
        self.ledger.release("a")
        self.assertEqual(self.ledger.peak("wh"), 2)

    def test_holders(self) -> None:
        self.ledger.acquire("a", {"wh": 1})
        self.ledger.acquire("b", {"lic": 1})
        self.assertEqual(self.ledger.holders(), ("a", "b"))
        self.assertEqual(self.ledger.holders("lic"), ("b",))

    def test_release_all_and_reset(self) -> None:
        self.ledger.acquire("a", {"wh": 1})
        self.ledger.acquire("b", {"wh": 1})
        self.assertEqual(len(self.ledger.release_all()), 2)
        self.ledger.acquire("c", {"wh": 1})
        self.ledger.reset()
        self.assertEqual(self.ledger.used("wh"), 0)
        self.assertEqual(self.ledger.peak("wh"), 0)

    def test_usage_and_describe(self) -> None:
        self.ledger.acquire("a", {"wh": 1})
        self.assertEqual(self.ledger.usage(), {"wh": (1, 2), "lic": (0, 1)})
        self.assertIn("wh: 1/2", self.ledger.describe())

    def test_empty_request_holds_nothing_but_registers(self) -> None:
        self.ledger.acquire("a", {})
        self.assertTrue(self.ledger.is_held_by("a"))
        self.assertEqual(self.ledger.used("wh"), 0)


class RetryDecisionTests(unittest.TestCase):
    def test_no_retry_when_attempts_are_exhausted(self) -> None:
        decision = decide_retry(
            RetryPolicy(max_attempts=2), task="t", attempt=2, now=0.0, error=ValueError()
        )
        self.assertFalse(decision.retry)
        self.assertIn("last", decision.reason)

    def test_retry_schedules_a_moment(self) -> None:
        decision = decide_retry(
            RetryPolicy(max_attempts=3, delay=5.0),
            task="t",
            attempt=1,
            now=100.0,
            error=ValueError(),
        )
        self.assertTrue(decision.retry)
        self.assertEqual(decision.delay, 5.0)
        self.assertEqual(decision.at, 105.0)

    def test_cancellation_beats_the_policy(self) -> None:
        decision = decide_retry(
            RetryPolicy(max_attempts=5),
            task="t",
            attempt=1,
            now=0.0,
            error=ValueError(),
            cancelled=True,
        )
        self.assertFalse(decision.retry)

    def test_allow_list_can_refuse_a_retry(self) -> None:
        decision = decide_retry(
            RetryPolicy(max_attempts=3, retry_on=("OSError",)),
            task="t",
            attempt=1,
            now=0.0,
            error=ValueError(),
        )
        self.assertFalse(decision.retry)
        self.assertIn("not retried", decision.reason)

    def test_a_timeout_is_retried_like_any_failure(self) -> None:
        decision = decide_retry(
            RetryPolicy(max_attempts=2, delay=1.0),
            task="t",
            attempt=1,
            now=0.0,
            timed_out=True,
        )
        self.assertTrue(decision.retry)

    def test_attempt_numbers_start_at_one(self) -> None:
        with self.assertRaises(ValueError):
            decide_retry(RetryPolicy(), task="t", attempt=0, now=0.0)

    def test_attempt_schedule(self) -> None:
        policy = RetryPolicy(max_attempts=3, delay=1.0, multiplier=2.0)
        self.assertEqual(attempt_schedule(policy, task="t", start=0.0), [0.0, 1.0, 3.0])


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(0.0)

    def context(self, spec: TaskSpec, attempt: int = 1) -> TaskContext:
        return TaskContext(
            run_id="r",
            workflow="w",
            spec=spec,
            attempt=attempt,
            params={},
            results={},
            clock=self.clock,
            deadline=Deadline(self.clock, spec.timeout),
        )

    def test_signature_detection(self) -> None:
        self.assertTrue(accepts_context(lambda ctx: None))
        self.assertTrue(accepts_context(lambda *args: None))
        self.assertFalse(accepts_context(lambda: None))
        self.assertFalse(accepts_context(lambda *, key=1: None))

    def test_call_body_matches_the_signature(self) -> None:
        spec = TaskSpec("t")
        context = self.context(spec)
        self.assertEqual(call_body(lambda: 1, context), 1)
        self.assertIs(call_body(lambda ctx: ctx, context), context)

    def test_successful_invocation(self) -> None:
        spec = TaskSpec.build("t", lambda ctx: "value")
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertTrue(completion.ok)
        self.assertEqual(completion.value, "value")

    def test_a_raising_body_is_caught(self) -> None:
        spec = TaskSpec.build("t", lambda ctx: 1 / 0)
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertFalse(completion.ok)
        self.assertIsInstance(completion.error, ZeroDivisionError)

    def test_body_less_tasks_succeed_immediately(self) -> None:
        spec = TaskSpec("t")
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertTrue(completion.ok)
        self.assertIsNone(completion.value)

    def test_elapsed_is_measured_on_the_run_clock(self) -> None:
        def body(ctx):
            ctx.clock.advance(3.0)
            return 1

        spec = TaskSpec.build("t", body)
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertEqual(completion.elapsed, 3.0)

    def test_overrunning_a_timeout_fails_the_attempt(self) -> None:
        def body(ctx):
            ctx.clock.advance(10.0)
            return "ignored"

        spec = TaskSpec.build("t", body, timeout="5s")
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertFalse(completion.ok)
        self.assertTrue(completion.timed_out)
        self.assertIsNone(completion.value)

    def test_running_exactly_to_the_timeout_is_allowed(self) -> None:
        def body(ctx):
            ctx.clock.advance(5.0)
            return "kept"

        spec = TaskSpec.build("t", body, timeout="5s")
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertTrue(completion.ok)
        self.assertEqual(completion.value, "kept")

    def test_notes_travel_with_the_completion(self) -> None:
        spec = TaskSpec.build("t", lambda ctx: ctx.note("hello", n=1))
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertEqual(completion.notes, ("hello n=1",))

    def test_cancellation_is_reported(self) -> None:
        spec = TaskSpec.build("t", lambda ctx: ctx.cancel("stop"))
        completion = InlineExecutor().run(Invocation(spec, self.context(spec)))
        self.assertTrue(completion.cancelled)

    def test_recording_executor_remembers_calls(self) -> None:
        recorder = RecordingExecutor()
        spec = TaskSpec.build("t", lambda ctx: 1)
        recorder.run(Invocation(spec, self.context(spec, attempt=1)))
        recorder.run(Invocation(spec, self.context(spec, attempt=2)))
        self.assertEqual(recorder.calls, ("t#1", "t#2"))
        self.assertEqual(recorder.attempts_of("t"), 2)
        recorder.reset()
        self.assertEqual(recorder.calls, ())

    def test_max_in_flight_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            InlineExecutor(max_in_flight=0)


class TaskContextTests(unittest.TestCase):
    def context(self, **kwargs) -> TaskContext:
        clock = kwargs.pop("clock", ManualClock(0.0))
        spec = kwargs.pop("spec", TaskSpec.build("t", lambda ctx: None, retry=3))
        return TaskContext(
            run_id="r",
            workflow="w",
            spec=spec,
            attempt=kwargs.pop("attempt", 1),
            params=kwargs.pop("params", {"region": "eu"}),
            results=kwargs.pop("results", {"up": 42}),
            upstreams=kwargs.pop("upstreams", ("up",)),
            clock=clock,
            deadline=Deadline(clock, kwargs.pop("timeout", None)),
        )

    def test_identity(self) -> None:
        context = self.context()
        self.assertEqual(context.task, "t")
        self.assertEqual(context.max_attempts, 3)
        self.assertFalse(context.is_retry)
        self.assertFalse(context.is_last_attempt)

    def test_last_attempt(self) -> None:
        self.assertTrue(self.context(attempt=3).is_last_attempt)

    def test_results_access(self) -> None:
        context = self.context()
        self.assertEqual(context.result("up"), 42)
        self.assertTrue(context.has_result("up"))
        self.assertEqual(context.result("ghost", "fallback"), "fallback")
        with self.assertRaises(UnknownTaskError):
            context.result("ghost")

    def test_none_is_a_valid_default(self) -> None:
        self.assertIsNone(self.context().result("ghost", None))

    def test_params(self) -> None:
        context = self.context()
        self.assertEqual(context.param("region"), "eu")
        self.assertIsNone(context.param("missing"))

    def test_results_mapping_is_a_copy(self) -> None:
        context = self.context()
        context.results["injected"] = 1
        self.assertFalse(context.has_result("injected"))

    def test_notes_accumulate(self) -> None:
        context = self.context()
        context.note("first")
        context.note("second", n=2)
        self.assertEqual(context.notes, ("first", "second n=2"))

    def test_elapsed_and_remaining(self) -> None:
        clock = ManualClock(0.0)
        context = self.context(clock=clock, timeout=10.0)
        clock.advance(4.0)
        self.assertEqual(context.elapsed(), 4.0)
        self.assertAlmostEqual(context.remaining(), 6.0)
        self.assertFalse(context.expired())

    def test_check_raises_once_the_deadline_passes(self) -> None:
        clock = ManualClock(0.0)
        context = self.context(clock=clock, timeout=5.0)
        context.check()
        clock.advance(6.0)
        with self.assertRaises(ExecutionError):
            context.check()

    def test_cancel_sets_the_flag_and_notes_it(self) -> None:
        context = self.context()
        context.cancel("operator")
        self.assertTrue(context.cancelled)
        self.assertIn("operator", context.notes[0])


class CancellationTests(unittest.TestCase):
    def test_first_cancel_wins(self) -> None:
        token = CancellationToken()
        self.assertTrue(token.cancel("first"))
        self.assertFalse(token.cancel("second"))
        self.assertEqual(token.reason, "first")

    def test_a_fresh_token_is_falsy(self) -> None:
        self.assertFalse(bool(CancellationToken()))
        token = CancellationToken()
        token.cancel()
        self.assertTrue(bool(token))

    def test_listeners_fire_once(self) -> None:
        token = CancellationToken()
        seen: list[str] = []
        token.subscribe(seen.append)
        token.cancel("stop")
        token.cancel("again")
        self.assertEqual(seen, ["stop"])

    def test_subscribing_after_cancellation_fires_immediately(self) -> None:
        token = CancellationToken()
        token.cancel("stop")
        seen: list[str] = []
        token.subscribe(seen.append)
        self.assertEqual(seen, ["stop"])

    def test_reset(self) -> None:
        token = CancellationToken()
        token.cancel("stop")
        token.reset()
        self.assertFalse(token.cancelled)

    def test_cancellable_tasks_skip_terminal_ones(self) -> None:
        states = {
            "a": TaskState.SUCCEEDED,
            "b": TaskState.PENDING,
            "c": TaskState.READY,
        }
        self.assertEqual(cancellable_tasks(states, ["a", "b", "c"]), ("b", "c"))

    def test_downstream_of_failure(self) -> None:
        downstream = {"a": ["b"], "b": ["c"], "c": []}
        states = {
            "a": TaskState.FAILED,
            "b": TaskState.PENDING,
            "c": TaskState.PENDING,
        }
        self.assertEqual(list(downstream_of_failure(["a"], downstream, states)), ["b", "c"])

    def test_downstream_of_failure_stops_at_terminal_tasks(self) -> None:
        downstream = {"a": ["b"], "b": ["c"], "c": []}
        states = {
            "a": TaskState.FAILED,
            "b": TaskState.SUCCEEDED,
            "c": TaskState.PENDING,
        }
        self.assertEqual(list(downstream_of_failure(["a"], downstream, states)), [])


class DecideTaskTests(unittest.TestCase):
    def build(self, **kwargs) -> Workflow:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", **kwargs)
        return flow

    def decide(self, flow: Workflow, states, results=None, params=None):
        return decide_task(
            flow,
            "down",
            states,
            results=results or {},
            params=params or {},
            run_id="r",
        )

    def test_no_upstreams_runs(self) -> None:
        flow = Workflow("w")
        flow.add("solo", lambda ctx: 1)
        decision = decide_task(
            flow, "solo", {"solo": TaskState.PENDING}, results={}, params={}, run_id="r"
        )
        self.assertIs(decision.verdict, Verdict.RUN)

    def test_waits_for_a_running_upstream(self) -> None:
        flow = self.build()
        decision = self.decide(flow, {"up": TaskState.RUNNING, "down": TaskState.PENDING})
        self.assertIs(decision.verdict, Verdict.WAIT)
        self.assertIn("up", decision.reason)

    def test_runs_once_the_upstream_succeeds(self) -> None:
        flow = self.build()
        decision = self.decide(flow, {"up": TaskState.SUCCEEDED, "down": TaskState.PENDING})
        self.assertIs(decision.verdict, Verdict.RUN)

    def test_a_failed_upstream_blocks(self) -> None:
        flow = self.build()
        decision = self.decide(flow, {"up": TaskState.FAILED, "down": TaskState.PENDING})
        self.assertIs(decision.verdict, Verdict.UPSTREAM_FAILED)

    def test_a_skipped_upstream_skips(self) -> None:
        flow = self.build()
        decision = self.decide(flow, {"up": TaskState.SKIPPED, "down": TaskState.PENDING})
        self.assertIs(decision.verdict, Verdict.SKIP)

    def test_all_done_runs_after_a_failure(self) -> None:
        flow = self.build(trigger_rule=TriggerRule.ALL_DONE)
        decision = self.decide(flow, {"up": TaskState.FAILED, "down": TaskState.PENDING})
        self.assertIs(decision.verdict, Verdict.RUN)

    def test_a_true_condition_lets_the_edge_through(self) -> None:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", condition="results.up > 0")
        decision = self.decide(
            flow, {"up": TaskState.SUCCEEDED, "down": TaskState.PENDING}, results={"up": 5}
        )
        self.assertIs(decision.verdict, Verdict.RUN)
        self.assertTrue(decision.edges[0].condition_result)

    def test_a_false_condition_skips(self) -> None:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", condition="results.up > 10")
        decision = self.decide(
            flow, {"up": TaskState.SUCCEEDED, "down": TaskState.PENDING}, results={"up": 5}
        )
        self.assertIs(decision.verdict, Verdict.SKIP)
        self.assertIs(decision.edges[0].effective_state, TaskState.SKIPPED)

    def test_a_broken_condition_is_an_upstream_failure(self) -> None:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", condition="results.up.missing")
        decision = self.decide(
            flow, {"up": TaskState.SUCCEEDED, "down": TaskState.PENDING}, results={"up": {}}
        )
        self.assertIs(decision.verdict, Verdict.UPSTREAM_FAILED)
        self.assertIsNotNone(decision.edges[0].error)

    def test_conditions_are_not_evaluated_for_failed_upstreams(self) -> None:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", condition="results.up > 0")
        decision = self.decide(flow, {"up": TaskState.FAILED, "down": TaskState.PENDING})
        self.assertIsNone(decision.edges[0].condition_result)
        self.assertIs(decision.verdict, Verdict.UPSTREAM_FAILED)

    def test_conditions_are_cached(self) -> None:
        flow = Workflow("w")
        flow.add("up", lambda ctx: 1)
        flow.add("down", lambda ctx: 2, after="up", condition="results.up > 0")
        cache: dict = {}
        states = {"up": TaskState.SUCCEEDED, "down": TaskState.PENDING}
        decide_task(flow, "down", states, results={"up": 5}, params={}, run_id="r", condition_cache=cache)
        self.assertEqual(len(cache), 1)

    def test_condition_environment_binds_four_names(self) -> None:
        env = condition_environment(
            run_id="r", workflow="w", params={}, results={}, upstream="a", downstream="b"
        )
        self.assertEqual(env.names, ("params", "results", "run", "task"))


class DispatcherTests(unittest.TestCase):
    def build(self) -> Workflow:
        flow = Workflow("w")
        flow.pool("wh", 1)
        flow.add("late", lambda ctx: 1, priority=1, resources="wh")
        flow.add("early", lambda ctx: 1, priority=-1, resources="wh")
        flow.add("middle", lambda ctx: 1, resources="wh")
        return flow

    def test_ordering_is_priority_then_declaration(self) -> None:
        flow = self.build()
        dispatcher = Dispatcher(flow)
        self.assertEqual(
            dispatcher.order(["middle", "late", "early"]), ("early", "middle", "late")
        )

    def test_equal_priorities_keep_declaration_order(self) -> None:
        flow = Workflow("w")
        flow.add("b", lambda ctx: 1)
        flow.add("a", lambda ctx: 1)
        self.assertEqual(Dispatcher(flow).order(["a", "b"]), ("b", "a"))

    def test_admission_respects_pool_capacity(self) -> None:
        flow = self.build()
        ledger = ResourceLedger(flow.pools)
        admission = Dispatcher(flow).admit(["late", "early", "middle"], ledger)
        self.assertEqual(admission.admitted, ("early",))
        self.assertEqual([name for name, _ in admission.blocked], ["middle", "late"])
        self.assertEqual(admission.blockers_for("middle"), ("wh",))

    def test_admission_is_greedy_not_head_of_line_blocking(self) -> None:
        flow = Workflow("w")
        flow.pool("wh", 1)
        flow.add("big", lambda ctx: 1, resources={"wh": 1})
        flow.add("small", lambda ctx: 1)
        admission = Dispatcher(flow).admit(["big", "small"], ResourceLedger(flow.pools))
        self.assertEqual(admission.admitted, ("big", "small"))

    def test_parallelism_limit_blocks_the_rest(self) -> None:
        flow = Workflow("w")
        for name in ("a", "b", "c"):
            flow.add(name, lambda ctx: 1)
        admission = Dispatcher(flow).admit(
            ["a", "b", "c"], ResourceLedger(flow.pools), max_in_flight=2
        )
        self.assertEqual(admission.admitted, ("a", "b"))
        self.assertEqual(admission.blockers_for("c"), (PARALLELISM,))

    def test_in_flight_count_is_honoured(self) -> None:
        flow = Workflow("w")
        flow.add("a", lambda ctx: 1)
        admission = Dispatcher(flow).admit(
            ["a"], ResourceLedger(flow.pools), in_flight=2, max_in_flight=2
        )
        self.assertEqual(admission.admitted, ())

    def test_admitted_tasks_hold_their_reservations(self) -> None:
        flow = self.build()
        ledger = ResourceLedger(flow.pools)
        Dispatcher(flow).admit(["early"], ledger)
        self.assertTrue(ledger.is_held_by("early"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
