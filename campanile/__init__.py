"""campanile -- a deterministic workflow orchestration engine.

Describe a graph of tasks, hand it to an engine, get a run back::

    from campanile import Engine, WorkflowBuilder

    builder = WorkflowBuilder("nightly")
    extract = builder.task("extract", extract_fn)
    load = builder.task("load", load_fn)
    extract >> load

    result = Engine().run(builder.build())
    assert result.succeeded

The engine retries what fails, respects the resource pools you declared, gates
edges on conditions, and writes an append-only event log describing exactly what
happened.  Given a manual clock, the same workflow produces the same events, in
the same order, with the same timestamps, every time.

Layering, bottom to top::

    util -> model -> expr/schedule -> dsl -> store -> runtime -> report -> cli

Each layer imports only from the ones beneath it.
"""

from __future__ import annotations

from .dsl.builder import TaskGroup, TaskHandle, WorkflowBuilder
from .dsl.decorators import TaskRegistry, build_workflow, task
from .dsl.validate import assert_valid, validate
from .errors import (
    CampanileError,
    ConfigurationError,
    CycleError,
    ExecutionError,
    Issue,
    StoreError,
    TaskFailed,
    TaskTimeout,
    ValidationError,
    WorkflowDefinitionError,
)
from .model.policy import Backoff, FailurePolicy, RetryPolicy, TriggerRule
from .model.resource import ResourcePool
from .model.state import RunState, TaskState
from .model.task import TaskSpec
from .model.workflow import Edge, Workflow
from .report.summary import summarise
from .report.timeline import render_timeline
from .runtime.cancel import CancellationToken
from .runtime.context import TaskContext
from .runtime.engine import Engine
from .runtime.executor import InlineExecutor, RecordingExecutor
from .runtime.result import RunResult, TaskResult
from .runtime.scheduler import Scheduler
from .schedule.calendar import BusinessCalendar
from .schedule.cron import CronExpression, parse_cron
from .schedule.interval import IntervalTrigger, make_trigger
from .schedule.timetable import ScheduleEntry, Timetable
from .store.events import Event, EventKind
from .store.filestore import FileStore
from .store.log import EventLog
from .store.memory import InMemoryStore
from .store.replay import RunSnapshot, replay
from .util.clock import ManualClock, SystemClock
from .version import VERSION, VERSION_INFO

__all__ = [
    "VERSION",
    "VERSION_INFO",
    "Backoff",
    "BusinessCalendar",
    "CampanileError",
    "CancellationToken",
    "ConfigurationError",
    "CronExpression",
    "CycleError",
    "Edge",
    "Engine",
    "Event",
    "EventKind",
    "EventLog",
    "ExecutionError",
    "FailurePolicy",
    "FileStore",
    "InMemoryStore",
    "InlineExecutor",
    "IntervalTrigger",
    "Issue",
    "ManualClock",
    "RecordingExecutor",
    "ResourcePool",
    "RetryPolicy",
    "RunResult",
    "RunSnapshot",
    "RunState",
    "ScheduleEntry",
    "Scheduler",
    "StoreError",
    "SystemClock",
    "TaskContext",
    "TaskFailed",
    "TaskGroup",
    "TaskHandle",
    "TaskRegistry",
    "TaskResult",
    "TaskSpec",
    "TaskState",
    "TaskTimeout",
    "Timetable",
    "TriggerRule",
    "ValidationError",
    "Workflow",
    "WorkflowBuilder",
    "WorkflowDefinitionError",
    "assert_valid",
    "build_workflow",
    "make_trigger",
    "parse_cron",
    "render_timeline",
    "replay",
    "summarise",
    "task",
    "validate",
]
