"""State machines, retry policies and trigger rules."""

from __future__ import annotations

import unittest

from campanile.errors import ConfigurationError, StateTransitionError
from campanile.model.policy import (
    Backoff,
    FailurePolicy,
    RetryPolicy,
    TriggerDecision,
    TriggerRule,
    evaluate_trigger_rule,
)
from campanile.model.state import (
    TASK_TRANSITIONS,
    RunState,
    TaskState,
    check_run_transition,
    check_task_transition,
    derive_run_state,
    summarise_states,
)


class TaskStateTests(unittest.TestCase):
    def test_terminal_states(self) -> None:
        terminal = {state for state in TaskState if state.is_terminal}
        self.assertEqual(
            terminal,
            {
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.SKIPPED,
                TaskState.UPSTREAM_FAILED,
                TaskState.CANCELLED,
            },
        )

    def test_only_failed_counts_as_a_failure(self) -> None:
        self.assertTrue(TaskState.FAILED.is_failure)
        self.assertFalse(TaskState.UPSTREAM_FAILED.is_failure)
        self.assertFalse(TaskState.SKIPPED.is_failure)

    def test_terminal_states_have_no_successors(self) -> None:
        for state in TaskState:
            if state.is_terminal:
                self.assertEqual(TASK_TRANSITIONS[state], frozenset())


class TransitionTests(unittest.TestCase):
    def test_normal_path(self) -> None:
        state = TaskState.PENDING
        for target in (TaskState.READY, TaskState.RUNNING, TaskState.SUCCEEDED):
            state = check_task_transition("t", state, target)
        self.assertIs(state, TaskState.SUCCEEDED)

    def test_retry_path(self) -> None:
        state = check_task_transition("t", TaskState.RUNNING, TaskState.RETRY_WAIT)
        state = check_task_transition("t", state, TaskState.READY)
        self.assertIs(state, TaskState.READY)

    def test_self_transition_is_allowed(self) -> None:
        self.assertIs(
            check_task_transition("t", TaskState.SUCCEEDED, TaskState.SUCCEEDED),
            TaskState.SUCCEEDED,
        )

    def test_illegal_transitions_raise(self) -> None:
        cases = [
            (TaskState.PENDING, TaskState.RUNNING),
            (TaskState.PENDING, TaskState.SUCCEEDED),
            (TaskState.SUCCEEDED, TaskState.RUNNING),
            (TaskState.READY, TaskState.SUCCEEDED),
            (TaskState.RETRY_WAIT, TaskState.RUNNING),
        ]
        for current, target in cases:
            with self.subTest(current=current, target=target):
                with self.assertRaises(StateTransitionError):
                    check_task_transition("t", current, target)

    def test_run_transitions(self) -> None:
        state = check_run_transition("r", RunState.PENDING, RunState.RUNNING)
        self.assertIs(check_run_transition("r", state, RunState.FAILED), RunState.FAILED)
        with self.assertRaises(StateTransitionError):
            check_run_transition("r", RunState.SUCCEEDED, RunState.RUNNING)


class DeriveRunStateTests(unittest.TestCase):
    def test_empty_run_succeeds(self) -> None:
        self.assertIs(derive_run_state([]), RunState.SUCCEEDED)

    def test_all_succeeded(self) -> None:
        self.assertIs(
            derive_run_state([TaskState.SUCCEEDED, TaskState.SKIPPED]), RunState.SUCCEEDED
        )

    def test_any_non_terminal_means_running(self) -> None:
        self.assertIs(
            derive_run_state([TaskState.SUCCEEDED, TaskState.RUNNING]), RunState.RUNNING
        )

    def test_failure_beats_cancellation(self) -> None:
        self.assertIs(
            derive_run_state([TaskState.FAILED, TaskState.CANCELLED]), RunState.FAILED
        )

    def test_upstream_failure_fails_the_run(self) -> None:
        self.assertIs(
            derive_run_state([TaskState.SUCCEEDED, TaskState.UPSTREAM_FAILED]),
            RunState.FAILED,
        )

    def test_cancellation_alone(self) -> None:
        self.assertIs(
            derive_run_state([TaskState.SUCCEEDED, TaskState.CANCELLED]),
            RunState.CANCELLED,
        )

    def test_summarise_includes_zero_counts(self) -> None:
        counts = summarise_states([TaskState.SUCCEEDED, TaskState.SUCCEEDED])
        self.assertEqual(counts["succeeded"], 2)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(len(counts), len(TaskState))


