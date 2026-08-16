"""Events, logs, stores, replay and queries."""

from __future__ import annotations

import unittest

from campanile.errors import EventLogCorrupt, StoreError, UnknownRunError
from campanile.model.state import RunState, TaskState
from campanile.store.events import Event, EventKind, kinds_of, make_event
from campanile.store.filestore import FileStore
from campanile.store.log import EventLog, merge_logs
from campanile.store.memory import InMemoryStore
from campanile.store.query import RunQuery, sort_snapshots
from campanile.store.replay import replay

from .helpers import CampanileTestCase


def build_log(run_id: str = "r1") -> EventLog:
    """A small but complete run: one task succeeds, one is blocked by it."""

    log = EventLog(run_id)
    log.append(
        EventKind.RUN_STARTED, 100.0, workflow="nightly", params={"day": "x"}, tasks=["a", "b"]
    )
    log.append(EventKind.TASK_SCHEDULED, 100.0, task="a")
    log.append(EventKind.TASK_STARTED, 100.0, task="a", attempt=1)
    log.append(EventKind.TASK_SUCCEEDED, 102.0, task="a", attempt=1, result={"rows": 5}, elapsed=2.0)
    log.append(EventKind.TASK_SKIPPED, 102.0, task="b", reason="condition false")
    log.append(EventKind.RUN_FINISHED, 102.0, state="succeeded")
    return log


class EventTests(unittest.TestCase):
    def test_make_event_drops_none_payload_entries(self) -> None:
        event = make_event(0, 1.0, EventKind.TASK_STARTED, "r", task="a", note=None, kept=1)
        self.assertEqual(event.payload, {"kept": 1})

    def test_round_trips_through_as_dict(self) -> None:
        event = make_event(3, 1.5, EventKind.TASK_FAILED, "r", task="a", attempt=2, error={"x": 1})
        self.assertEqual(Event.from_dict(event.as_dict()), event)

    def test_optional_fields_are_omitted(self) -> None:
        event = make_event(0, 1.0, EventKind.RUN_STARTED, "r")
        self.assertNotIn("task", event.as_dict())
        self.assertNotIn("attempt", event.as_dict())
        self.assertNotIn("payload", event.as_dict())

    def test_classification(self) -> None:
        started = make_event(0, 1.0, EventKind.TASK_STARTED, "r", task="a")
        finished = make_event(1, 1.0, EventKind.TASK_SUCCEEDED, "r", task="a")
        run = make_event(2, 1.0, EventKind.RUN_FINISHED, "r")
        self.assertTrue(started.is_task_event)
        self.assertFalse(started.is_terminal)
        self.assertTrue(finished.is_terminal)
        self.assertFalse(run.is_task_event)

    def test_sequence_and_attempt_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Event(seq=-1, at=0.0, kind=EventKind.RUN_STARTED, run_id="r")
        with self.assertRaises(ValueError):
            Event(seq=0, at=0.0, kind=EventKind.TASK_STARTED, run_id="r", attempt=0)

    def test_from_dict_rejects_malformed_events(self) -> None:
        base = {"seq": 0, "at": 1.0, "kind": "run_started", "run_id": "r"}
        for broken in (
            {**base, "kind": "nonsense"},
            {**base, "seq": "0"},
            {**base, "at": "now"},
            {**base, "attempt": "2"},
            {**base, "payload": [1]},
            {"at": 1.0, "kind": "run_started", "run_id": "r"},
        ):
            with self.subTest(broken=broken), self.assertRaises(EventLogCorrupt):
                Event.from_dict(broken)

    def test_describe_mentions_task_and_attempt(self) -> None:
        event = make_event(4, 100.0, EventKind.TASK_STARTED, "r", task="a", attempt=2)
        described = event.describe()
        self.assertIn("#0004", described)
        self.assertIn("a#2", described)


