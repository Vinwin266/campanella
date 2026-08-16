"""The commands themselves.

Each command is a plain function taking parsed arguments and a
:class:`~campanile.cli.formatting.Console`, and returning an exit status.  None of
them read ``sys.argv``, none of them call ``print``, and none of them exit the
process -- which is what lets the whole surface be tested by calling the
functions directly.
"""

from __future__ import annotations

import argparse
from typing import Any, Sequence

from ..dsl.validate import validate as validate_workflow
from ..errors import CampanileError, ConfigurationError, StoreError
from ..model.state import RunState
from ..model.workflow import Workflow
from ..report.export import (
    export_events_jsonl,
    export_runs_csv,
    export_runs_json,
    export_tasks_csv,
)
from ..report.metrics import collect
from ..report.render import Table, render_bars, render_kv
from ..report.summary import summarise, summarise_many
from ..report.timeline import critical_path, render_timeline
from ..runtime.engine import Engine
from ..schedule.cron import field_summary, parse_cron
from ..schedule.interval import make_trigger
from ..store.filestore import FileStore
from ..store.memory import InMemoryStore
from ..store.query import RunQuery
from ..store.replay import replay
from ..util.clock import ManualClock, SystemClock
from ..util.duration import format_duration
from ..util.ids import format_iso, parse_timestamp
from .formatting import Console, ExitCode, parse_assignment
from .loader import load_workflows, select_workflow

__all__ = [
    "cmd_describe",
    "cmd_validate",
    "cmd_plan",
    "cmd_run",
    "cmd_runs",
    "cmd_show",
    "cmd_events",
    "cmd_timeline",
    "cmd_metrics",
    "cmd_export",
    "cmd_schedule",
]


# --------------------------------------------------------------------------
# definition commands
# --------------------------------------------------------------------------


def cmd_describe(args: argparse.Namespace, console: Console) -> int:
    """Print a workflow's structure without running anything."""

    workflow = _load(args)
    console.block(workflow.describe())
    console.write()
    console.block(_wave_table(Engine().plan(workflow)).render())
    return ExitCode.OK