class RetryPolicyTests(unittest.TestCase):
    def test_default_is_no_retry(self) -> None:
        policy = RetryPolicy()
        self.assertEqual(policy.max_attempts, 1)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.retries, 0)
        self.assertEqual(policy.schedule(), [])

    def test_exponential_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=4, delay=1.0, multiplier=2.0)
        self.assertEqual(policy.schedule(), [1.0, 2.0, 4.0])

    def test_linear_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=4, backoff=Backoff.LINEAR, delay=2.0)
        self.assertEqual(policy.schedule(), [2.0, 4.0, 6.0])

    def test_fixed_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff=Backoff.FIXED, delay=5.0)
        self.assertEqual(policy.schedule(), [5.0, 5.0])

    def test_max_delay_clamps(self) -> None:
        policy = RetryPolicy(max_attempts=5, delay=1.0, multiplier=10.0, max_delay=20.0)
        self.assertEqual(policy.schedule(), [1.0, 10.0, 20.0, 20.0])

    def test_jitter_is_deterministic_and_bounded(self) -> None:
        policy = RetryPolicy(max_attempts=3, delay=10.0, jitter=0.5)
        first = policy.delay_for(1, key="task")
        second = policy.delay_for(1, key="task")
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 5.0)
        self.assertLessEqual(first, 10.0)

    def test_jitter_differs_between_keys(self) -> None:
        policy = RetryPolicy(max_attempts=2, delay=10.0, jitter=0.9)
        self.assertNotEqual(policy.delay_for(1, key="a"), policy.delay_for(1, key="b"))

    def test_should_retry_respects_attempt_count(self) -> None:
        policy = RetryPolicy(max_attempts=2)
        self.assertTrue(policy.should_retry(1))
        self.assertFalse(policy.should_retry(2))

    def test_retry_on_acts_as_an_allow_list(self) -> None:
        policy = RetryPolicy(max_attempts=3, retry_on=("OSError",))
        self.assertTrue(policy.should_retry(1, OSError("x")))
        self.assertFalse(policy.should_retry(1, ValueError("x")))

    def test_retry_on_matches_real_class_names_not_aliases(self) -> None:
        # ``IOError`` is a Python alias for ``OSError``; the class raised is an
        # OSError and that is the name the policy sees.
        policy = RetryPolicy(max_attempts=3, retry_on=("IOError",))
        self.assertFalse(policy.should_retry(1, IOError("x")))

    def test_retry_on_matches_base_classes(self) -> None:
        policy = RetryPolicy(max_attempts=3, retry_on=("Exception",))
        self.assertTrue(policy.should_retry(1, ValueError("x")))

    def test_attempt_numbering_starts_at_one(self) -> None:
        policy = RetryPolicy(max_attempts=2)
        with self.assertRaises(ValueError):
            policy.should_retry(0)
        with self.assertRaises(ValueError):
            policy.delay_for(0)

    def test_invalid_configurations(self) -> None:
        for kwargs in (
            {"max_attempts": 0},
            {"delay": -1.0},
            {"multiplier": 0.0},
            {"max_delay": -5.0},
            {"jitter": 1.5},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                RetryPolicy(**kwargs)  # type: ignore[arg-type]

    def test_of_accepts_loose_forms(self) -> None:
        policy = RetryPolicy.of(3, backoff="linear", delay="10s", max_delay="1m")
        self.assertEqual(policy.backoff, Backoff.LINEAR)
        self.assertEqual(policy.delay, 10.0)
        self.assertEqual(policy.max_delay, 60.0)

    def test_from_dict_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ConfigurationError):
            RetryPolicy.from_dict({"max_attempts": 2, "nonsense": 1})

    def test_round_trips_through_as_dict(self) -> None:
        policy = RetryPolicy.of(3, backoff="fixed", delay=2.0)
        self.assertEqual(RetryPolicy.from_dict(policy.as_dict()), policy)


