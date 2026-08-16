"""Business calendars.

A trigger says when a workflow *wants* to run; a calendar says when it is
*allowed* to.  The two compose: the timetable asks the trigger for a candidate
moment and then asks the calendar to move it to the next permitted one.

A calendar is three independent restrictions:

* weekend days -- weekdays on which nothing runs,
* holidays -- specific dates on which nothing runs,
* windows -- times of day during which running is permitted.

All of it is UTC, and all of it is pure arithmetic over ``datetime``; there is
no timezone database and no host locale involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..errors import ScheduleError

__all__ = [
    "TimeWindow",
    "BusinessCalendar",
    "ALWAYS_OPEN",
    "parse_window",
    "parse_date",
]

_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

_WEEKDAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_MINUTES_PER_DAY = 24 * 60

#: How many days ahead :meth:`BusinessCalendar.align` and
#: :meth:`BusinessCalendar.next_business_day` will look before giving up.  A
#: calendar that is shut for more than a year is a definition bug.
_SEARCH_DAYS = 400


@dataclass(frozen=True)
class TimeWindow:
    """A half-open range of minutes within a day, ``[start, end)``.

    A window whose end is at or before its start wraps past midnight, so
    ``22:00-02:00`` covers the four hours around it.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        for value, label in ((self.start, "start"), (self.end, "end")):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ScheduleError(f"window {label} must be an integer minute")
            if not 0 <= value <= _MINUTES_PER_DAY:
                raise ScheduleError(
                    f"window {label} must be between 0 and {_MINUTES_PER_DAY}"
                )
        if self.start == self.end:
            raise ScheduleError("window must not be empty")

    @classmethod
    def parse(cls, text: str) -> "TimeWindow":
        return parse_window(text)

    @property
    def wraps(self) -> bool:
        return self.end < self.start

    def contains(self, minute_of_day: int) -> bool:
        if self.wraps:
            return minute_of_day >= self.start or minute_of_day < self.end
        return self.start <= minute_of_day < self.end

    def next_open_minute(self, minute_of_day: int) -> int | None:
        """Minutes to wait from ``minute_of_day`` until this window opens.

        ``None`` when the window is already open.  The result is always within
        one day, since every window recurs daily.
        """

        if self.contains(minute_of_day):
            return None
        return (self.start - minute_of_day) % _MINUTES_PER_DAY

    def describe(self) -> str:
        return f"{_render_minute(self.start)}-{_render_minute(self.end)}"

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end}


def parse_window(text: str) -> TimeWindow:
    """Parse ``"09:00-17:30"`` into a :class:`TimeWindow`.

    >>> parse_window("09:00-17:30").describe()
    '09:00-17:30'
    """

    if not isinstance(text, str):
        raise ScheduleError(f"window must be a string, got {type(text).__name__}")
    match = _WINDOW_RE.match(text.strip())
    if match is None:
        raise ScheduleError(f"cannot read a time window from {text!r}; expected HH:MM-HH:MM")
    start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    for hour in (start_hour, end_hour):
        if hour > 24:
            raise ScheduleError(f"hour {hour} is out of range in window {text!r}")
    for minute in (start_minute, end_minute):
        if minute > 59:
            raise ScheduleError(f"minute {minute} is out of range in window {text!r}")
    return TimeWindow(start_hour * 60 + start_minute, end_hour * 60 + end_minute)


