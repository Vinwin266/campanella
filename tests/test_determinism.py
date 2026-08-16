"""The property the whole design exists to protect.

Given the same workflow, the same parameters and a manual clock, two runs must
produce byte-identical event logs -- same order, same timestamps, same payloads.
These tests run the same thing twice and compare, which is the only way to catch
an accidental dependency on hash order, insertion order or the wall clock.
"""

from __future__ import annotations

import unittest

from campanile.dsl.builder import WorkflowBuilder
from campanile.model.policy import FailurePolicy, RetryPolicy, TriggerRule
from campanile.runtime.engine import Engine
from campanile.store.replay import replay
from campanile.util.clock import ManualClock

from .helpers import ORIGIN, failing_task, flaky_task, returns, slow_task


def elaborate_workflow():
    """One workflow exercising retries, pools, conditions, skips and failures."""

    builder = (
        WorkflowBuilder("elaborate", failure_policy=FailurePolicy.CONTINUE)
        .param("region", "string", default="eu", choices=["eu", "us"])
        .param("publish", "boolean", default=False)
        .pool("warehouse", 2)
        .pool("licence", 1)
    )

    extract = builder.task(
        "extract", slow_task(2.0, {"rows": 40}), resources="warehouse", priority=-1
    )
    dedupe = builder.task(
        "dedupe", flaky_task(2, {"rows": 36}), retry=RetryPolicy(max_attempts=3, delay=5.0),
        resources={"warehouse": 2},
    )
    enrich = builder.task("enrich", slow_task(1.0, {"rows": 36}), resources="licence")
    audit = builder.task("audit", failing_task("audit is down"))
    rollup = builder.task("rollup", slow_task(0.5, {"rows": 12}))
    publish = builder.task("publish", returns("published"))
    notify = builder.task("notify", returns("sent"), trigger_rule=TriggerRule.ALL_DONE)

    extract >> [dedupe, enrich] >> rollup
    extract >> audit
    rollup.when("results.rollup.rows > 0 and params.publish") >> publish
    [rollup, audit, publish] >> notify
    return builder.build()


def run_once(params=None):
    """Build the workflow afresh and run it on a clean engine.

    The definition is rebuilt each time on purpose.  Task bodies are ordinary
    Python closures and a test body that counts its own attempts would otherwise
    carry that count from one run into the next -- which would be the *test*
    being non-deterministic, not the engine.
    """

    engine = Engine(clock=ManualClock(ORIGIN))
    return engine.run(elaborate_workflow(), params)


class DeterminismTests(unittest.TestCase):
    def test_event_logs_are_byte_identical(self) -> None:
        first, second = run_once(), run_once()
        assert first.log is not None and second.log is not None
        self.assertEqual(first.log.to_jsonl(), second.log.to_jsonl())

    def test_checksums_match(self) -> None:
        first, second = run_once(), run_once()
        assert first.log is not None and second.log is not None
        self.assertEqual(first.log.checksum(), second.log.checksum())

    def test_run_ids_match(self) -> None:
        self.assertEqual(run_once().run_id, run_once().run_id)

    def test_states_and_timings_match(self) -> None:
        first, second = run_once(), run_once()
        self.assertEqual(first.states(), second.states())
        self.assertEqual(first.duration, second.duration)
        for name in first.states():
            self.assertEqual(first[name].duration, second[name].duration, name)

    def test_different_parameters_change_the_outcome(self) -> None:
        default = run_once()
        published = run_once({"publish": True})
        assert default.log is not None and published.log is not None
        self.assertNotEqual(default.log.checksum(), published.log.checksum())
        self.assertEqual(str(default["publish"].state), "skipped")
        self.assertEqual(str(published["publish"].state), "succeeded")

    def test_the_run_exercises_everything_it_claims_to(self) -> None:
        result = run_once()
        states = {name: str(state) for name, state in result.states().items()}
        self.assertEqual(states["extract"], "succeeded")
        self.assertEqual(states["dedupe"], "succeeded")
        self.assertEqual(states["audit"], "failed")
        self.assertEqual(states["publish"], "skipped")
        self.assertEqual(states["notify"], "succeeded")
        self.assertEqual(result["dedupe"].attempts, 3)
        self.assertTrue(result.failed)

    def test_the_replayed_snapshot_agrees_with_the_result(self) -> None:
        result = run_once()
        assert result.log is not None
        snapshot = replay(result.log)
        self.assertEqual(snapshot.state, result.state)
        self.assertEqual(snapshot.states(), result.states())
        self.assertEqual(snapshot.total_attempts(), result.total_attempts())
        self.assertEqual(snapshot.results(), result.values())

    def test_nothing_reads_the_host_clock(self) -> None:
        """Every timestamp must come from the manual clock, not ``time.time``."""

        result = run_once()
        assert result.log is not None
        for event in result.log:
            self.assertGreaterEqual(event.at, ORIGIN)
            self.assertLess(event.at, ORIGIN + 3600.0)

    def test_timestamps_never_go_backwards(self) -> None:
        result = run_once()
        assert result.log is not None
        moments = [event.at for event in result.log]
        self.assertEqual(moments, sorted(moments))

    def test_task_order_is_stable_across_runs(self) -> None:
        first, second = run_once(), run_once()
        assert first.log is not None and second.log is not None
        self.assertEqual(
            [event.task for event in first.log],
            [event.task for event in second.log],
        )


class ReproducibilityAcrossStoresTests(unittest.TestCase):
    def test_a_run_replayed_from_disk_matches_the_original(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        from campanile.store.filestore import FileStore

        root = Path(tempfile.mkdtemp(prefix="campanile-determinism-"))
        try:
            store = FileStore(root)
            engine = Engine(clock=ManualClock(ORIGIN), store=store)
            result = engine.run(elaborate_workflow())
            store.flush_all()
            store.evict()

            reloaded = FileStore(root)
            snapshot = reloaded.snapshot(result.run_id)
            self.assertEqual(snapshot.state, result.state)
            self.assertEqual(snapshot.states(), result.states())
            self.assertEqual(reloaded.log(result.run_id).checksum(), result.log.checksum())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
