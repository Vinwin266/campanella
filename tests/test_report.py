"""Tables, metrics, summaries, timelines and exports."""

from __future__ import annotations

import csv
import io
import json
import unittest

from campanile.dsl.builder import WorkflowBuilder
from campanile.report.export import (
    RUN_COLUMNS,
    TASK_COLUMNS,
    export_events_jsonl,
    export_run_json,
    export_runs_csv,
    export_runs_json,
    export_tasks_csv,
)
from campanile.report.metrics import Counter, Histogram, aggregate, collect, state_shares
from campanile.report.render import Table, render_bars, render_kv
from campanile.report.summary import RunSummary, summarise, summarise_many
from campanile.report.timeline import Timeline, critical_path, render_timeline
from campanile.util.text import bar, columnize, join_human, pluralize, truncate, wrap

from .helpers import CampanileTestCase, failing_task, flaky_task, returns, slow_task


def sample_workflow():
    builder = WorkflowBuilder("etl").pool("wh", 1)
    builder.task("extract", slow_task(2.0, {"rows": 5}), resources="wh")
    builder.task("transform", flaky_task(1, {"rows": 4}), after="extract", retry=2, resources="wh")
    builder.task("load", slow_task(1.0, {"rows": 4}), after="transform")
    handle = builder.handle("load")
    tail = builder.task("archive", returns("archived"))
    handle.when("results.load.rows > 100") >> tail
    return builder.build()


class TextHelperTests(unittest.TestCase):
    def test_truncate(self) -> None:
        self.assertEqual(truncate("orchestration", 8), "orche...")
        self.assertEqual(truncate("short", 8), "short")
        self.assertEqual(truncate("abc", 2), "ab")
        self.assertEqual(truncate("abc", 0), "")

    def test_pluralize(self) -> None:
        self.assertEqual(pluralize(1, "task"), "1 task")
        self.assertEqual(pluralize(2, "task"), "2 tasks")
        self.assertEqual(pluralize(0, "entry", "entries"), "0 entries")

    def test_join_human(self) -> None:
        self.assertEqual(join_human([]), "")
        self.assertEqual(join_human(["a"]), "a")
        self.assertEqual(join_human(["a", "b"]), "a and b")
        self.assertEqual(join_human(["a", "b", "c"]), "a, b and c")

    def test_wrap(self) -> None:
        self.assertEqual(wrap("one two three", 7), ["one two", "three"])
        self.assertEqual(wrap("", 10), [""])
        with self.assertRaises(ValueError):
            wrap("x", 0)

    def test_bar(self) -> None:
        self.assertEqual(bar(0.5, 10), "#####.....")
        self.assertEqual(bar(-1.0, 4), "....")
        self.assertEqual(bar(2.0, 4), "####")

    def test_columnize_aligns(self) -> None:
        lines = columnize([["a", "bb"], ["ccc", "d"]])
        self.assertEqual(lines, ["a    bb", "ccc  d"])