def parse_date(text: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` date."""

    if isinstance(text, date):
        return text
    if not isinstance(text, str) or _DATE_RE.match(text.strip()) is None:
        raise ScheduleError(f"cannot read a date from {text!r}; expected YYYY-MM-DD")
    year, month, day = (int(part) for part in text.strip().split("-"))
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ScheduleError(f"{text!r} is not a real date: {exc}") from exc


@dataclass(frozen=True)
class BusinessCalendar:
    """When a workflow is permitted to run."""

    name: str = "always"
    weekend: frozenset[int] = frozenset()
    holidays: frozenset[date] = frozenset()
    windows: tuple[TimeWindow, ...] = ()

    @classmethod
    def of(
        cls,
        name: str = "business",
        *,
        weekend: Iterable[int | str] = ("sat", "sun"),
        holidays: Iterable[str | date] = (),
        windows: Iterable[str | TimeWindow] = (),
    ) -> "BusinessCalendar":
        """Build from the loose forms a definition uses."""

        return cls(
            name=name,
            weekend=frozenset(_weekday_number(day) for day in weekend),
            holidays=frozenset(parse_date(day) for day in holidays),
            windows=tuple(
                window if isinstance(window, TimeWindow) else parse_window(window)
                for window in windows
            ),
        )

    # -- queries -------------------------------------------------------------

    @property
    def unrestricted(self) -> bool:
        return not self.weekend and not self.holidays and not self.windows

    def is_business_day(self, timestamp: float) -> bool:
        moment = _moment(timestamp)
        if moment.weekday() in self.weekend:
            return False
        return moment.date() not in self.holidays

    def is_open(self, timestamp: float) -> bool:
        """Whether ``timestamp`` falls on a business day inside some window."""

        if not self.is_business_day(timestamp):
            return False
        if not self.windows:
            return True
        moment = _moment(timestamp)
        minute_of_day = moment.hour * 60 + moment.minute
        return any(window.contains(minute_of_day) for window in self.windows)

    def align(self, timestamp: float) -> float:
        """Move ``timestamp`` forward to the first open moment at or after it.

        Returns the input unchanged when it is already open.  The search is
        bounded: a calendar that closes every day of the year raises rather than
        looping, because that is a definition bug, not a schedule.
        """

        original = float(timestamp)
        if self.is_open(original):
            return original

        moment = _moment(timestamp).replace(second=0, microsecond=0)

        # Each iteration either settles on a moment today or moves to the next
        # day, so the search costs one step per closed day rather than one per
        # closed minute.
        for _ in range(_SEARCH_DAYS):
            if not self.is_business_day(moment.timestamp()):
                moment = _next_midnight(moment)
                continue
            if not self.windows:
                return moment.timestamp()

            minute_of_day = moment.hour * 60 + moment.minute
            waits = [window.next_open_minute(minute_of_day) for window in self.windows]
            if any(wait is None for wait in waits):
                return moment.timestamp()

            candidate = moment + timedelta(
                minutes=min(wait for wait in waits if wait is not None)
            )
            if candidate.date() != moment.date():
                # The next opening is tomorrow, which may itself be closed.
                moment = _next_midnight(moment)
                continue
            return candidate.timestamp()

        raise ScheduleError(
            f"calendar {self.name!r} never opens; check its windows and holidays"
        )

    def next_business_day(self, timestamp: float) -> float:
        """Midnight at the start of the next business day strictly after."""

        moment = _next_midnight(_moment(timestamp))
        for _ in range(_SEARCH_DAYS):
            if self.is_business_day(moment.timestamp()):
                return moment.timestamp()
            moment = _next_midnight(moment)
        raise ScheduleError(f"calendar {self.name!r} has no business days")

    def business_days_between(self, start: float, end: float) -> int:
        """Count business days in ``[start, end)``, by date."""

        if end < start:
            raise ValueError("end must not precede start")
        first = _moment(start).date()
        last = _moment(end).date()
        count = 0
        current = first
        while current < last:
            stamp = datetime(
                current.year, current.month, current.day, tzinfo=timezone.utc
            ).timestamp()
            if self.is_business_day(stamp):
                count += 1
            current = current + timedelta(days=1)
        return count

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        if self.unrestricted:
            return f"{self.name}: always open"
        bits: list[str] = []
        if self.weekend:
            names = [
                name for name, number in sorted(_WEEKDAY_NAMES.items(), key=lambda kv: kv[1])
                if number in self.weekend
            ]
            bits.append("closed " + ",".join(names))
        if self.holidays:
            bits.append(f"{len(self.holidays)} holiday(s)")
        if self.windows:
            bits.append("open " + ",".join(window.describe() for window in self.windows))
        return f"{self.name}: " + "; ".join(bits)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "weekend": sorted(self.weekend),
            "holidays": sorted(day.isoformat() for day in self.holidays),
            "windows": [window.as_dict() for window in self.windows],
        }


#: A calendar with no restrictions at all.
ALWAYS_OPEN = BusinessCalendar()


def _weekday_number(day: int | str) -> int:
    if isinstance(day, bool):
        raise ScheduleError("weekday must be a name or a number")
    if isinstance(day, int):
        if not 0 <= day <= 6:
            raise ScheduleError(f"weekday {day} is outside 0-6 (Monday is 0)")
        return day
    key = str(day).strip().lower()[:3]
    if key not in _WEEKDAY_NAMES:
        raise ScheduleError(f"unknown weekday {day!r}")
    return _WEEKDAY_NAMES[key]


def _moment(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _next_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _render_minute(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def merge_calendars(name: str, calendars: Sequence[BusinessCalendar]) -> BusinessCalendar:
    """A calendar open only where every input is open.

    Weekends and holidays union; windows intersect pairwise.  With no windows
    anywhere the result has none, which means "any time of day".
    """

    weekend: set[int] = set()
    holidays: set[date] = set()
    windows: list[TimeWindow] | None = None

    for source in calendars:
        weekend |= set(source.weekend)
        holidays |= set(source.holidays)
        if not source.windows:
            continue
        if windows is None:
            windows = list(source.windows)
            continue
        windows = _intersect_windows(windows, source.windows)

    return BusinessCalendar(
        name=name,
        weekend=frozenset(weekend),
        holidays=frozenset(holidays),
        windows=tuple(windows or ()),
    )


def _intersect_windows(
    left: Sequence[TimeWindow], right: Sequence[TimeWindow]
) -> list[TimeWindow]:
    minutes = {
        minute
        for minute in range(_MINUTES_PER_DAY)
        if any(window.contains(minute) for window in left)
        and any(window.contains(minute) for window in right)
    }
    return _windows_from_minutes(minutes)


def _windows_from_minutes(minutes: set[int]) -> list[TimeWindow]:
    """Collapse a set of open minutes back into contiguous windows."""

    if not minutes:
        return []
    ordered = sorted(minutes)
    windows: list[TimeWindow] = []
    start = previous = ordered[0]
    for minute in ordered[1:]:
        if minute == previous + 1:
            previous = minute
            continue
        windows.append(TimeWindow(start, previous + 1))
        start = previous = minute
    windows.append(TimeWindow(start, previous + 1))
    return windows
