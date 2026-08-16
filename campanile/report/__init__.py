"""Turning finished runs into something a person or a tool can read.

Everything in this package is a pure function of a replayed run.  Nothing here
touches the scheduler, the clock or the file system, which is why a report of a
run from last month is byte-identical to the report printed when it finished.
"""

from __future__ import annotations

from .export import (
    RUN_COLUMNS,
    TASK_COLUMNS,
    export_events_jsonl,
    export_run_json,
    export_runs_csv,
    export_runs_json,
    export_tasks_csv,
)
from .metrics import (
    DURATION_BUCKETS,
    Counter,
    Histogram,
    RunMetrics,
    aggregate,
    collect,
    count_events,
    state_shares,
)
from .render import Column, Table, render_bars, render_kv
from .summary import STATE_MARKS, RunSummary, summarise, summarise_many
from .timeline import Timeline, TimelineRow, critical_path, render_timeline

__all__ = [
    "DURATION_BUCKETS",
    "RUN_COLUMNS",
    "STATE_MARKS",
    "TASK_COLUMNS",
    "Column",
    "Counter",
    "Histogram",
    "RunMetrics",
    "RunSummary",
    "Table",
    "Timeline",
    "TimelineRow",
    "aggregate",
    "collect",
    "count_events",
    "critical_path",
    "export_events_jsonl",
    "export_run_json",
    "export_runs_csv",
    "export_runs_json",
    "export_tasks_csv",
    "render_bars",
    "render_kv",
    "render_timeline",
    "state_shares",
    "summarise",
    "summarise_many",
]
