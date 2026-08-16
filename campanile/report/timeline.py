"""A text timeline of a run.

Reading a run's event log tells you what happened; a timeline tells you *when*,
and that is usually the question -- which task held the critical path, where the
pool contention was, how long the retry backoff actually cost.

The rendering is a fixed-width character grid.  Each task gets a row, the run's
span is mapped onto the available columns, and the task's active period is drawn
in it.  Rows keep declaration order rather than start order, so the same
workflow always renders the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..model.state import TaskState
from ..store.replay import RunSnapshot, TaskSnapshot
from ..util.duration import format_duration
from ..util.ids import format_iso
from ..util.text import pad
from .render import Table

__all__ = ["TimelineRow", "Timeline", "render_timeline", "critical_path"]

#: Character used to draw each state's span.
_MARKS: dict[TaskState, str] = {
    TaskState.SUCCEEDED: "=",
    TaskState.FAILED: "!",
    TaskState.RETRY_WAIT: "~",
    TaskState.RUNNING: "*",
    TaskState.SKIPPED: ".",
    TaskState.UPSTREAM_FAILED: "x",
    TaskState.CANCELLED: "/",
}

_DEFAULT_MARK = "-"


@dataclass(frozen=True)
class TimelineRow:
    """One task's placement on the timeline."""

    name: str
    state: TaskState
    start: float | None
    finish: float | None
    attempts: int

    @property
    def duration(self) -> float | None:
        if self.start is None or self.finish is None:
            return None
        return max(0.0, self.finish - self.start)

    @property
    def placed(self) -> bool:
        return self.start is not None and self.finish is not None


@dataclass
class Timeline:
    """Rows plus the span they are drawn against."""

    rows: tuple[TimelineRow, ...]
    origin: float
    span: float

    @classmethod
    def of(cls, snapshot: RunSnapshot) -> "Timeline":
        """Build a timeline from a replayed run."""

        rows = tuple(
            TimelineRow(
                name=task.name,
                state=task.state,
                start=task.started_at,
                finish=task.finished_at,
                attempts=task.attempts,
            )
            for task in snapshot.tasks.values()
        )
        starts = [row.start for row in rows if row.start is not None]
        finishes = [row.finish for row in rows if row.finish is not None]
        origin = snapshot.started_at if snapshot.started_at is not None else min(starts, default=0.0)
        end = snapshot.finished_at if snapshot.finished_at is not None else max(finishes, default=origin)
        return cls(rows=rows, origin=float(origin), span=max(0.0, float(end) - float(origin)))

    def bar(self, row: TimelineRow, width: int) -> str:
        """Draw one row's span into ``width`` characters.

        A task with no duration still gets one character, because "it happened
        instantly" and "it did not happen" must not look the same.
        """

        if width < 1:
            return ""
        if not row.placed:
            return " " * width

        if self.span <= 0:
            return _MARKS.get(row.state, _DEFAULT_MARK) + " " * (width - 1)

        start_column = int((row.start - self.origin) / self.span * (width - 1))
        finish_column = int((row.finish - self.origin) / self.span * (width - 1))
        start_column = max(0, min(width - 1, start_column))
        finish_column = max(start_column, min(width - 1, finish_column))

        mark = _MARKS.get(row.state, _DEFAULT_MARK)
        cells = [" "] * width
        for column in range(start_column, finish_column + 1):
            cells[column] = mark
        return "".join(cells)

    def render(self, width: int = 48) -> str:
        """Render the whole timeline as a table."""

        if not self.rows:
            return "no tasks\n"

        table = Table.of("task", "timeline", "duration", "attempts")
        table.align("left", "left", "right", "right")
        for row in self.rows:
            table.add(
                row.name,
                self.bar(row, width),
                format_duration(row.duration) if row.duration is not None else "-",
                row.attempts or "-",
            )

        header = (
            f"{format_iso(self.origin)} .. "
            f"{format_iso(self.origin + self.span)} "
            f"({format_duration(self.span)})"
        )
        return header + "\n" + table.render() + "\n"

    def legend(self) -> str:
        """Explain the marks actually used in this timeline."""

        used = {row.state for row in self.rows if row.placed}
        parts = [
            f"{_MARKS.get(state, _DEFAULT_MARK)} {state}"
            for state in sorted(used, key=lambda item: item.value)
        ]
        return "  ".join(parts)

    def busiest_moment(self, samples: int = 64) -> tuple[float, int]:
        """The sampled instant with the most tasks running, and how many.

        Sampling rather than exact interval arithmetic keeps this cheap and is
        plenty for a report; ties resolve to the earliest sample.
        """

        if self.span <= 0 or not self.rows:
            return (self.origin, sum(1 for row in self.rows if row.placed))
        best_at = self.origin
        best_count = -1
        for index in range(max(1, samples)):
            moment = self.origin + self.span * index / max(1, samples - 1)
            count = sum(
                1
                for row in self.rows
                if row.placed and row.start <= moment <= row.finish
            )
            if count > best_count:
                best_at, best_count = moment, count
        return (best_at, best_count)


def render_timeline(snapshot: RunSnapshot, width: int = 48) -> str:
    """Render ``snapshot`` as a timeline with a legend."""

    timeline = Timeline.of(snapshot)
    legend = timeline.legend()
    body = timeline.render(width)
    return body + (f"\n{legend}\n" if legend else "")


def critical_path(snapshot: RunSnapshot, upstreams: dict[str, Sequence[str]]) -> list[str]:
    """The chain of tasks that determined the run's total duration.

    Walks backwards from the task that finished last, each step choosing the
    upstream that finished latest.  Tasks that never ran are skipped, so a
    branch that was pruned by a condition cannot appear on the path.
    """

    ran = {
        name: task
        for name, task in snapshot.tasks.items()
        if task.finished_at is not None and task.started_at is not None
    }
    if not ran:
        return []

    last = max(ran.values(), key=lambda task: (task.finished_at or 0.0, task.name))
    path = [last.name]
    current: TaskSnapshot | None = last

    while current is not None:
        candidates = [
            ran[name]
            for name in upstreams.get(current.name, ())
            if name in ran
        ]
        if not candidates:
            break
        previous = max(candidates, key=lambda task: (task.finished_at or 0.0, task.name))
        path.append(previous.name)
        current = previous

    path.reverse()
    return path


def render_gantt_header(width: int, span: float) -> str:
    """A ruler line for a timeline of ``span`` seconds across ``width`` columns."""

    if width < 8:
        return ""
    ticks = 4
    labels = []
    for index in range(ticks + 1):
        fraction = index / ticks
        labels.append(format_duration(span * fraction))
    cell = max(1, width // ticks)
    return "".join(pad(label, cell) for label in labels[:ticks]) + labels[-1]
