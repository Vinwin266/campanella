"""Where runs are recorded.

The log is the source of truth.  A store keeps logs; :func:`replay` turns a log
back into the state the scheduler had while it was writing it; a query filters
the results.  Nothing here knows how to *run* anything -- the runtime writes,
this layer remembers.
"""

from __future__ import annotations

from .events import (
    TASK_EVENTS,
    TERMINAL_TASK_EVENTS,
    Event,
    EventKind,
    kinds_of,
    make_event,
)
from .filestore import LOG_SUFFIX, FileStore
from .log import EventLog, merge_logs
from .memory import InMemoryStore, RunStore
from .query import SORT_KEYS, RunQuery, sort_snapshots
from .replay import RunSnapshot, TaskSnapshot, replay, replay_log

__all__ = [
    "LOG_SUFFIX",
    "SORT_KEYS",
    "TASK_EVENTS",
    "TERMINAL_TASK_EVENTS",
    "Event",
    "EventKind",
    "EventLog",
    "FileStore",
    "InMemoryStore",
    "RunQuery",
    "RunSnapshot",
    "RunStore",
    "TaskSnapshot",
    "kinds_of",
    "make_event",
    "merge_logs",
    "replay",
    "replay_log",
    "sort_snapshots",
]
