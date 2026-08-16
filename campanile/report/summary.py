"""Human-readable run summaries.

One run, one page: a headline, a table of tasks, and -- when something went
wrong -- a section explaining what and why.  This is what ``campanile show`` prints
and what a caller pastes into a ticket.

Summaries are built from a replayed :class:`~campanile.store.replay.RunSnapshot`,
not from a live scheduler, so a run that finished last month renders exactly the
same as one that just ended.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..model.state import RunState, TaskState
from ..store.replay import RunSnapshot, TaskSnapshot
from ..util.duration import format_duration
from ..util.ids import format_iso
from ..util.text import indent, pluralize
from .render import Table, render_kv

__all__ = ["RunSummary", "summarise", "summarise_many", "STATE_MARKS"]

#: A one-character mark per task state, for compact listings.
STATE_MARKS: dict[TaskState, str] = {
    TaskState.PENDING: ".",
    TaskState.READY: ">",
    TaskState.RUNNING: "*",
    TaskState.RETRY_WAIT: "~",
    TaskState.SUCCEEDED: "+",
    TaskState.FAILED: "!",
    TaskState.SKIPPED: "-",
    TaskState.UPSTREAM_FAILED: "x",
    TaskState.CANCELLED: "/",
}


@dataclass
class RunSummary:
    """A rendered view of one run."""

    snapshot: RunSnapshot

    # -- pieces --------------------------------------------------------------

    def headline(self) -> str:
        """One line: run, workflow, outcome, timing."""

        snapshot = self.snapshot
        parts = [f"{snapshot.run_id}", f"[{snapshot.state}]", snapshot.workflow]
        if snapshot.started_at is not None:
            parts.append(format_iso(snapshot.started_at))
        if snapshot.duration is not None:
            parts.append(f"in {format_duration(snapshot.duration)}")
        return " ".join(parts)

    def facts(self) -> list[tuple[str, Any]]:
        """Key/value pairs for the header block."""

        snapshot = self.snapshot
        counts = snapshot.counts()
        interesting = {key: value for key, value in counts.items() if value}
        return [
            ("run", snapshot.run_id),
            ("workflow", snapshot.workflow),
            ("state", str(snapshot.state)),
            ("started", format_iso(snapshot.started_at) if snapshot.started_at else "-"),
            (
                "finished",
                format_iso(snapshot.finished_at) if snapshot.finished_at else "-",
            ),
            (
                "duration",
                format_duration(snapshot.duration) if snapshot.duration is not None else "-",
            ),
            ("tasks", ", ".join(f"{key} {value}" for key, value in interesting.items())),
            ("attempts", snapshot.total_attempts()),
            ("params", _render_params(snapshot.params)),
        ]

    def task_table(self) -> Table:
        """A row per task: state, attempts, duration, detail."""

        table = Table.of("", "task", "state", "attempts", "duration", "detail")
        table.align("center", "left", "left", "right", "right", "left")
        for task in self.snapshot.tasks.values():
            table.add(
                STATE_MARKS.get(task.state, "?"),
                task.name,
                str(task.state),
                task.attempts or "-",
                format_duration(task.duration) if task.duration is not None else "-",
                _detail(task),
            )
        return table

    def failure_report(self) -> str:
        """A block describing every failure, or an empty string."""

        failures = self.snapshot.failures()
        if not failures:
            return ""
        lines = [f"{pluralize(len(failures), 'failure')}:"]
        for task in failures:
            error = task.error or {}
            kind = error.get("type", "error")
            message = error.get("message", "no detail")
            lines.append(f"  {task.name} ({kind}) after {task.attempts} attempt(s)")
            lines.append(f"    {message}")
            if task.timed_out:
                lines.append("    exceeded its timeout")
        return "\n".join(lines)

    def blocked_report(self) -> str:
        """A block naming tasks that never ran, and why."""

        blocked = [
            task
            for task in self.snapshot.tasks.values()
            if task.state in (TaskState.SKIPPED, TaskState.UPSTREAM_FAILED, TaskState.CANCELLED)
        ]
        if not blocked:
            return ""
        lines = [f"{pluralize(len(blocked), 'task')} did not run:"]
        for task in blocked:
            reason = task.skip_reason or str(task.state)
            lines.append(f"  {task.name} ({task.state}): {reason}")
        return "\n".join(lines)

    def condition_report(self) -> str:
        """A block listing every edge condition that was evaluated."""

        if not self.snapshot.conditions:
            return ""
        table = Table.of("edge", "condition", "result")
        for entry in self.snapshot.conditions:
            outcome = entry.get("result")
            rendered = "error" if outcome is None else ("true" if outcome else "false")
            table.add(
                f"{entry.get('upstream')} -> {entry.get('downstream')}",
                str(entry.get("condition")),
                rendered,
            )
        return "conditions:\n" + indent(table.render())

    # -- assembly ------------------------------------------------------------

    def render(self, *, verbose: bool = False) -> str:
        """The whole summary."""

        blocks = [render_kv(self.facts()), "", self.task_table().render()]

        failures = self.failure_report()
        if failures:
            blocks.extend(["", failures])

        if verbose:
            blocked = self.blocked_report()
            if blocked:
                blocks.extend(["", blocked])
            conditions = self.condition_report()
            if conditions:
                blocks.extend(["", conditions])

        return "\n".join(blocks).rstrip() + "\n"

    def one_line(self) -> str:
        """A single line, for listings."""

        marks = "".join(
            STATE_MARKS.get(task.state, "?") for task in self.snapshot.tasks.values()
        )
        duration = (
            format_duration(self.snapshot.duration)
            if self.snapshot.duration is not None
            else "-"
        )
        return f"{self.snapshot.run_id}  {self.snapshot.state:<10}  {duration:>8}  {marks}"

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot.as_dict()


def summarise(snapshot: RunSnapshot, *, verbose: bool = False) -> str:
    """Render one run."""

    return RunSummary(snapshot).render(verbose=verbose)


def summarise_many(snapshots: Sequence[RunSnapshot]) -> str:
    """Render a list of runs, one per line, with a totals footer."""

    if not snapshots:
        return "no runs\n"

    table = Table.of("run", "workflow", "state", "started", "duration", "tasks")
    table.align("left", "left", "left", "left", "right", "right")
    for snapshot in snapshots:
        table.add(
            snapshot.run_id,
            snapshot.workflow,
            str(snapshot.state),
            format_iso(snapshot.started_at) if snapshot.started_at else "-",
            format_duration(snapshot.duration) if snapshot.duration is not None else "-",
            len(snapshot.tasks),
        )

    succeeded = sum(1 for snapshot in snapshots if snapshot.state is RunState.SUCCEEDED)
    footer = f"{len(snapshots)} run(s), {succeeded} succeeded"
    return table.render() + "\n\n" + footer + "\n"


def _detail(task: TaskSnapshot) -> str:
    if task.error:
        return str(task.error.get("message", "failed"))
    if task.skip_reason:
        return task.skip_reason
    if task.retries:
        return f"{task.retries} retry(ies)"
    if task.resource_waits:
        return f"waited {task.resource_waits}x for resources"
    return ""


def _render_params(params: dict[str, Any]) -> str:
    if not params:
        return "(none)"
    return ", ".join(f"{key}={value!r}" for key, value in sorted(params.items()))
