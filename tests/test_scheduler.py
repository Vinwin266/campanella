"""The scheduling loop, end to end."""

from __future__ import annotations

import unittest

from campanile.dsl.builder import WorkflowBuilder
from campanile.errors import ExecutionError, MissingParameterError, ValidationError
from campanile.model.policy import FailurePolicy, RetryPolicy, TriggerRule
from campanile.model.state import RunState, TaskState
from campanile.runtime.cancel import CancellationToken
from campanile.runtime.engine import Engine
from campanile.runtime.executor import InlineExecutor, RecordingExecutor
from campanile.store.events import EventKind
from campanile.store.memory import InMemoryStore
from campanile.store.replay import replay
from campanile.util.clock import ManualClock

from .helpers import (
    CampanileTestCase,
    counting_task,
    diamond_workflow,
    failing_task,
    flaky_task,
    linear_workflow,
    returns,
    slow_task,
)


class HappyPathTests(CampanileTestCase):
    def test_linear_workflow_runs_in_order(self) -> None:
        result = self.run_workflow(linear_workflow(length=4))
        self.assertIs(result.state, RunState.SUCCEEDED)
        self.assertCalls(["t0#1", "t1#1", "t2#1", "t3#1"])
        self.assertEqual(result.values(), {"t0": 0, "t1": 1, "t2": 2, "t3": 3})

    def test_diamond_joins_correctly(self) -> None:
        result = self.run_workflow(diamond_workflow())
        self.assertTrue(result.succeeded)
        self.assertCalls(["root#1", "left#1", "right#1", "join#1"])

    def test_results_flow_downstream(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns(21))
        builder.task("b", lambda ctx: ctx.result("a") * 2, after="a")
        result = self.run_workflow(builder.build())
        self.assertEqual(result.value("b"), 42)

    def test_a_body_taking_no_arguments_is_called_bare(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("bare", lambda: "value")
        self.assertEqual(self.run_workflow(builder.build()).value("bare"), "value")

    def test_a_gate_succeeds_with_no_value(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns(1))
        builder.gate("gate", after="a")
        builder.task("b", returns(2), after="gate")
        result = self.run_workflow(builder.build())
        self.assertTrue(result.succeeded)
        self.assertIsNone(result.value("gate"))

    def test_parameters_are_bound_and_visible(self) -> None:
        builder = WorkflowBuilder("flow").param("region", "string", required=True)
        builder.task("a", lambda ctx: ctx.param("region"))
        result = self.run_workflow(builder.build(), {"region": "eu"})
        self.assertEqual(result.value("a"), "eu")
        self.assertEqual(result.params, {"region": "eu"})

    def test_missing_required_parameter_stops_the_run(self) -> None:
        builder = WorkflowBuilder("flow").param("region", "string", required=True)
        builder.task("a", returns(1))
        with self.assertRaises(MissingParameterError):
            self.run_workflow(builder.build())

    def test_notes_are_carried_into_the_result(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", lambda ctx: ctx.note("did a thing"))
        self.assertEqual(self.run_workflow(builder.build())["a"].notes, ("did a thing",))


class OrderingTests(CampanileTestCase):
    def test_priority_orders_independent_tasks(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("last", returns(1), priority=5)
        builder.task("first", returns(1), priority=-5)
        builder.task("middle", returns(1))
        self.run_workflow(builder.build())
        self.assertCalls(["first#1", "middle#1", "last#1"])

    def test_declaration_order_breaks_priority_ties(self) -> None:
        builder = WorkflowBuilder("flow")
        for name in ("zeta", "alpha", "mu"):
            builder.task(name, returns(1))
        self.run_workflow(builder.build())
        self.assertCalls(["zeta#1", "alpha#1", "mu#1"])

    def test_two_runs_of_the_same_workflow_agree(self) -> None:
        workflow = diamond_workflow()
        first = Engine(clock=ManualClock(0.0)).run(workflow)
        second = Engine(clock=ManualClock(0.0)).run(workflow)
        assert first.log is not None and second.log is not None
        self.assertEqual(first.log.checksum(), second.log.checksum())


class RetryTests(CampanileTestCase):
    def test_a_flaky_task_eventually_succeeds(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("flaky", flaky_task(2), retry=3)
        result = self.run_workflow(builder.build())
        self.assertTrue(result.succeeded)
        self.assertEqual(result["flaky"].attempts, 3)
        self.assertEqual(result["flaky"].retries, 2)
        self.assertCalls(["flaky#1", "flaky#2", "flaky#3"])

    def test_a_successful_retry_clears_the_error(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("flaky", flaky_task(1), retry=2)
        result = self.run_workflow(builder.build())
        self.assertIsNone(result["flaky"].error)

    def test_exhausting_attempts_fails_the_task(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("bad", failing_task(), retry=2)
        result = self.run_workflow(builder.build())
        self.assertTrue(result.failed)
        self.assertEqual(result["bad"].attempts, 2)
        self.assertIsInstance(result["bad"].error, RuntimeError)

    def test_backoff_advances_the_clock(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task(
            "flaky",
            flaky_task(2),
            retry=RetryPolicy(max_attempts=3, delay=10.0, multiplier=2.0),
        )
        result = self.run_workflow(builder.build())
        self.assertTrue(result.succeeded)
        self.assertEqual(result.duration, 30.0)

    def test_a_retry_does_not_rerun_upstreams(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("up", returns(1))
        builder.task("flaky", flaky_task(1), after="up", retry=2)
        self.run_workflow(builder.build())
        self.assertCalls(["up#1", "flaky#1", "flaky#2"])

    def test_retry_events_are_recorded(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("flaky", flaky_task(1), retry=2)
        result = self.run_workflow(builder.build())
        self.assertEventKinds(
            result,
            "task_started",
            "task_retry_scheduled",
            "task_scheduled",
            "task_started",
            "task_succeeded",
        )

    def test_a_non_retryable_error_fails_immediately(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task(
            "bad",
            failing_task("nope", ValueError),
            retry=RetryPolicy(max_attempts=5, retry_on=("OSError",)),
        )
        result = self.run_workflow(builder.build())
        self.assertEqual(result["bad"].attempts, 1)


class FailurePropagationTests(CampanileTestCase):
    def build(self, policy: FailurePolicy):
        builder = WorkflowBuilder("flow", failure_policy=policy)
        builder.task("bad", failing_task())
        builder.task("downstream", returns(1), after="bad")
        builder.task("unrelated", returns(2))
        return builder.build()

    def test_continue_only_affects_descendants(self) -> None:
        result = self.run_workflow(self.build(FailurePolicy.CONTINUE))
        self.assertStates(
            result,
            {
                "bad": "failed",
                "downstream": "upstream_failed",
                "unrelated": "succeeded",
            },
        )
        self.assertTrue(result.failed)

    def test_isolate_reports_success_despite_the_failure(self) -> None:
        result = self.run_workflow(self.build(FailurePolicy.ISOLATE))
        self.assertTrue(result.succeeded)
        self.assertStates(result, {"bad": "failed"})

    def test_fail_fast_cancels_what_has_not_started(self) -> None:
        builder = WorkflowBuilder("flow", failure_policy=FailurePolicy.FAIL_FAST)
        builder.task("bad", failing_task())
        builder.task("later", returns(1))
        builder.task("last", returns(2), after="later")
        result = self.run_workflow(builder.build())
        self.assertTrue(result.failed)
        self.assertStates(result, {"bad": "failed", "later": "cancelled", "last": "cancelled"})

    def test_deep_failures_cascade(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", failing_task())
        builder.task("b", returns(1), after="a")
        builder.task("c", returns(2), after="b")
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"b": "upstream_failed", "c": "upstream_failed"})

    def test_all_done_still_runs_after_an_upstream_failure(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("bad", failing_task())
        builder.task("notify", returns("sent"), after="bad", trigger_rule=TriggerRule.ALL_DONE)
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"notify": "succeeded"})
        self.assertTrue(result.failed)

    def test_any_success_runs_on_a_partial_failure(self) -> None:
        builder = WorkflowBuilder("flow")
        good = builder.task("good", returns(1))
        bad = builder.task("bad", failing_task())
        join = builder.task("join", returns(2), trigger_rule=TriggerRule.ANY_SUCCESS)
        [good, bad] >> join
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"join": "succeeded"})


class ConditionTests(CampanileTestCase):
    def test_a_true_condition_runs_the_downstream(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns({"rows": 10}))
        downstream = builder.task("down", returns("ran"))
        upstream.when("results.up.rows > 0") >> downstream
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"down": "succeeded"})

    def test_a_false_condition_skips_the_downstream(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns({"rows": 0}))
        downstream = builder.task("down", returns("ran"))
        upstream.when("results.up.rows > 0") >> downstream
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"down": "skipped"})
        self.assertTrue(result.succeeded)

    def test_a_skip_cascades_to_further_downstreams(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns(0))
        middle = builder.task("middle", returns(1))
        tail = builder.task("tail", returns(2))
        upstream.when("results.up > 0") >> middle >> tail
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"middle": "skipped", "tail": "skipped"})

    def test_conditions_can_read_parameters(self) -> None:
        builder = WorkflowBuilder("flow").param("publish", "boolean", default=False)
        upstream = builder.task("up", returns(1))
        downstream = builder.task("down", returns(2))
        upstream.when("params.publish") >> downstream
        skipped = self.run_workflow(builder.build())
        self.assertStates(skipped, {"down": "skipped"})

        self.setUp()
        ran = self.run_workflow(builder.build(), {"publish": True})
        self.assertStates(ran, {"down": "succeeded"})

    def test_a_broken_condition_fails_the_run(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns({"rows": 1}))
        downstream = builder.task("down", returns(2))
        upstream.when("results.up.missing") >> downstream
        result = self.run_workflow(builder.build(), validate=False)
        self.assertTrue(result.failed)
        self.assertStates(result, {"down": "upstream_failed"})

    def test_conditions_are_evaluated_once_and_logged(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns(5))
        first = builder.task("first", returns(1))
        second = builder.task("second", returns(2))
        upstream.when("results.up > 0") >> [first, second]
        result = self.run_workflow(builder.build())
        assert result.log is not None
        evaluated = result.log.of_kind(EventKind.CONDITION_EVALUATED)
        self.assertEqual(len(evaluated), 2)

    def test_none_failed_joins_a_skipped_branch(self) -> None:
        builder = WorkflowBuilder("flow")
        upstream = builder.task("up", returns(0))
        branch = builder.task("branch", returns(1))
        always = builder.task("always", returns(2))
        upstream.when("results.up > 0") >> branch
        [branch, upstream] >> always
        builder.workflow.replace_task(
            builder.workflow.task("always").evolve(trigger_rule=TriggerRule.NONE_FAILED)
        )
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"branch": "skipped", "always": "succeeded"})


