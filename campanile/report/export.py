"""Machine-readable exports.

Three formats, all deterministic: JSON for tooling, CSV for a spreadsheet, and
JSONL for streaming a log somewhere else.  Field order is fixed and keys are
sorted, so exporting the same run twice produces identical bytes and a diff
between two runs shows only real differences.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Sequence

from ..store.events import Event
from ..store.replay import RunSnapshot
from ..util.ids import format_iso
from ..util.jsonio import canonical_dumps, dump_lines

__all__ = [
    "RUN_COLUMNS",
    "TASK_COLUMNS",
    "export_run_json",
    "export_runs_json",
    "export_runs_csv",
    "export_tasks_csv",
    "export_events_jsonl",
]

#: Columns of the run-level CSV, in order.
RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "workflow",
    "state",
    "started_at",
    "finished_at",
    "duration",
    "tasks",
    "succeeded",
    "failed",
    "skipped",
    "attempts",
)

#: Columns of the task-level CSV, in order.
TASK_COLUMNS: tuple[str, ...] = (
    "run_id",
    "workflow",
    "task",
    "state",
    "attempts",
    "retries",
    "started_at",
    "finished_at",
    "duration",
    "timed_out",
    "detail",
)


def export_run_json(snapshot: RunSnapshot, *, indent: int | None = 2) -> str:
    """One run as a JSON document."""

    return canonical_dumps(snapshot.as_dict(), indent=indent)


def export_runs_json(
    snapshots: Sequence[RunSnapshot], *, indent: int | None = 2
) -> str:
    """Several runs as a JSON document with a summary header."""

    payload = {
        "runs": [snapshot.as_dict() for snapshot in snapshots],
        "count": len(snapshots),
        "succeeded": sum(1 for snapshot in snapshots if snapshot.succeeded),
        "failed": sum(1 for snapshot in snapshots if snapshot.failed),
    }
    return canonical_dumps(payload, indent=indent)


def export_runs_csv(snapshots: Iterable[RunSnapshot]) -> str:
    """Run-level CSV with a header row."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(RUN_COLUMNS)
    for snapshot in snapshots:
        counts = snapshot.counts()
        writer.writerow(
            [
                snapshot.run_id,
                snapshot.workflow,
                snapshot.state.value,
                _stamp(snapshot.started_at),
                _stamp(snapshot.finished_at),
                _number(snapshot.duration),
                len(snapshot.tasks),
                counts.get("succeeded", 0),
                counts.get("failed", 0),
                counts.get("skipped", 0) + counts.get("upstream_failed", 0),
                snapshot.total_attempts(),
            ]
        )
    return buffer.getvalue()


def export_tasks_csv(snapshots: Iterable[RunSnapshot]) -> str:
    """Task-level CSV, one row per task per run."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(TASK_COLUMNS)
    for snapshot in snapshots:
        for task in snapshot.tasks.values():
            writer.writerow(
                [
                    snapshot.run_id,
                    snapshot.workflow,
                    task.name,
                    task.state.value,
                    task.attempts,
                    task.retries,
                    _stamp(task.started_at),
                    _stamp(task.finished_at),
                    _number(task.duration),
                    "yes" if task.timed_out else "no",
                    _detail(task),
                ]
            )
    return buffer.getvalue()


def export_events_jsonl(events: Iterable[Event]) -> str:
    """Events as JSONL, exactly as the file store would write them."""

    return dump_lines(event.as_dict() for event in events)


def _stamp(value: float | None) -> str:
    return format_iso(value) if value is not None else ""


def _number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def _detail(task: Any) -> str:
    if task.error:
        return str(task.error.get("message", "failed"))
    if task.skip_reason:
        return str(task.skip_reason)
    return ""