class TableTests(unittest.TestCase):
    def test_header_and_rule(self) -> None:
        table = Table.of("name", "state")
        table.add("extract", "succeeded")
        lines = table.render().splitlines()
        self.assertEqual(lines[0].split(), ["name", "state"])
        self.assertTrue(set(lines[1]) <= {"-", " "})
        self.assertIn("extract", lines[2])

    def test_alignment(self) -> None:
        table = Table.of("n").align("right")
        table.add("1")
        table.add("100")
        lines = table.render().splitlines()
        self.assertEqual(lines[2], "  1")
        self.assertEqual(lines[3], "100")

    def test_cells_are_stringified(self) -> None:
        table = Table.of("a", "b", "c", "d")
        table.add(None, True, False, 1.5)
        self.assertIn("-", table.render())
        self.assertIn("yes", table.render())
        self.assertIn("no", table.render())
        self.assertIn("1.500", table.render())

    def test_short_rows_are_padded(self) -> None:
        table = Table.of("a", "b")
        table.add("only")
        self.assertEqual(len(table.rows[0]), 2)

    def test_too_many_cells_is_an_error(self) -> None:
        table = Table.of("a")
        with self.assertRaises(ValueError):
            table.add("one", "two")

    def test_width_limits_truncate(self) -> None:
        table = Table.of("name").limit(name=6)
        table.add("a-very-long-value")
        self.assertIn("a-v...", table.render())

    def test_empty_table(self) -> None:
        self.assertEqual(Table().render(), "")
        self.assertTrue(Table.of("a").empty)

    def test_render_kv_aligns_colons(self) -> None:
        rendered = render_kv([("short", 1), ("much-longer", 2)])
        first, second = rendered.splitlines()
        self.assertEqual(first.index("1"), second.index("2"))

    def test_render_bars_scales_to_the_largest(self) -> None:
        rendered = render_bars([("a", 10.0), ("b", 5.0)], width=10)
        lines = rendered.splitlines()
        self.assertEqual(lines[0].count("#"), 10)
        self.assertEqual(lines[1].count("#"), 5)

    def test_render_bars_with_an_explicit_total(self) -> None:
        rendered = render_bars([("a", 5.0)], width=10, total=10.0)
        self.assertEqual(rendered.splitlines()[0].count("#"), 5)

    def test_render_bars_of_nothing(self) -> None:
        self.assertEqual(render_bars([]), "")


class MetricTests(unittest.TestCase):
    def test_counter(self) -> None:
        counter = Counter("c")
        counter.add("a", 2)
        counter.add("b")
        counter.add("a")
        self.assertEqual(counter.get("a"), 3)
        self.assertEqual(counter.total, 4)
        self.assertEqual(counter.top(1), [("a", 3)])
        self.assertEqual(counter.get("missing"), 0)

    def test_histogram_buckets(self) -> None:
        histogram = Histogram("h")
        for value in (0.05, 0.5, 5.0, 50.0, 500.0, 5000.0):
            histogram.observe(value)
        self.assertEqual(histogram.count, 6)
        self.assertEqual([count for _, count in histogram.buckets()], [1, 1, 1, 1, 0, 1, 1])

    def test_histogram_statistics(self) -> None:
        histogram = Histogram("h")
        for value in (1.0, 2.0, 3.0, 4.0):
            histogram.observe(value)
        self.assertEqual(histogram.minimum, 1.0)
        self.assertEqual(histogram.maximum, 4.0)
        self.assertEqual(histogram.mean, 2.5)
        self.assertEqual(histogram.quantile(0.0), 1.0)
        self.assertEqual(histogram.quantile(1.0), 4.0)

    def test_empty_histogram(self) -> None:
        histogram = Histogram("h")
        self.assertEqual(histogram.mean, 0.0)
        self.assertEqual(histogram.quantile(0.5), 0.0)
        self.assertIn("no observations", histogram.describe())

    def test_quantile_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Histogram("h").quantile(1.5)

    def test_state_shares(self) -> None:
        counter = Counter("c")
        counter.add("a", 3)
        counter.add("b", 1)
        self.assertEqual(state_shares(counter), {"a": 0.75, "b": 0.25})
        self.assertEqual(state_shares(Counter("empty")), {})