class EventLogTests(unittest.TestCase):
    def test_sequence_numbers_are_dense(self) -> None:
        log = build_log()
        self.assertEqual([event.seq for event in log], list(range(len(log))))

    def test_filtering(self) -> None:
        log = build_log()
        self.assertEqual(len(log.for_task("a")), 3)
        self.assertEqual(len(log.of_kind(EventKind.TASK_SUCCEEDED)), 1)
        self.assertEqual(log.tasks(), ("a", "b"))
        self.assertEqual(log.count(EventKind.TASK_STARTED), 1)

    def test_span_and_endpoints(self) -> None:
        log = build_log()
        self.assertEqual(log.span(), (100.0, 102.0))
        assert log.first() is not None and log.last() is not None
        self.assertIs(log.first().kind, EventKind.RUN_STARTED)
        self.assertIs(log.last().kind, EventKind.RUN_FINISHED)
        self.assertIsNone(EventLog("empty").span())

    def test_since(self) -> None:
        log = build_log()
        self.assertEqual(len(log.since(4)), 2)

    def test_listeners_see_appended_events(self) -> None:
        log = EventLog("r")
        seen: list[str] = []
        unsubscribe = log.subscribe(lambda event: seen.append(event.kind.value))
        log.append(EventKind.RUN_STARTED, 0.0)
        unsubscribe()
        log.append(EventKind.RUN_FINISHED, 1.0)
        self.assertEqual(seen, ["run_started"])

    def test_jsonl_round_trip_preserves_the_checksum(self) -> None:
        log = build_log()
        restored = EventLog.from_jsonl(log.to_jsonl())
        self.assertEqual(restored.checksum(), log.checksum())
        self.assertEqual(len(restored), len(log))

    def test_checksum_changes_with_content(self) -> None:
        first = build_log()
        second = build_log()
        second.append(EventKind.TASK_CANCELLED, 103.0, task="b")
        self.assertNotEqual(first.checksum(), second.checksum())

    def test_extending_out_of_order_is_rejected(self) -> None:
        log = EventLog("r")
        with self.assertRaises(EventLogCorrupt):
            log.extend_one(make_event(3, 0.0, EventKind.RUN_STARTED, "r"))

    def test_events_from_another_run_are_rejected(self) -> None:
        log = EventLog("r")
        with self.assertRaises(EventLogCorrupt):
            log.extend_one(make_event(0, 0.0, EventKind.RUN_STARTED, "other"))

    def test_a_gap_in_a_stored_log_is_caught(self) -> None:
        log = build_log()
        lines = log.to_jsonl().splitlines()
        broken = "\n".join(lines[:2] + lines[3:])
        with self.assertRaises(EventLogCorrupt):
            EventLog.from_jsonl(broken)

    def test_empty_jsonl_needs_a_run_id(self) -> None:
        with self.assertRaises(EventLogCorrupt):
            EventLog.from_jsonl("")
        self.assertEqual(len(EventLog.from_jsonl("", run_id="r")), 0)

    def test_merge_logs_is_totally_ordered(self) -> None:
        first = build_log("r1")
        second = build_log("r2")
        merged = merge_logs([second, first])
        keys = [(event.at, event.run_id, event.seq) for event in merged]
        self.assertEqual(keys, sorted(keys))

    def test_kinds_of(self) -> None:
        self.assertEqual(kinds_of(build_log())[0], EventKind.RUN_STARTED)


