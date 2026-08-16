"""The command line: loading files, parsing arguments, running commands."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path

from campanile.cli.formatting import Console, ExitCode, exit_code_for, parse_assignment, render_error
from campanile.cli.loader import load_module, load_workflows, select_workflow, workflows_in
from campanile.cli.main import build_parser, main, run_command
from campanile.errors import ConfigurationError, StoreError, WorkflowDefinitionError

from .helpers import CampanileTestCase

BUILDER_MODULE = '''
from campanile import WorkflowBuilder

builder = WorkflowBuilder("sample").param("region", "string", default="eu").pool("wh", 1)
extract = builder.task("extract", lambda ctx: {"rows": 3}, resources="wh")
load = builder.task("load", lambda ctx: ctx.result("extract")["rows"], after="extract")
publish = builder.task("publish", lambda ctx: "published")
load.when("results.load > 100") >> publish

sample = builder.build()
'''

REGISTRY_MODULE = '''
from campanile import TaskRegistry

registry = TaskRegistry("registered")


@registry.task
def first(ctx):
    """Do the first thing."""
    return 1


@registry.task(after=["first"])
def second(ctx):
    return ctx.result("first") + 1
'''

FAILING_MODULE = '''
from campanile import WorkflowBuilder

builder = WorkflowBuilder("broken")
builder.task("bad", lambda ctx: 1 / 0)
broken = builder.build()
'''

INVALID_MODULE = '''
from campanile import WorkflowBuilder

builder = WorkflowBuilder("invalid")
builder.task("a", lambda ctx: 1, resources="nonexistent")
invalid = builder.workflow
'''

TWO_WORKFLOWS_MODULE = '''
from campanile import WorkflowBuilder

first_builder = WorkflowBuilder("first")
first_builder.task("a", lambda ctx: 1)
first = first_builder.build()

second_builder = WorkflowBuilder("second")
second_builder.task("b", lambda ctx: 2)
second = second_builder.build()
'''


class CliTestCase(CampanileTestCase):
    """Runs commands in-process and captures what they wrote."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp_dir()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.console = Console(out=self.out, err=self.err)

    def write_module(self, name: str, source: str) -> Path:
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def invoke(self, *argv: str) -> int:
        return main(list(argv), console=self.console)

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()


