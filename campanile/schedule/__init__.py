"""When workflows run.

Three independent pieces that compose:

* a **trigger** proposes moments (cron, interval, one-shot, never),
* a **calendar** vetoes them (weekends, holidays, opening hours),
* a **timetable** merges many of each into a single ordered stream.

Everything is UTC and everything is pure arithmetic, so ``next_after`` gives the
same answer on any host at any time.
"""

from __future__ import annotations

from .calendar import (
    ALWAYS_OPEN,
    BusinessCalendar,
    TimeWindow,
    merge_calendars,
    parse_date,
    parse_window,
)
from .cron import MACROS, CronExpression, CronField, field_summary, parse_cron
from .interval import (
    CronTrigger,
    IntervalTrigger,
    NeverTrigger,
    OnceTrigger,
    Trigger,
    iter_fires,
    make_trigger,
)
from .timetable import CLOSED_POLICIES, ScheduledFire, ScheduleEntry, Timetable

__all__ = [
    "ALWAYS_OPEN",
    "CLOSED_POLICIES",
    "MACROS",
    "BusinessCalendar",
    "CronExpression",
    "CronField",
    "CronTrigger",
    "IntervalTrigger",
    "NeverTrigger",
    "OnceTrigger",
    "ScheduleEntry",
    "ScheduledFire",
    "TimeWindow",
    "Timetable",
    "Trigger",
    "field_summary",
    "iter_fires",
    "make_trigger",
    "merge_calendars",
    "parse_cron",
    "parse_date",
    "parse_window",
]