class ReplayTests(unittest.TestCase):
    def test_rebuilds_the_run(self) -> None:
        snapshot = replay(build_log())
        self.assertEqual(snapshot.run_id, "r1")
        self.assertEqual(snapshot.workflow, "nightly")
        self.assertIs(snapshot.state, RunState.SUCCEEDED)
        self.assertEqual(snapshot.params, {"day": "x"})
        self.assertEqual(snapshot.duration, 2.0)

    def test_task_detail(self) -> None:
        snapshot = replay(build_log())
        task = snapshot.task("a")
        self.assertIs(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.result, {"rows": 5})
        self.assertEqual(task.duration, 2.0)
        self.assertEqual(snapshot.task("b").skip_reason, "condition false")

    def test_results_and_counts(self) -> None:
        snapshot = replay(build_log())
        self.assertEqual(snapshot.results(), {"a": {"rows": 5}})
        self.assertEqual(snapshot.counts()["skipped"], 1)
        self.assertEqual(snapshot.total_attempts(), 1)

    def test_retries_are_counted(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(EventKind.TASK_SCHEDULED, 0.0, task="a")
        log.append(EventKind.TASK_STARTED, 0.0, task="a", attempt=1)
        log.append(EventKind.TASK_RETRY_SCHEDULED, 1.0, task="a", attempt=1, error={"message": "x"})
        log.append(EventKind.TASK_SCHEDULED, 2.0, task="a")
        log.append(EventKind.TASK_STARTED, 2.0, task="a", attempt=2)
        log.append(EventKind.TASK_SUCCEEDED, 3.0, task="a", attempt=2, result=1)
        log.append(EventKind.RUN_FINISHED, 3.0, state="succeeded")
        snapshot = replay(log)
        self.assertEqual(snapshot.task("a").attempts, 2)
        self.assertEqual(snapshot.task("a").retries, 1)

    def test_conditions_are_recorded(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(
            EventKind.CONDITION_EVALUATED, 0.0, task="b", upstream="a",
            condition="results.a > 0", result=False,
        )
        log.append(EventKind.TASK_SKIPPED, 0.0, task="b", reason="condition false")
        log.append(EventKind.RUN_FINISHED, 0.0, state="succeeded")
        snapshot = replay(log)
        self.assertEqual(len(snapshot.conditions), 1)
        self.assertFalse(snapshot.conditions[0]["result"])

    def test_resource_waits_are_counted(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(EventKind.RESOURCE_BLOCKED, 0.0, task="a", pools=["wh"])
        log.append(EventKind.TASK_SCHEDULED, 0.0, task="a")
        log.append(EventKind.TASK_STARTED, 0.0, task="a", attempt=1)
        log.append(EventKind.TASK_SUCCEEDED, 0.0, task="a", attempt=1, result=1)
        log.append(EventKind.RUN_FINISHED, 0.0, state="succeeded")
        self.assertEqual(replay(log).task("a").resource_waits, 1)

    def test_empty_stream_is_rejected(self) -> None:
        with self.assertRaises(EventLogCorrupt):
            replay([])

    def test_out_of_order_sequence_is_rejected(self) -> None:
        events = list(build_log())
        with self.assertRaises(EventLogCorrupt):
            replay([events[0], events[2]])

    def test_mixed_run_ids_are_rejected(self) -> None:
        first = list(build_log("r1"))
        stray = make_event(1, 1.0, EventKind.RUN_FINISHED, "r2")
        with self.assertRaises(EventLogCorrupt):
            replay([first[0], stray])

    def test_success_without_a_start_is_rejected(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(EventKind.TASK_SUCCEEDED, 1.0, task="a", attempt=1, result=1)
        with self.assertRaises(EventLogCorrupt):
            replay(log)

    def test_attempt_numbers_must_increase_by_one(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(EventKind.TASK_STARTED, 0.0, task="a", attempt=3)
        with self.assertRaises(EventLogCorrupt):
            replay(log)

    def test_unfinished_run_reports_running(self) -> None:
        log = EventLog("r")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        log.append(EventKind.TASK_SCHEDULED, 0.0, task="a")
        self.assertIs(replay(log).state, RunState.RUNNING)


class InMemoryStoreTests(unittest.TestCase):
    def test_create_and_lookup(self) -> None:
        store = InMemoryStore()
        log = store.create("r1")
        self.assertIs(store.log("r1"), log)
        self.assertTrue(store.has("r1"))
        self.assertEqual(store.run_ids(), ("r1",))

    def test_duplicate_creation_is_rejected(self) -> None:
        store = InMemoryStore()
        store.create("r1")
        with self.assertRaises(StoreError):
            store.create("r1")

    def test_unknown_run(self) -> None:
        with self.assertRaises(UnknownRunError):
            InMemoryStore().log("ghost")

    def test_adopt_and_snapshot(self) -> None:
        store = InMemoryStore()
        store.adopt(build_log("r1"))
        self.assertIs(store.snapshot("r1").state, RunState.SUCCEEDED)
        self.assertEqual(len(store.snapshots()), 1)

    def test_ingest_interleaved_runs(self) -> None:
        store = InMemoryStore()
        first = list(build_log("r1"))
        second = list(build_log("r2"))
        interleaved = [value for pair in zip(first, second) for value in pair]
        store.ingest(interleaved)
        self.assertEqual(set(store.run_ids()), {"r1", "r2"})
        self.assertEqual(len(store.log("r1")), len(first))

    def test_delete_and_clear(self) -> None:
        store = InMemoryStore()
        store.create("r1")
        store.delete("r1")
        self.assertFalse(store.has("r1"))
        with self.assertRaises(UnknownRunError):
            store.delete("r1")
        store.create("r2")
        store.clear()
        self.assertEqual(store.run_ids(), ())

    def test_an_empty_store_is_falsy_by_length_but_still_usable(self) -> None:
        store = InMemoryStore()
        self.assertEqual(len(store), 0)
        self.assertIsNotNone(store.create("r1"))


class FileStoreTests(CampanileTestCase):
    def test_write_and_read_back(self) -> None:
        root = self.temp_dir()
        store = FileStore(root)
        log = store.create("r1")
        for event in build_log("r1"):
            if event.seq == 0:
                log.append(
                    event.kind, event.at, workflow="nightly", params={"day": "x"}, tasks=["a", "b"]
                )
            else:
                log.append(event.kind, event.at, task=event.task, attempt=event.attempt, **event.payload)
        store.flush("r1")
        store.evict()

        reloaded = FileStore(root)
        self.assertEqual(reloaded.run_ids(), ("r1",))
        self.assertIs(reloaded.snapshot("r1").state, RunState.SUCCEEDED)

    def test_flush_is_incremental(self) -> None:
        root = self.temp_dir()
        store = FileStore(root)
        log = store.create("r1")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        store.flush("r1")
        first = (root / "r1.jsonl").read_text().count("\n")
        log.append(EventKind.RUN_FINISHED, 1.0, state="succeeded")
        store.flush("r1")
        second = (root / "r1.jsonl").read_text().count("\n")
        self.assertEqual((first, second), (1, 2))

    def test_flushing_twice_writes_nothing_new(self) -> None:
        root = self.temp_dir()
        store = FileStore(root)
        log = store.create("r1")
        log.append(EventKind.RUN_STARTED, 0.0, workflow="w", tasks=["a"])
        store.flush("r1")
        before = (root / "r1.jsonl").read_text()
        store.flush("r1")
        self.assertEqual((root / "r1.jsonl").read_text(), before)

    def test_duplicate_creation_is_rejected(self) -> None:
        store = FileStore(self.temp_dir())
        store.create("r1")
        with self.assertRaises(StoreError):
            store.create("r1")

    def test_unknown_run(self) -> None:
        with self.assertRaises(UnknownRunError):
            FileStore(self.temp_dir()).log("ghost")

    def test_dangerous_run_ids_are_rejected(self) -> None:
        store = FileStore(self.temp_dir())
        with self.assertRaises(StoreError):
            store.create("../escape")

    def test_write_replaces_atomically(self) -> None:
        root = self.temp_dir()
        store = FileStore(root)
        store.write(build_log("r1"))
        self.assertEqual(len(store.log("r1")), 6)
        self.assertFalse(list(root.glob("*.tmp")))

    def test_verify_detects_corruption(self) -> None:
        root = self.temp_dir()
        store = FileStore(root)
        store.write(build_log("r1"))
        store.verify("r1")
        (root / "r1.jsonl").write_text("not json\n")
        store.evict()
        with self.assertRaises(EventLogCorrupt):
            store.verify("r1")

    def test_delete(self) -> None:
        store = FileStore(self.temp_dir())
        store.write(build_log("r1"))
        store.delete("r1")
        self.assertFalse(store.has("r1"))
        with self.assertRaises(UnknownRunError):
            store.delete("r1")

    def test_run_ids_are_sorted(self) -> None:
        store = FileStore(self.temp_dir())
        store.write(build_log("b"))
        store.write(build_log("a"))
        self.assertEqual(store.run_ids(), ("a", "b"))

    def test_missing_directory_without_create(self) -> None:
        with self.assertRaises(StoreError):
            FileStore(self.temp_dir() / "nope", create=False)


class QueryTests(unittest.TestCase):
    def snapshots(self):
        first = replay(build_log("r1"))
        second = replay(build_log("r2"))
        second.workflow = "other"
        second.state = RunState.FAILED
        second.started_at = 200.0
        second.finished_at = 210.0
        return [first, second]

    def test_filter_by_workflow(self) -> None:
        matched = RunQuery(workflow="nightly").apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r1"])

    def test_failed_only(self) -> None:
        matched = RunQuery(failed_only=True).apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r2"])

    def test_filter_by_state(self) -> None:
        matched = RunQuery().in_state(RunState.FAILED).apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r2"])

    def test_time_window(self) -> None:
        matched = RunQuery(since=150.0).apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r2"])
        matched = RunQuery(until=150.0).apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r1"])

    def test_min_duration(self) -> None:
        matched = RunQuery(min_duration=5.0).apply(self.snapshots())
        self.assertEqual([item.run_id for item in matched], ["r2"])

    def test_contains_task_and_task_state(self) -> None:
        self.assertEqual(len(RunQuery(contains_task="a").apply(self.snapshots())), 2)
        self.assertEqual(len(RunQuery(contains_task="zz").apply(self.snapshots())), 0)
        self.assertEqual(
            len(RunQuery(task_state=TaskState.SKIPPED).apply(self.snapshots())), 2
        )

    def test_custom_predicate(self) -> None:
        query = RunQuery().where(lambda snapshot: snapshot.run_id.endswith("2"))
        self.assertEqual([item.run_id for item in query.apply(self.snapshots())], ["r2"])

    def test_limit_and_sort(self) -> None:
        query = RunQuery(sort_by="duration", descending=True, limit=1)
        self.assertEqual([item.run_id for item in query.apply(self.snapshots())], ["r2"])

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(StoreError):
            RunQuery(sort_by="colour")
        with self.assertRaises(StoreError):
            RunQuery(limit=0)
        with self.assertRaises(StoreError):
            RunQuery(since=10.0, until=1.0)

    def test_sort_puts_missing_values_last_in_both_directions(self) -> None:
        snapshots = self.snapshots()
        snapshots[0].finished_at = None
        ascending = sort_snapshots(snapshots, "duration")
        descending = sort_snapshots(snapshots, "duration", descending=True)
        self.assertEqual(ascending[-1].run_id, "r1")
        self.assertEqual(descending[-1].run_id, "r1")

    def test_sort_rejects_unknown_keys(self) -> None:
        with self.assertRaises(StoreError):
            sort_snapshots([], "colour")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