def cmd_validate(args: argparse.Namespace, console: Console) -> int:
    """Run every static check and report what it found."""

    workflows = load_workflows(args.file)
    names = [args.workflow] if args.workflow else sorted(workflows)
    worst = ExitCode.OK

    for name in names:
        workflow = select_workflow(workflows, name)
        issues = validate_workflow(workflow)
        errors = [issue for issue in issues if issue.is_error]
        warnings = [issue for issue in issues if not issue.is_error]

        console.write(f"{workflow.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
        console.issues(issues)

        if errors or (warnings and args.strict):
            worst = ExitCode.DEFINITION

    return worst


def cmd_plan(args: argparse.Namespace, console: Console) -> int:
    """Show the waves a run would go through."""

    workflow = _load(args)
    waves = Engine().plan(workflow)
    console.block(_wave_table(waves).render())
    console.write()
    console.write(
        f"{len(workflow.tasks)} task(s) in {len(waves)} wave(s); "
        f"widest wave holds {max((len(wave) for wave in waves), default=0)}"
    )
    if workflow.max_parallelism is not None:
        console.write(f"max parallelism is capped at {workflow.max_parallelism}")
    return ExitCode.OK


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    """Run a workflow once and report the outcome."""

    workflow = _load(args)
    params = _collect_params(args)
    store = _open_store(args)
    clock = SystemClock() if getattr(args, "real_time", False) else ManualClock(
        _origin(args)
    )

    engine = Engine(clock=clock, store=store, strict=bool(getattr(args, "strict", False)))
    if args.only:
        workflow = workflow.subgraph(_expand_only(workflow, args.only))

    result = engine.run(workflow, params, run_id=_free_run_id(engine, workflow.name))

    if args.json:
        console.raw(export_runs_json([result.snapshot()]) + "\n")
    else:
        console.block(summarise(result.snapshot(), verbose=args.verbose))

    if result.state is RunState.SUCCEEDED:
        return ExitCode.OK
    return ExitCode.RUN_FAILED


# --------------------------------------------------------------------------
# history commands
# --------------------------------------------------------------------------


def cmd_runs(args: argparse.Namespace, console: Console) -> int:
    """List runs held in a store."""

    store = _open_store(args, create=False)
    snapshots = [
        replay(store.log(run_id))
        for run_id in store.run_ids()
        if len(store.log(run_id))
    ]
    query = RunQuery(
        workflow=args.workflow,
        failed_only=args.failed,
        limit=args.limit,
        sort_by=args.sort,
        descending=args.desc,
    )
    matched = query.apply(snapshots)

    if args.json:
        console.raw(export_runs_json(matched) + "\n")
        return ExitCode.OK

    console.block(summarise_many(matched))
    return ExitCode.OK


def cmd_show(args: argparse.Namespace, console: Console) -> int:
    """Show one run in full."""

    snapshot = _load_run(args)
    if args.json:
        console.raw(export_runs_json([snapshot]) + "\n")
        return ExitCode.OK
    console.block(summarise(snapshot, verbose=True))
    return ExitCode.OK


def cmd_events(args: argparse.Namespace, console: Console) -> int:
    """Dump a run's event log."""

    store = _open_store(args, create=False)
    log = store.log(args.run)

    events = list(log)
    if args.task:
        events = [event for event in events if event.task == args.task]
    if args.kind:
        wanted = set(args.kind)
        events = [event for event in events if event.kind.value in wanted]

    if args.json:
        console.raw(export_events_jsonl(events))
        return ExitCode.OK

    for event in events:
        console.write(event.describe())
    console.write()
    console.write(f"{len(events)} event(s) of {len(log)} in run {args.run}")
    return ExitCode.OK


def cmd_timeline(args: argparse.Namespace, console: Console) -> int:
    """Draw a run's timeline."""

    snapshot = _load_run(args)
    console.block(render_timeline(snapshot, width=args.width))
    if args.file:
        workflow = _load(args)
        path = critical_path(snapshot, workflow.upstream_map())
        if path:
            console.write("critical path: " + " -> ".join(path))
    return ExitCode.OK


def cmd_metrics(args: argparse.Namespace, console: Console) -> int:
    """Summarise a store's runs as counters and histograms."""

    store = _open_store(args, create=False)
    snapshots = [
        replay(store.log(run_id))
        for run_id in store.run_ids()
        if len(store.log(run_id))
    ]
    if args.workflow:
        snapshots = [
            snapshot for snapshot in snapshots if snapshot.workflow == args.workflow
        ]

    metrics = collect(snapshots)
    if args.json:
        from ..util.jsonio import canonical_dumps

        console.raw(canonical_dumps(metrics.as_dict(), indent=2) + "\n")
        return ExitCode.OK

    console.block(metrics.describe())
    slowest = metrics.slowest(args.top)
    if slowest:
        console.write()
        console.write("slowest tasks:")
        console.block(render_bars([(name, value) for name, value in slowest]))
    flakiest = metrics.flakiest(args.top)
    if flakiest:
        console.write()
        console.write("most retried tasks:")
        console.block(render_bars([(name, float(value)) for name, value in flakiest]))
    return ExitCode.OK


def cmd_export(args: argparse.Namespace, console: Console) -> int:
    """Write runs out in a machine-readable format."""

    store = _open_store(args, create=False)
    snapshots = [
        replay(store.log(run_id))
        for run_id in store.run_ids()
        if len(store.log(run_id))
    ]
    if args.run:
        snapshots = [snapshot for snapshot in snapshots if snapshot.run_id == args.run]
        if not snapshots:
            raise StoreError(f"no run {args.run!r} in {args.store}")

    if args.format == "json":
        console.raw(export_runs_json(snapshots) + "\n")
    elif args.format == "csv":
        console.raw(export_runs_csv(snapshots))
    elif args.format == "tasks-csv":
        console.raw(export_tasks_csv(snapshots))
    else:  # pragma: no cover - argparse restricts the choices
        raise ConfigurationError(f"unknown export format {args.format!r}")
    return ExitCode.OK


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------


def cmd_schedule(args: argparse.Namespace, console: Console) -> int:
    """Explain a schedule and list its next fires."""

    trigger = make_trigger(args.expression)
    console.write(trigger.describe())

    if len(str(args.expression).split()) == 5 or str(args.expression).startswith("@"):
        expression = parse_cron(args.expression)
        table = Table.of("field", "matches")
        for name, values in field_summary(expression):
            table.add(name, values)
        console.block(table.render())

    start = parse_timestamp(args.after) if args.after else 0.0
    console.write()
    console.write(f"next {args.count} fire(s) after {format_iso(start)}:")

    moment = start
    for _ in range(args.count):
        following = trigger.next_after(moment)
        if following is None:
            console.write("  (no further fires)")
            break
        gap = following - moment
        console.write(f"  {format_iso(following)}  (+{format_duration(gap)})")
        moment = following
    return ExitCode.OK


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load(args: argparse.Namespace) -> Workflow:
    if not getattr(args, "file", None):
        raise ConfigurationError("this command needs a workflow file")
    workflows = load_workflows(args.file)
    return select_workflow(workflows, getattr(args, "workflow", None))


def _open_store(args: argparse.Namespace, *, create: bool = True) -> Any:
    location = getattr(args, "store", None)
    if not location:
        if not create:
            raise StoreError("this command needs --store pointing at a run directory")
        return InMemoryStore()
    return FileStore(location, create=create)


def _load_run(args: argparse.Namespace) -> Any:
    store = _open_store(args, create=False)
    run_id = args.run
    if run_id in (None, "last", "latest"):
        ids = store.run_ids()
        if not ids:
            raise StoreError(f"no runs in {args.store}")
        run_id = ids[-1]
    return replay(store.log(run_id))


def _collect_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for assignment in getattr(args, "param", None) or ():
        try:
            key, value = parse_assignment(assignment)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        params[key] = value
    return params


def _free_run_id(engine: Engine, workflow: str) -> str:
    """Pick a run id the store does not already hold.

    Every CLI invocation starts a fresh id factory, and under a virtual clock
    every invocation also starts at the same instant -- so without this, a second
    run against the same directory would collide with the first.  Counting past
    what is already there keeps ids unique and still ordered.
    """

    for _ in range(10_000):
        candidate = engine.ids.run_id(workflow, engine.clock.now())
        if not engine.store.has(candidate):
            return candidate
    raise StoreError(f"cannot find a free run id for workflow {workflow!r}")


def _origin(args: argparse.Namespace) -> float:
    stamp = getattr(args, "at", None)
    if not stamp:
        return 1767225600.0
    return parse_timestamp(stamp)


def _expand_only(workflow: Workflow, names: Sequence[str]) -> list[str]:
    """Expand ``--only`` into the named tasks plus everything they need."""

    wanted: list[str] = []
    for name in names:
        workflow.task(name)
        for ancestor in workflow.ancestors_of(name):
            if ancestor not in wanted:
                wanted.append(ancestor)
        if name not in wanted:
            wanted.append(name)
    return [task for task in workflow.task_names if task in wanted]


def _wave_table(waves: Sequence[Sequence[str]]) -> Table:
    table = Table.of("wave", "tasks")
    table.align("right", "left")
    for index, wave in enumerate(waves):
        table.add(index, ", ".join(wave))
    return table


def _render_summary_facts(pairs: Sequence[tuple[str, Any]]) -> str:
    return render_kv(pairs)


def describe_error(error: CampanileError) -> str:  # pragma: no cover - thin wrapper
    return f"{error.code}: {error.message}"
