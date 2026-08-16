"""Argument parsing and dispatch.

``campanile <command> [options]``.  The parser is built in one function so that
``campanile --help`` and the tests see exactly the same surface, and :func:`main`
returns an exit status instead of calling ``sys.exit``, so it can be driven from
a test without a subprocess.
"""

from __future__ import annotations

import argparse
from typing import Callable, Sequence

from ..errors import CampanileError
from ..version import VERSION
from . import commands
from .formatting import Console, ExitCode, exit_code_for, render_error

__all__ = ["build_parser", "main", "run_command", "COMMANDS"]

Command = Callable[[argparse.Namespace, Console], int]

#: Command name -> implementation.  Kept separate from the parser so a caller
#: can invoke a command directly with a hand-built namespace.
COMMANDS: dict[str, Command] = {
    "describe": commands.cmd_describe,
    "validate": commands.cmd_validate,
    "plan": commands.cmd_plan,
    "run": commands.cmd_run,
    "runs": commands.cmd_runs,
    "show": commands.cmd_show,
    "events": commands.cmd_events,
    "timeline": commands.cmd_timeline,
    "metrics": commands.cmd_metrics,
    "export": commands.cmd_export,
    "schedule": commands.cmd_schedule,
}

_EXPORT_FORMATS = ("json", "csv", "tasks-csv")
_SORT_KEYS = ("started", "finished", "duration", "workflow", "run_id", "tasks")


def build_parser() -> argparse.ArgumentParser:
    """Build the whole command-line surface."""

    parser = argparse.ArgumentParser(
        prog="campanile",
        description="Run and inspect deterministic workflows.",
    )
    parser.add_argument("--version", action="version", version=f"campanile {VERSION}")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress ordinary output"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    # -- definition ---------------------------------------------------------

    describe = subparsers.add_parser("describe", help="print a workflow's structure")
    _add_definition_arguments(describe)

    validate = subparsers.add_parser("validate", help="run the static checks")
    _add_definition_arguments(validate)
    validate.add_argument(
        "--strict", action="store_true", help="treat warnings as errors"
    )

    plan = subparsers.add_parser("plan", help="show the waves a run would go through")
    _add_definition_arguments(plan)

    # -- running ------------------------------------------------------------

    run = subparsers.add_parser("run", help="run a workflow once")
    _add_definition_arguments(run)
    run.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="a run parameter; repeat for more than one",
    )
    run.add_argument("--store", help="directory to record the run in")
    run.add_argument(
        "--only",
        nargs="+",
        metavar="TASK",
        help="run only these tasks and what they depend on",
    )
    run.add_argument("--at", metavar="TIMESTAMP", help="start the virtual clock here")
    run.add_argument(
        "--real-time",
        action="store_true",
        help="use the host clock and sleep through retry backoff",
    )
    run.add_argument("--strict", action="store_true", help="treat warnings as errors")
    run.add_argument("-v", "--verbose", action="store_true", help="show skipped tasks")
    run.add_argument("--json", action="store_true", help="emit JSON instead of a report")

    # -- history ------------------------------------------------------------

    runs = subparsers.add_parser("runs", help="list recorded runs")
    runs.add_argument("--store", required=True, help="directory holding the runs")
    runs.add_argument("--workflow", help="only runs of this workflow")
    runs.add_argument("--failed", action="store_true", help="only failed runs")
    runs.add_argument("--limit", type=int, help="show at most this many")
    runs.add_argument("--sort", choices=_SORT_KEYS, default="started")
    runs.add_argument("--desc", action="store_true", help="reverse the sort")
    runs.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="show one run in full")
    show.add_argument("run", nargs="?", default="last", help="run id, or 'last'")
    show.add_argument("--store", required=True)
    show.add_argument("--json", action="store_true")

    events = subparsers.add_parser("events", help="dump a run's event log")
    events.add_argument("run")
    events.add_argument("--store", required=True)
    events.add_argument("--task", help="only events for this task")
    events.add_argument("--kind", nargs="+", help="only these event kinds")
    events.add_argument("--json", action="store_true")

    timeline = subparsers.add_parser("timeline", help="draw a run's timeline")
    timeline.add_argument("run", nargs="?", default="last")
    timeline.add_argument("--store", required=True)
    timeline.add_argument("--width", type=int, default=48)
    timeline.add_argument("--file", help="workflow file, to add the critical path")
    timeline.add_argument("--workflow", help="which workflow in that file")

    metrics = subparsers.add_parser("metrics", help="summarise a store's runs")
    metrics.add_argument("--store", required=True)
    metrics.add_argument("--workflow")
    metrics.add_argument("--top", type=int, default=5)
    metrics.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export", help="write runs out for other tools")
    export.add_argument("--store", required=True)
    export.add_argument("--run", help="a single run id")
    export.add_argument("--format", choices=_EXPORT_FORMATS, default="json")

    # -- scheduling ---------------------------------------------------------

    schedule = subparsers.add_parser("schedule", help="explain a schedule")
    schedule.add_argument("expression", help="cron expression, macro or interval")
    schedule.add_argument("--count", type=int, default=5, help="how many fires to list")
    schedule.add_argument("--after", metavar="TIMESTAMP", help="start looking here")

    return parser


def _add_definition_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="Python file defining the workflow")
    parser.add_argument("--workflow", help="which workflow, if the file has several")


def run_command(
    args: argparse.Namespace, console: Console | None = None
) -> int:
    """Dispatch to a command, turning package errors into exit statuses."""

    console = console or Console(quiet=bool(getattr(args, "quiet", False)))
    handler = COMMANDS.get(getattr(args, "command", None) or "")
    if handler is None:
        console.error("error: no command given; try 'campanile --help'")
        return ExitCode.USAGE

    try:
        return handler(args, console)
    except CampanileError as error:
        console.error(render_error(error))
        return exit_code_for(error)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.error("interrupted")
        return ExitCode.INTERRUPTED


def main(argv: Sequence[str] | None = None, console: Console | None = None) -> int:
    """Entry point.  Returns an exit status rather than raising ``SystemExit``."""

    parser = build_parser()
    args = parser.parse_args(argv)
    console = console or Console(quiet=bool(getattr(args, "quiet", False)))
    if getattr(args, "command", None) is None:
        parser.print_help(console.out)
        return ExitCode.USAGE
    return run_command(args, console)


if __name__ == "__main__":  # pragma: no cover - module entry point
    import sys

    sys.exit(main())