class ResourceTests(CampanileTestCase):
    def test_a_single_slot_pool_serialises_tasks(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        for name in ("a", "b", "c"):
            builder.task(name, returns(name), resources="wh")
        result = self.run_workflow(builder.build())
        self.assertTrue(result.succeeded)
        self.assertCalls(["a#1", "b#1", "c#1"])
        assert result.log is not None
        blocked = result.log.of_kind(EventKind.RESOURCE_BLOCKED)
        self.assertEqual({event.task for event in blocked}, {"b", "c"})

    def test_a_wide_pool_admits_a_whole_wave(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 4)
        for name in ("a", "b", "c"):
            builder.task(name, returns(name), resources="wh")
        result = self.run_workflow(builder.build())
        assert result.log is not None
        self.assertEqual(result.log.of_kind(EventKind.RESOURCE_BLOCKED), ())

    def test_a_multi_slot_request_excludes_others(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 2)
        builder.task("big", returns(1), resources={"wh": 2})
        builder.task("small", returns(2), resources={"wh": 1})
        result = self.run_workflow(builder.build())
        assert result.log is not None
        blocked = [event.task for event in result.log.of_kind(EventKind.RESOURCE_BLOCKED)]
        self.assertEqual(blocked, ["small"])

    def test_resources_are_released_after_each_wave(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", returns(1), resources="wh")
        builder.task("b", returns(2), after="a", resources="wh")
        self.assertTrue(self.run_workflow(builder.build()).succeeded)

    def test_resources_are_released_when_a_task_fails(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("bad", failing_task(), resources="wh")
        builder.task("good", returns(1), resources="wh")
        result = self.run_workflow(builder.build())
        self.assertStates(result, {"bad": "failed", "good": "succeeded"})

    def test_max_parallelism_narrows_a_wave(self) -> None:
        builder = WorkflowBuilder("flow", max_parallelism=1)
        for name in ("a", "b"):
            builder.task(name, returns(name))
        result = self.run_workflow(builder.build())
        assert result.log is not None
        blocked = [event.task for event in result.log.of_kind(EventKind.RESOURCE_BLOCKED)]
        self.assertEqual(blocked, ["b"])

    def test_the_executor_limit_also_narrows_a_wave(self) -> None:
        builder = WorkflowBuilder("flow")
        for name in ("a", "b", "c"):
            builder.task(name, returns(name))
        executor = RecordingExecutor(InlineExecutor(max_in_flight=2))
        result = Engine(clock=self.clock, executor=executor).run(builder.build())
        assert result.log is not None
        blocked = [event.task for event in result.log.of_kind(EventKind.RESOURCE_BLOCKED)]
        self.assertEqual(blocked, ["c"])
        self.assertTrue(result.succeeded)


class TimeoutTests(CampanileTestCase):
    def test_an_overrunning_task_fails(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("slow", slow_task(10.0), timeout="5s")
        result = self.run_workflow(builder.build())
        self.assertTrue(result.failed)
        self.assertTrue(result["slow"].timed_out)

    def test_a_timeout_is_recorded_as_an_event(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("slow", slow_task(10.0), timeout="5s")
        result = self.run_workflow(builder.build())
        self.assertEventKinds(result, "task_started", "task_timed_out", "task_failed")

    def test_a_timeout_can_be_retried(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("slow", slow_task(10.0), timeout="5s", retry=2)
        result = self.run_workflow(builder.build())
        self.assertEqual(result["slow"].attempts, 2)

    def test_a_task_inside_its_budget_is_untouched(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("quick", slow_task(1.0), timeout="5s")
        self.assertTrue(self.run_workflow(builder.build()).succeeded)


class CancellationTests(CampanileTestCase):
    def test_a_pre_cancelled_token_stops_everything(self) -> None:
        token = CancellationToken()
        token.cancel("operator")
        result = self.engine().run(linear_workflow(length=3), token=token)
        self.assertIs(result.state, RunState.CANCELLED)
        self.assertCalls([])

    def test_a_task_can_cancel_the_run(self) -> None:
        calls: list[str] = []
        builder = WorkflowBuilder("flow")
        builder.task("a", lambda ctx: ctx.cancel("nothing to do"))
        builder.task("b", counting_task(calls, "b"), after="a")
        result = self.run_workflow(builder.build())
        self.assertIs(result.state, RunState.CANCELLED)
        self.assertEqual(calls, [])
        self.assertStates(result, {"a": "succeeded", "b": "cancelled"})

    def test_cancellation_is_recorded(self) -> None:
        token = CancellationToken()
        token.cancel("operator")
        result = self.engine().run(linear_workflow(length=2), token=token)
        self.assertEventKinds(result, "run_started", "task_cancelled", "run_finished")

    def test_a_failure_still_wins_over_a_cancellation(self) -> None:
        builder = WorkflowBuilder("flow", failure_policy=FailurePolicy.FAIL_FAST)
        builder.task("bad", failing_task())
        builder.task("other", returns(1))
        self.assertTrue(self.run_workflow(builder.build()).failed)


class EventLogTests(CampanileTestCase):
    def test_the_log_replays_to_the_same_result(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", flaky_task(1), retry=2, resources="wh")
        builder.task("b", returns(2), after="a")
        builder.task("c", failing_task(), after="b")
        result = self.run_workflow(builder.build())
        snapshot = result.snapshot()

        self.assertEqual(snapshot.state, result.state)
        self.assertEqual(snapshot.states(), result.states())
        self.assertEqual(snapshot.task("a").attempts, result["a"].attempts)
        self.assertEqual(snapshot.results(), result.values())

    def test_the_log_survives_a_round_trip_through_text(self) -> None:
        result = self.run_workflow(diamond_workflow())
        assert result.log is not None
        from campanile.store.log import EventLog

        restored = EventLog.from_jsonl(result.log.to_jsonl())
        self.assertEqual(replay(restored).states(), result.states())

    def test_every_run_starts_and_finishes(self) -> None:
        result = self.run_workflow(linear_workflow())
        assert result.log is not None
        self.assertIs(result.log[0].kind, EventKind.RUN_STARTED)
        assert result.log.last() is not None
        self.assertIs(result.log.last().kind, EventKind.RUN_FINISHED)

    def test_results_appear_in_the_success_event(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns({"rows": 3}))
        result = self.run_workflow(builder.build())
        assert result.log is not None
        succeeded = result.log.of_kind(EventKind.TASK_SUCCEEDED)[0]
        self.assertEqual(succeeded.get("result"), {"rows": 3})


class ResultTests(CampanileTestCase):
    def test_value_lookup_and_errors(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("good", returns(1))
        builder.task("bad", failing_task())
        result = self.run_workflow(builder.build())
        self.assertEqual(result.value("good"), 1)
        with self.assertRaises(ExecutionError):
            result.value("bad")
        with self.assertRaises(Exception):
            result.value("ghost")

    def test_raise_for_status(self) -> None:
        ok = self.run_workflow(linear_workflow(length=1))
        self.assertIs(ok.raise_for_status(), ok)

        self.setUp()
        builder = WorkflowBuilder("flow")
        builder.task("bad", failing_task("detail"))
        with self.assertRaises(ExecutionError) as caught:
            self.run_workflow(builder.build()).raise_for_status()
        self.assertIn("detail", str(caught.exception))

    def test_counts_and_failures(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("good", returns(1))
        builder.task("bad", failing_task())
        builder.task("blocked", returns(2), after="bad")
        result = self.run_workflow(builder.build())
        counts = result.counts()
        self.assertEqual(counts["succeeded"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["upstream_failed"], 1)
        self.assertEqual([item.name for item in result.failures()], ["bad"])
        self.assertEqual(
            [item.name for item in result.in_state(TaskState.UPSTREAM_FAILED)], ["blocked"]
        )

    def test_iteration_and_membership(self) -> None:
        result = self.run_workflow(linear_workflow(length=2))
        self.assertEqual([item.name for item in result], ["t0", "t1"])
        self.assertIn("t0", result)
        self.assertEqual(len(result), 2)


class EngineTests(CampanileTestCase):
    def test_registration_and_running_by_name(self) -> None:
        engine = self.engine()
        engine.register(linear_workflow("named", length=2))
        self.assertIn("named", engine)
        self.assertTrue(engine.run("named").succeeded)

    def test_duplicate_registration_is_rejected(self) -> None:
        engine = self.engine()
        engine.register(linear_workflow("named"))
        with self.assertRaises(Exception):
            engine.register(linear_workflow("named"))

    def test_unknown_workflow(self) -> None:
        with self.assertRaises(Exception):
            self.engine().run("ghost")

    def test_plan_groups_tasks_into_waves(self) -> None:
        self.assertEqual(
            self.engine().plan(diamond_workflow()),
            [["root"], ["left", "right"], ["join"]],
        )

    def test_validation_runs_before_the_workflow(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns(1), resources="ghost")
        with self.assertRaises(ValidationError):
            self.engine().run(builder.workflow)

    def test_validation_can_be_turned_off(self) -> None:
        builder = WorkflowBuilder("flow").pool("unused", 1)
        builder.task("a", returns(1))
        self.assertTrue(self.engine(validate=False).run(builder.workflow).succeeded)

    def test_strict_mode_rejects_warnings(self) -> None:
        builder = WorkflowBuilder("flow").pool("unused", 1)
        builder.task("a", returns(1))
        with self.assertRaises(ValidationError):
            self.engine(strict=True).run(builder.workflow)

    def test_run_many_keeps_going_after_a_failure(self) -> None:
        builder = WorkflowBuilder("flow").param("fail", "boolean", default=False)

        def body(ctx):
            if ctx.param("fail"):
                raise RuntimeError("asked to fail")
            return "ok"

        builder.task("a", body)
        results = self.engine().run_many(
            builder.build(), [{"fail": False}, {"fail": True}, {"fail": False}]
        )
        self.assertEqual([item.state.value for item in results], ["succeeded", "failed", "succeeded"])

    def test_runs_are_recorded_in_the_store(self) -> None:
        store = InMemoryStore()
        engine = self.engine(store=store)
        engine.run(linear_workflow(length=1))
        engine.run(linear_workflow(length=1))
        self.assertEqual(len(store.run_ids()), 2)
        self.assertEqual(len(engine.snapshots()), 2)
        self.assertIsNotNone(engine.last_run())

    def test_run_ids_are_unique_within_an_engine(self) -> None:
        engine = self.engine()
        first = engine.run(linear_workflow(length=1))
        second = engine.run(linear_workflow(length=1))
        self.assertNotEqual(first.run_id, second.run_id)

    def test_an_explicit_run_id_is_honoured(self) -> None:
        result = self.engine().run(linear_workflow(length=1), run_id="chosen")
        self.assertEqual(result.run_id, "chosen")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