class FormattingTests(unittest.TestCase):
    def test_parse_assignment_reads_json_then_falls_back(self) -> None:
        self.assertEqual(parse_assignment("n=3"), ("n", 3))
        self.assertEqual(parse_assignment("flag=true"), ("flag", True))
        self.assertEqual(parse_assignment("day=2026-03-11"), ("day", "2026-03-11"))
        self.assertEqual(parse_assignment("xs=[1,2]"), ("xs", [1, 2]))

    def test_parse_assignment_rejects_bad_input(self) -> None:
        for text in ("no-equals", "=value"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_assignment(text)

    def test_exit_codes_follow_the_error_family(self) -> None:
        self.assertEqual(exit_code_for(ConfigurationError("x")), ExitCode.DEFINITION)
        self.assertEqual(exit_code_for(StoreError("x")), ExitCode.STORAGE)
        self.assertEqual(exit_code_for(KeyboardInterrupt()), ExitCode.INTERRUPTED)

    def test_render_error_includes_the_code(self) -> None:
        self.assertIn("[configuration_error]", render_error(ConfigurationError("x")))
        self.assertIn("ValueError", render_error(ValueError("x")))

    def test_quiet_suppresses_output_but_not_errors(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        console = Console(out=out, err=err, quiet=True)
        console.write("hidden")
        console.error("shown")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("shown", err.getvalue())


class LoaderTests(CliTestCase):
    def test_loads_a_builder_module(self) -> None:
        path = self.write_module("sample.py", BUILDER_MODULE)
        workflows = load_workflows(path)
        self.assertEqual(list(workflows), ["sample"])
        self.assertEqual(len(workflows["sample"].tasks), 3)

    def test_a_builder_and_its_workflow_do_not_collide(self) -> None:
        path = self.write_module("sample.py", BUILDER_MODULE)
        self.assertEqual(len(load_workflows(path)), 1)

    def test_loads_a_registry_module(self) -> None:
        path = self.write_module("registry.py", REGISTRY_MODULE)
        workflows = load_workflows(path)
        self.assertEqual(list(workflows), ["registered"])
        self.assertEqual(workflows["registered"].task_names, ("first", "second"))

    def test_select_defaults_to_the_only_workflow(self) -> None:
        workflows = load_workflows(self.write_module("sample.py", BUILDER_MODULE))
        self.assertEqual(select_workflow(workflows).name, "sample")

    def test_select_needs_a_name_when_there_are_several(self) -> None:
        workflows = load_workflows(self.write_module("two.py", TWO_WORKFLOWS_MODULE))
        with self.assertRaises(ConfigurationError):
            select_workflow(workflows)
        self.assertEqual(select_workflow(workflows, "second").name, "second")

    def test_select_rejects_an_unknown_name(self) -> None:
        workflows = load_workflows(self.write_module("sample.py", BUILDER_MODULE))
        with self.assertRaises(ConfigurationError):
            select_workflow(workflows, "ghost")

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_module(self.root / "nope.py")

    def test_non_python_file(self) -> None:
        path = self.root / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_module(path)

    def test_a_directory_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_module(self.root)

    def test_an_import_error_is_reported_not_masked(self) -> None:
        path = self.write_module("bad.py", "raise ValueError('module blew up')")
        with self.assertRaises(ConfigurationError) as caught:
            load_module(path)
        self.assertIn("module blew up", str(caught.exception))

    def test_a_module_with_no_workflows(self) -> None:
        path = self.write_module("empty.py", "x = 1\n")
        with self.assertRaises(ConfigurationError):
            load_workflows(path)

    def test_two_distinct_workflows_with_one_name_are_rejected(self) -> None:
        from campanile import WorkflowBuilder

        first = WorkflowBuilder("same")
        first.task("a", lambda ctx: 1)
        second = WorkflowBuilder("same")
        second.task("b", lambda ctx: 2)
        with self.assertRaises(WorkflowDefinitionError):
            workflows_in({"one": first.build(), "two": second.build()})

    def test_private_names_are_skipped(self) -> None:
        from campanile import WorkflowBuilder

        builder = WorkflowBuilder("hidden")
        builder.task("a", lambda ctx: 1)
        self.assertEqual(workflows_in({"_hidden": builder.build()}), {})


class ParserTests(unittest.TestCase):
    def test_every_command_is_registered(self) -> None:
        parser = build_parser()
        for command in (
            "describe", "validate", "plan", "run", "runs", "show",
            "events", "timeline", "metrics", "export", "schedule",
        ):
            with self.subTest(command=command):
                args = parser.parse_args([command] + _minimum_arguments(command))
                self.assertEqual(args.command, command)

    def test_no_command_prints_help(self) -> None:
        out = io.StringIO()
        console = Console(out=out, err=io.StringIO())
        self.assertEqual(main([], console=console), ExitCode.USAGE)
        self.assertIn("usage", out.getvalue().lower())

    def test_unknown_command_in_a_namespace(self) -> None:
        import argparse

        namespace = argparse.Namespace(command="nonsense")
        console = Console(out=io.StringIO(), err=io.StringIO())
        self.assertEqual(run_command(namespace, console), ExitCode.USAGE)


def _minimum_arguments(command: str) -> list[str]:
    if command in ("describe", "validate", "plan", "run"):
        return ["file.py"]
    if command in ("runs", "metrics", "export"):
        return ["--store", "dir"]
    if command in ("show", "timeline"):
        return ["--store", "dir"]
    if command == "events":
        return ["run-1", "--store", "dir"]
    return ["@daily"]


class CommandTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = self.write_module("sample.py", BUILDER_MODULE)
        self.store = self.temp_dir()

    def test_describe(self) -> None:
        self.assertEqual(self.invoke("describe", str(self.module)), ExitCode.OK)
        self.assertIn("workflow sample", self.stdout)
        self.assertIn("wave", self.stdout)

    def test_validate_clean(self) -> None:
        self.assertEqual(self.invoke("validate", str(self.module)), ExitCode.OK)
        self.assertIn("0 error(s)", self.stdout)

    def test_validate_reports_errors(self) -> None:
        module = self.write_module("invalid.py", INVALID_MODULE)
        self.assertEqual(self.invoke("validate", str(module)), ExitCode.DEFINITION)
        self.assertIn("unknown_pool", self.stderr)

    def test_plan(self) -> None:
        self.assertEqual(self.invoke("plan", str(self.module)), ExitCode.OK)
        self.assertIn("3 task(s)", self.stdout)

    def test_run_succeeds(self) -> None:
        code = self.invoke("run", str(self.module), "--store", str(self.store))
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("succeeded", self.stdout)
        self.assertTrue(list(self.store.glob("*.jsonl")))

    def test_run_with_parameters(self) -> None:
        code = self.invoke("run", str(self.module), "--param", "region=us")
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("region='us'", self.stdout)

    def test_run_rejects_a_bad_assignment(self) -> None:
        code = self.invoke("run", str(self.module), "--param", "nonsense")
        self.assertEqual(code, ExitCode.DEFINITION)
        self.assertIn("key=value", self.stderr)

    def test_run_json_output(self) -> None:
        self.assertEqual(self.invoke("run", str(self.module), "--json"), ExitCode.OK)
        payload = json.loads(self.stdout)
        self.assertEqual(payload["count"], 1)

    def test_run_reports_a_failure_with_a_nonzero_status(self) -> None:
        module = self.write_module("failing.py", FAILING_MODULE)
        self.assertEqual(self.invoke("run", str(module)), ExitCode.RUN_FAILED)
        self.assertIn("failed", self.stdout)

    def test_run_only_narrows_the_graph(self) -> None:
        code = self.invoke("run", str(self.module), "--only", "load")
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("extract", self.stdout)
        self.assertNotIn("publish", self.stdout)

    def test_repeated_runs_get_distinct_ids(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.assertEqual(len(list(self.store.glob("*.jsonl"))), 2)

    def test_runs_listing(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(self.invoke("runs", "--store", str(self.store)), ExitCode.OK)
        self.assertIn("1 run(s), 1 succeeded", self.stdout)

    def test_runs_json(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        self.invoke("runs", "--store", str(self.store), "--json")
        self.assertEqual(json.loads(self.stdout)["count"], 1)

    def test_show_defaults_to_the_last_run(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(self.invoke("show", "--store", str(self.store)), ExitCode.OK)
        self.assertIn("workflow:", self.stdout)

    def test_show_without_any_runs(self) -> None:
        self.assertEqual(
            self.invoke("show", "--store", str(self.store)), ExitCode.STORAGE
        )

    def test_events(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        run_id = next(self.store.glob("*.jsonl")).stem
        self.out.truncate(0)
        self.out.seek(0)
        code = self.invoke("events", run_id, "--store", str(self.store), "--kind", "task_succeeded")
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("task_succeeded", self.stdout)

    def test_events_filtered_by_task(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        run_id = next(self.store.glob("*.jsonl")).stem
        self.out.truncate(0)
        self.out.seek(0)
        self.invoke("events", run_id, "--store", str(self.store), "--task", "extract")
        self.assertNotIn(" load ", self.stdout)

    def test_timeline(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        code = self.invoke(
            "timeline", "--store", str(self.store), "--file", str(self.module)
        )
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("critical path", self.stdout)

    def test_metrics(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(self.invoke("metrics", "--store", str(self.store)), ExitCode.OK)
        self.assertIn("success rate", self.stdout)

    def test_metrics_json(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        self.invoke("metrics", "--store", str(self.store), "--json")
        self.assertEqual(json.loads(self.stdout)["runs"], 1)

    def test_export_csv(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        self.out.truncate(0)
        self.out.seek(0)
        code = self.invoke("export", "--store", str(self.store), "--format", "csv")
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("run_id,workflow,state", self.stdout)

    def test_export_of_an_unknown_run(self) -> None:
        self.invoke("run", str(self.module), "--store", str(self.store))
        code = self.invoke("export", "--store", str(self.store), "--run", "ghost")
        self.assertEqual(code, ExitCode.STORAGE)

    def test_schedule_explains_a_cron_expression(self) -> None:
        code = self.invoke(
            "schedule", "0 2 * * mon-fri", "--count", "2", "--after", "2026-03-11T00:00:00Z"
        )
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("weekday", self.stdout)
        self.assertIn("2026-03-11T02:00:00Z", self.stdout)

    def test_schedule_of_an_interval(self) -> None:
        self.assertEqual(self.invoke("schedule", "every 30s", "--count", "2"), ExitCode.OK)
        self.assertIn("every 30s", self.stdout)

    def test_schedule_of_a_one_shot_that_runs_out(self) -> None:
        self.invoke("schedule", "0 0 30 2 *", "--count", "2")
        self.assertIn("no further fires", self.stdout)

    def test_a_missing_file_is_a_definition_error(self) -> None:
        code = self.invoke("describe", str(self.root / "ghost.py"))
        self.assertEqual(code, ExitCode.DEFINITION)
        self.assertIn("does not exist", self.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
