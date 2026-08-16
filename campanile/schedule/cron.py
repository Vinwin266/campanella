"""Cron expressions.

Five fields, in the usual order::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-6 (Sunday is 0, and 7 also means Sunday)

Each field accepts ``*``, a single value, a range ``a-b``, a step ``*/n`` or
``a-b/n``, and comma-separated lists of any of those.  Months and weekdays may be
written by name (``jan``, ``mon``).  ``?`` is accepted in the two day fields as a
synonym for ``*``.

The day-of-month / day-of-week interaction follows the traditional rule: when
both fields are restricted the expression fires if *either* matches, and when
only one is restricted it alone decides.  That is what makes ``0 0 1 * mon``
mean "the first of the month, and every Monday".

Everything is computed in UTC.  A workflow that must fire at 09:00 local time
somewhere is expected to say so in UTC; carrying a timezone database around
would make the engine's behaviour depend on the host, which is exactly what this
package refuses to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Sequence

from ..errors import CronSyntaxError

__all__ = ["CronExpression", "CronField", "parse_cron", "MACROS", "FIELD_NAMES"]

FIELD_NAMES: tuple[str, ...] = ("minute", "hour", "day", "month", "weekday")

_FIELD_BOUNDS: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_WEEKDAY_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

#: Convenience spellings understood in place of a full expression.
MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: Upper bound on the search for the next fire.  Four years covers every leap
#: year pattern, so an expression that finds nothing in that window matches
#: nothing at all -- ``0 0 30 2 *`` for instance.
_SEARCH_LIMIT_DAYS = 4 * 366


@dataclass(frozen=True)
class CronField:
    """One parsed field: the set of values it matches, plus whether it is ``*``."""

    name: str
    values: frozenset[int]
    unrestricted: bool

    def matches(self, value: int) -> bool:
        return value in self.values

    def sorted_values(self) -> tuple[int, ...]:
        return tuple(sorted(self.values))

    def describe(self) -> str:
        if self.unrestricted:
            return "*"
        return ",".join(str(value) for value in self.sorted_values())


@dataclass(frozen=True)
class CronExpression:
    """A parsed cron expression."""

    source: str
    minute: CronField
    hour: CronField
    day: CronField
    month: CronField
    weekday: CronField

    # -- construction --------------------------------------------------------

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        return parse_cron(expression)

    @property
    def fields(self) -> tuple[CronField, ...]:
        return (self.minute, self.hour, self.day, self.month, self.weekday)

    # -- matching ------------------------------------------------------------

    def matches(self, timestamp: float) -> bool:
        """Whether the expression fires at ``timestamp`` (to the minute)."""

        moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        return self._matches_moment(moment)

    def _matches_moment(self, moment: datetime) -> bool:
        if not self.minute.matches(moment.minute):
            return False
        if not self.hour.matches(moment.hour):
            return False
        if not self.month.matches(moment.month):
            return False
        return self._day_matches(moment)

    def _day_matches(self, moment: datetime) -> bool:
        # ``weekday()`` is Monday=0; cron wants Sunday=0.
        weekday = (moment.weekday() + 1) % 7
        day_ok = self.day.matches(moment.day)
        weekday_ok = self.weekday.matches(weekday)
        if self.day.unrestricted and self.weekday.unrestricted:
            return True
        if self.day.unrestricted:
            return weekday_ok
        if self.weekday.unrestricted:
            return day_ok
        return day_ok or weekday_ok

    # -- searching -----------------------------------------------------------

    def next_after(self, timestamp: float) -> float | None:
        """The first fire strictly after ``timestamp``, or ``None`` if never.

        The search skips whole months, days and hours that cannot match rather
        than stepping a minute at a time, so a once-a-year expression is found
        in a handful of iterations.
        """

        start = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        moment = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        horizon = moment + timedelta(days=_SEARCH_LIMIT_DAYS)

        while moment < horizon:
            if not self.month.matches(moment.month):
                moment = _start_of_next_month(moment)
                continue
            if not self._day_matches(moment):
                moment = _start_of_next_day(moment)
                continue
            if not self.hour.matches(moment.hour):
                moment = _start_of_next_hour(moment)
                continue
            if not self.minute.matches(moment.minute):
                moment = moment + timedelta(minutes=1)
                continue
            return moment.timestamp()
        return None

    def previous_before(self, timestamp: float) -> float | None:
        """The last fire strictly before ``timestamp``, or ``None`` if never."""

        start = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        moment = start.replace(second=0, microsecond=0)
        if moment >= start:
            moment -= timedelta(minutes=1)
        horizon = moment - timedelta(days=_SEARCH_LIMIT_DAYS)

        while moment > horizon:
            if not self.month.matches(moment.month):
                moment = _end_of_previous_month(moment)
                continue
            if not self._day_matches(moment):
                moment = _end_of_previous_day(moment)
                continue
            if not self.hour.matches(moment.hour):
                moment = _end_of_previous_hour(moment)
                continue
            if not self.minute.matches(moment.minute):
                moment = moment - timedelta(minutes=1)
                continue
            return moment.timestamp()
        return None

    def iter_after(self, timestamp: float, count: int) -> Iterator[float]:
        """Yield up to ``count`` successive fires after ``timestamp``."""

        if count < 0:
            raise ValueError("count must not be negative")
        current = float(timestamp)
        for _ in range(count):
            following = self.next_after(current)
            if following is None:
                return
            yield following
            current = following

    def between(self, start: float, end: float) -> list[float]:
        """Every fire in ``(start, end]``."""

        if end < start:
            raise ValueError("end must not precede start")
        fires: list[float] = []
        current = float(start)
        while True:
            following = self.next_after(current)
            if following is None or following > end:
                return fires
            fires.append(following)
            current = following

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        return " ".join(field.describe() for field in self.fields)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "fields": {field.name: field.describe() for field in self.fields},
        }

    def __str__(self) -> str:
        return self.source


def parse_cron(expression: str) -> CronExpression:
    """Parse ``expression`` into a :class:`CronExpression`.

    >>> parse_cron("*/15 9-17 * * mon-fri").describe()
    '0,15,30,45 9,10,11,12,13,14,15,16,17 * * 1,2,3,4,5'
    """

    if not isinstance(expression, str):
        raise CronSyntaxError(str(expression), "expression must be a string")

    source = expression.strip()
    if not source:
        raise CronSyntaxError(expression, "expression is empty")

    expanded = MACROS.get(source.lower(), source)
    if expanded is source and source.startswith("@"):
        known = ", ".join(sorted(MACROS))
        raise CronSyntaxError(expression, f"unknown macro; known macros are {known}")

    parts = expanded.split()
    if len(parts) != 5:
        raise CronSyntaxError(
            expression, f"expected 5 fields, found {len(parts)}"
        )

    fields = [
        _parse_field(expression, name, text, bounds)
        for name, text, bounds in zip(FIELD_NAMES, parts, _FIELD_BOUNDS)
    ]
    return CronExpression(source, *fields)


def _parse_field(
    expression: str, name: str, text: str, bounds: tuple[int, int]
) -> CronField:
    low, high = bounds
    if name in ("day", "weekday") and text == "?":
        text = "*"

    unrestricted = text == "*"
    values: set[int] = set()

    for piece in text.split(","):
        if not piece:
            raise CronSyntaxError(expression, "empty list item", field=name)
        values.update(_parse_piece(expression, name, piece, low, high))

    if name == "weekday":
        # 7 and 0 both mean Sunday; normalise so matching only checks 0.
        if 7 in values:
            values.discard(7)
            values.add(0)

    if not values:
        raise CronSyntaxError(expression, "matches no values", field=name)

    return CronField(name=name, values=frozenset(values), unrestricted=unrestricted)


def _parse_piece(
    expression: str, name: str, piece: str, low: int, high: int
) -> set[int]:
    body, _, step_text = piece.partition("/")
    step = 1
    if _:
        if not step_text:
            raise CronSyntaxError(expression, f"{piece!r} has no step value", field=name)
        if not step_text.isdigit():
            raise CronSyntaxError(
                expression, f"step {step_text!r} is not a number", field=name
            )
        step = int(step_text)
        if step < 1:
            raise CronSyntaxError(expression, "step must be at least 1", field=name)

    if body == "*":
        start, end = low, high
    elif "-" in body[1:]:
        head, _, tail = body.partition("-")
        start = _parse_value(expression, name, head, low, high)
        end = _parse_value(expression, name, tail, low, high)
        if start > end:
            raise CronSyntaxError(
                expression, f"range {body!r} runs backwards", field=name
            )
    else:
        start = _parse_value(expression, name, body, low, high)
        end = high if step > 1 else start

    return set(range(start, end + 1, step))


def _parse_value(expression: str, name: str, text: str, low: int, high: int) -> int:
    token = text.strip().lower()
    if not token:
        raise CronSyntaxError(expression, "empty value", field=name)

    if name == "month" and token in _MONTH_NAMES:
        return _MONTH_NAMES[token]
    if name == "weekday" and token in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[token]

    if not token.isdigit():
        raise CronSyntaxError(expression, f"{text!r} is not a number", field=name)

    value = int(token)
    if not low <= value <= high:
        raise CronSyntaxError(
            expression, f"{value} is outside {low}-{high}", field=name
        )
    return value


# --------------------------------------------------------------------------
# calendar arithmetic used by the search
# --------------------------------------------------------------------------


def _start_of_next_month(moment: datetime) -> datetime:
    if moment.month == 12:
        return moment.replace(
            year=moment.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return moment.replace(
        month=moment.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


def _start_of_next_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _start_of_next_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _end_of_previous_month(moment: datetime) -> datetime:
    first = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first - timedelta(minutes=1)


def _end_of_previous_day(moment: datetime) -> datetime:
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(minutes=1)


def _end_of_previous_hour(moment: datetime) -> datetime:
    top = moment.replace(minute=0, second=0, microsecond=0)
    return top - timedelta(minutes=1)


def field_summary(expression: CronExpression) -> Sequence[tuple[str, str]]:
    """Field-by-field breakdown, for ``campanile schedule explain``."""

    return tuple((field.name, field.describe()) for field in expression.fields)
