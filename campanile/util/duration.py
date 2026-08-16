"""Parsing and formatting of human-written durations.

Durations appear everywhere in a workflow definition -- timeouts, retry delays,
schedule intervals, SLA budgets -- and they are always written as strings like
``"90s"``, ``"1h30m"`` or ``"2d"``.  Internally everything is a float number of
seconds, so this module is the single boundary where the two representations
meet.

Grammar::

    duration := sign? component+
    component := number unit?
    unit := "w" | "d" | "h" | "m" | "s" | "ms" | "us"

A component without a unit is read as seconds, so ``"30"`` and ``"30s"`` agree.
Components accumulate, so ``"1h30m"`` is 5400 seconds and ``"1h 30m"`` is the
same value -- whitespace between components is insignificant.
"""

from __future__ import annotations

import re
from typing import Union

__all__ = [
    "UNITS",
    "parse_duration",
    "format_duration",
    "coerce_duration",
    "humanize_duration",
]

#: Unit suffix -> multiplier in seconds.  Ordered longest-suffix-first so that
#: ``ms`` is matched before ``m`` when scanning.
UNITS: dict[str, float] = {
    "w": 604800.0,
    "d": 86400.0,
    "h": 3600.0,
    "m": 60.0,
    "s": 1.0,
    "ms": 0.001,
    "us": 0.000001,
}

_COMPONENT = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>us|ms|[wdhms])?")

#: Descending unit table used when rendering.  ``ms`` is handled separately
#: because sub-second values render differently from whole ones.
_RENDER_UNITS: tuple[tuple[str, float], ...] = (
    ("w", 604800.0),
    ("d", 86400.0),
    ("h", 3600.0),
    ("m", 60.0),
    ("s", 1.0),
)

_LONG_NAMES: dict[str, str] = {
    "w": "week",
    "d": "day",
    "h": "hour",
    "m": "minute",
    "s": "second",
}


def parse_duration(text: str) -> float:
    """Parse ``text`` into a float number of seconds.

    >>> parse_duration("90s")
    90.0
    >>> parse_duration("1h30m")
    5400.0
    >>> parse_duration("-5m")
    -300.0

    Raises :class:`ValueError` for anything that is not a well-formed duration.
    An empty string is an error rather than zero; callers that want a default
    should pass one to :func:`coerce_duration`.
    """

    if not isinstance(text, str):
        raise ValueError(f"duration must be a string, got {type(text).__name__}")

    stripped = text.strip()
    if not stripped:
        raise ValueError("duration is empty")

    sign = 1.0
    if stripped[0] in "+-":
        if stripped[0] == "-":
            sign = -1.0
        stripped = stripped[1:].strip()
        if not stripped:
            raise ValueError(f"duration {text!r} has a sign but no value")

    total = 0.0
    position = 0
    seen_component = False
    length = len(stripped)

    while position < length:
        if stripped[position].isspace():
            position += 1
            continue
        match = _COMPONENT.match(stripped, position)
        if match is None:
            raise ValueError(f"duration {text!r} is malformed at offset {position}")
        number = float(match.group("number"))
        unit = match.group("unit") or "s"
        total += number * UNITS[unit]
        position = match.end()
        seen_component = True

    if not seen_component:
        raise ValueError(f"duration {text!r} contains no components")

    return sign * total


def coerce_duration(value: Union[str, int, float, None], default: float | None = None) -> float | None:
    """Accept whatever a workflow author wrote and return seconds.

    Numbers pass through as seconds, strings go to :func:`parse_duration`, and
    ``None`` yields ``default``.  This is the function every model class calls
    when normalising a user-supplied field.
    """

    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("duration must not be a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return parse_duration(value)
    raise ValueError(f"cannot read a duration from {type(value).__name__}")


def format_duration(seconds: float, *, precision: int = 2) -> str:
    """Render ``seconds`` as the most compact string that round-trips.

    >>> format_duration(5400)
    '1h30m'
    >>> format_duration(0.25)
    '250ms'
    >>> format_duration(0)
    '0s'

    ``precision`` caps how many units appear, so a value of 3661.5 seconds
    renders as ``1h1m`` at the default precision and ``1h1m1.5s`` at three.
    """

    if precision < 1:
        raise ValueError("precision must be at least 1")

    if seconds < 0:
        return "-" + format_duration(-seconds, precision=precision)
    if seconds == 0:
        return "0s"

    if seconds < 1.0:
        millis = seconds * 1000.0
        if millis >= 1.0:
            return f"{_trim(millis)}ms"
        return f"{_trim(seconds * 1_000_000.0)}us"

    parts: list[str] = []
    remainder = float(seconds)
    for suffix, size in _RENDER_UNITS:
        if len(parts) == precision:
            break
        if remainder < size:
            continue
        count = int(remainder // size)
        remainder -= count * size
        if suffix == "s" and remainder and len(parts) < precision:
            parts.append(f"{_trim(count + remainder)}s")
            remainder = 0.0
        else:
            parts.append(f"{count}{suffix}")

    if not parts:
        return f"{_trim(remainder)}s"
    return "".join(parts)


def humanize_duration(seconds: float) -> str:
    """Render ``seconds`` in prose, for report headers.

    >>> humanize_duration(5400)
    '1 hour 30 minutes'
    >>> humanize_duration(1)
    '1 second'
    """

    if seconds < 0:
        return "-" + humanize_duration(-seconds)
    if seconds < 1.0:
        millis = round(seconds * 1000.0, 3)
        return f"{_trim(millis)} milliseconds" if millis != 1 else "1 millisecond"

    parts: list[str] = []
    remainder = float(seconds)
    for suffix, size in _RENDER_UNITS:
        if remainder < size:
            continue
        count = int(remainder // size)
        remainder -= count * size
        name = _LONG_NAMES[suffix]
        parts.append(f"{count} {name}" if count == 1 else f"{count} {name}s")

    if not parts:
        return "0 seconds"
    return " ".join(parts)


def _trim(value: float) -> str:
    """Render a float without a trailing ``.0``."""

    rounded = round(value, 6)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"