class CollectTests(CampanileTestCase):
    def test_collects_a_single_run(self) -> None:
        result = self.run_workflow(sample_workflow())
        metrics = collect(result.snapshot())
        self.assertEqual(metrics.runs, 1)
        self.assertEqual(metrics.success_rate(), 1.0)
        self.assertEqual(metrics.retries.get("transform"), 1)
        self.assertEqual(metrics.task_states.get("skipped"), 1)

    def test_slowest_and_flakiest(self) -> None:
        result = self.run_workflow(sample_workflow())
        metrics = collect(result.snapshot())
        self.assertEqual(metrics.slowest(1)[0][0], "extract")
        self.assertEqual(metrics.flakiest(1)[0][0], "transform")

    def test_resource_waits_are_counted(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", returns(1), resources="wh")
        builder.task("b", returns(2), resources="wh")
        result = self.run_workflow(builder.build())
        self.assertEqual(collect(result.snapshot()).resource_waits.get("b"), 1)

    def test_failed_runs_lower_the_success_rate(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("bad", failing_task())
        engine = self.engine()
        engine.run(builder.build())
        engine.run(sample_workflow())
        metrics = collect(engine.snapshots())
        self.assertEqual(metrics.runs, 2)
        self.assertEqual(metrics.success_rate(), 0.5)

    def test_no_runs(self) -> None:
        metrics = collect([])
        self.assertEqual(metrics.runs, 0)
        self.assertEqual(metrics.success_rate(), 0.0)

    def test_aggregate_merges(self) -> None:
        result = self.run_workflow(sample_workflow())
        one = collect(result.snapshot())
        merged = aggregate([one, one])
        self.assertEqual(merged.runs, 2)
        self.assertEqual(merged.retries.get("transform"), 2)


class SummaryTests(CampanileTestCase):
    def test_headline_and_facts(self) -> None:
        result = self.run_workflow(sample_workflow())
        summary = RunSummary(result.snapshot())
        self.assertIn("succeeded", summary.headline())
        facts = dict(summary.facts())
        self.assertEqual(facts["workflow"], "etl")
        self.assertEqual(facts["state"], "succeeded")

    def test_task_table_lists_every_task(self) -> None:
        result = self.run_workflow(sample_workflow())
        rendered = RunSummary(result.snapshot()).task_table().render()
        for name in ("extract", "transform", "load", "archive"):
            self.assertIn(name, rendered)

    def test_failure_report(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("bad", failing_task("detail"))
        result = self.run_workflow(builder.build())
        report = RunSummary(result.snapshot()).failure_report()
        self.assertIn("bad", report)
        self.assertIn("detail", report)

    def test_no_failure_report_when_all_is_well(self) -> None:
        result = self.run_workflow(sample_workflow())
        self.assertEqual(RunSummary(result.snapshot()).failure_report(), "")

    def test_blocked_report_explains_skips(self) -> None:
        result = self.run_workflow(sample_workflow())
        report = RunSummary(result.snapshot()).blocked_report()
        self.assertIn("archive", report)

    def test_condition_report(self) -> None:
        result = self.run_workflow(sample_workflow())
        report = RunSummary(result.snapshot()).condition_report()
        self.assertIn("load -> archive", report)
        self.assertIn("false", report)

    def test_verbose_render_includes_the_extra_blocks(self) -> None:
        result = self.run_workflow(sample_workflow())
        terse = summarise(result.snapshot())
        verbose = summarise(result.snapshot(), verbose=True)
        self.assertGreater(len(verbose), len(terse))
        self.assertIn("conditions:", verbose)

    def test_one_line_marks_every_task(self) -> None:
        result = self.run_workflow(sample_workflow())
        line = RunSummary(result.snapshot()).one_line()
        self.assertIn(result.run_id, line)
        self.assertTrue(line.rstrip().endswith("+++-"))

    def test_summarise_many(self) -> None:
        engine = self.engine()
        engine.run(sample_workflow())
        engine.run(sample_workflow())
        rendered = summarise_many(list(engine.snapshots()))
        self.assertIn("2 run(s), 2 succeeded", rendered)

    def test_summarise_no_runs(self) -> None:
        self.assertEqual(summarise_many([]), "no runs\n")


class TimelineTests(CampanileTestCase):
    def test_rows_follow_declaration_order(self) -> None:
        result = self.run_workflow(sample_workflow())
        timeline = Timeline.of(result.snapshot())
        self.assertEqual(
            [row.name for row in timeline.rows],
            ["extract", "transform", "load", "archive"],
        )

    def test_bars_are_placed_within_the_span(self) -> None:
        result = self.run_workflow(sample_workflow())
        timeline = Timeline.of(result.snapshot())
        first = timeline.bar(timeline.rows[0], 40)
        last = timeline.bar(timeline.rows[2], 40)
        self.assertEqual(len(first), 40)
        self.assertLess(first.index("="), last.index("="))

    def test_a_task_that_never_ran_gets_a_blank_row(self) -> None:
        result = self.run_workflow(sample_workflow())
        timeline = Timeline.of(result.snapshot())
        archive = [row for row in timeline.rows if row.name == "archive"][0]
        self.assertEqual(timeline.bar(archive, 20).strip(), "")

    def test_instant_tasks_still_get_a_mark(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns(1))
        result = self.run_workflow(builder.build())
        timeline = Timeline.of(result.snapshot())
        self.assertIn("=", timeline.bar(timeline.rows[0], 10))

    def test_render_includes_a_header_and_legend(self) -> None:
        result = self.run_workflow(sample_workflow())
        rendered = render_timeline(result.snapshot(), width=30)
        self.assertIn("..", rendered)
        self.assertIn("succeeded", rendered)

    def test_busiest_moment(self) -> None:
        result = self.run_workflow(sample_workflow())
        _, count = Timeline.of(result.snapshot()).busiest_moment()
        self.assertGreaterEqual(count, 1)

    def test_critical_path(self) -> None:
        workflow = sample_workflow()
        result = self.run_workflow(workflow)
        path = critical_path(result.snapshot(), workflow.upstream_map())
        self.assertEqual(path, ["extract", "transform", "load"])

    def test_critical_path_of_an_empty_run(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", returns(1))
        snapshot = self.run_workflow(builder.build()).snapshot()
        snapshot.tasks["a"].finished_at = None
        self.assertEqual(critical_path(snapshot, {}), [])


class ExportTests(CampanileTestCase):
    def test_run_json_is_valid_and_sorted(self) -> None:
        result = self.run_workflow(sample_workflow())
        payload = json.loads(export_run_json(result.snapshot()))
        self.assertEqual(payload["workflow"], "etl")
        self.assertEqual(len(payload["tasks"]), 4)

    def test_runs_json_carries_a_header(self) -> None:
        result = self.run_workflow(sample_workflow())
        payload = json.loads(export_runs_json([result.snapshot()]))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 0)

    def test_runs_csv_columns(self) -> None:
        result = self.run_workflow(sample_workflow())
        rows = list(csv.reader(io.StringIO(export_runs_csv([result.snapshot()]))))
        self.assertEqual(tuple(rows[0]), RUN_COLUMNS)
        self.assertEqual(rows[1][1], "etl")
        self.assertEqual(rows[1][7], "3")

    def test_tasks_csv_has_a_row_per_task(self) -> None:
        result = self.run_workflow(sample_workflow())
        rows = list(csv.reader(io.StringIO(export_tasks_csv([result.snapshot()]))))
        self.assertEqual(tuple(rows[0]), TASK_COLUMNS)
        self.assertEqual(len(rows), 5)

    def test_events_jsonl_round_trips(self) -> None:
        result = self.run_workflow(sample_workflow())
        assert result.log is not None
        text = export_events_jsonl(result.log)
        lines = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(lines), len(result.log))
        self.assertEqual(lines[0]["kind"], "run_started")

    def test_exports_are_byte_stable(self) -> None:
        result = self.run_workflow(sample_workflow())
        snapshot = result.snapshot()
        self.assertEqual(export_run_json(snapshot), export_run_json(snapshot))
        self.assertEqual(export_runs_csv([snapshot]), export_runs_csv([snapshot]))

    def test_empty_exports_still_have_headers(self) -> None:
        self.assertEqual(
            export_runs_csv([]).strip(), ",".join(RUN_COLUMNS)
        )
        self.assertEqual(json.loads(export_runs_json([]))["count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