class TriggerRuleTests(unittest.TestCase):
    S = TaskState.SUCCEEDED
    F = TaskState.FAILED
    R = TaskState.RUNNING
    K = TaskState.SKIPPED
    U = TaskState.UPSTREAM_FAILED

    def decide(self, rule: TriggerRule, states: list[TaskState]) -> TriggerDecision:
        return evaluate_trigger_rule(rule, states)

    def test_no_upstreams_always_runs(self) -> None:
        for rule in TriggerRule:
            with self.subTest(rule=rule):
                self.assertIs(self.decide(rule, []), TriggerDecision.RUN)

    def test_all_success(self) -> None:
        rule = TriggerRule.ALL_SUCCESS
        self.assertIs(self.decide(rule, [self.S, self.S]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.S, self.R]), TriggerDecision.WAIT)
        self.assertIs(self.decide(rule, [self.S, self.F]), TriggerDecision.SKIP)
        self.assertIs(self.decide(rule, [self.S, self.K]), TriggerDecision.SKIP)

    def test_all_done_accepts_anything_terminal(self) -> None:
        rule = TriggerRule.ALL_DONE
        self.assertIs(self.decide(rule, [self.S, self.F, self.K]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.S, self.R]), TriggerDecision.WAIT)

    def test_none_failed_tolerates_skips(self) -> None:
        rule = TriggerRule.NONE_FAILED
        self.assertIs(self.decide(rule, [self.S, self.K]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.S, self.F]), TriggerDecision.SKIP)
        self.assertIs(self.decide(rule, [self.S, self.R]), TriggerDecision.WAIT)

    def test_any_success_runs_as_soon_as_one_succeeds(self) -> None:
        rule = TriggerRule.ANY_SUCCESS
        self.assertIs(self.decide(rule, [self.S, self.R]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.F, self.R]), TriggerDecision.WAIT)
        self.assertIs(self.decide(rule, [self.F, self.K]), TriggerDecision.SKIP)

    def test_any_failed(self) -> None:
        rule = TriggerRule.ANY_FAILED
        self.assertIs(self.decide(rule, [self.S, self.F]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.S, self.S]), TriggerDecision.SKIP)
        self.assertIs(self.decide(rule, [self.S, self.R]), TriggerDecision.WAIT)

    def test_all_failed(self) -> None:
        rule = TriggerRule.ALL_FAILED
        self.assertIs(self.decide(rule, [self.F, self.F]), TriggerDecision.RUN)
        self.assertIs(self.decide(rule, [self.F, self.S]), TriggerDecision.SKIP)
        self.assertIs(self.decide(rule, [self.F, self.R]), TriggerDecision.WAIT)
        self.assertIs(self.decide(rule, [self.F, self.K]), TriggerDecision.SKIP)

    def test_always_ignores_upstreams(self) -> None:
        self.assertIs(
            self.decide(TriggerRule.ALWAYS, [self.R, self.F]), TriggerDecision.RUN
        )

    def test_blocked_upstreams_do_not_satisfy_all_success(self) -> None:
        self.assertIs(
            self.decide(TriggerRule.ALL_SUCCESS, [self.U]), TriggerDecision.SKIP
        )


class FailurePolicyTests(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(
            {policy.value for policy in FailurePolicy},
            {"continue", "fail_fast", "isolate"},
        )

    def test_parses_from_string(self) -> None:
        self.assertIs(FailurePolicy("fail_fast"), FailurePolicy.FAIL_FAST)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
