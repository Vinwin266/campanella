"""Canonical JSON encoding.

The event log is append-only text, and two runs of the same workflow must
produce byte-identical lines.  ``json.dumps`` is nearly good enough but leaves
three things open: key order, float formatting, and how non-JSON types are
handled.  This module pins all three.

Rules:

* Object keys are always sorted.
* Separators are compact -- no space after ``:`` or ``,``.
* ``NaN`` and infinities are rejected rather than emitted as bare tokens.
* Enums render as their value, sets and tuples as sorted lists, dataclasses as
  objects, and anything with an ``as_dict`` method as whatever that returns.
"""

from __future__ import annotations

import dataclasses
import json
import math
from enum import Enum
from typing import Any, Iterable

from ..errors import EventLogCorrupt

__all__ = [
    "to_jsonable",
    "canonical_dumps",
    "canonical_loads",
    "dump_lines",
    "load_lines",
    "json_equal",
]


def to_jsonable(value: Any) -> Any:
    """Convert ``value`` into something ``json.dumps`` accepts.

    The conversion is total for the types the engine actually stores; anything
    else falls back to ``str`` rather than raising, because an event log entry
    with a slightly lossy payload is better than a run that dies while trying to
    record why it died.
    """

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"cannot encode non-finite float {value!r}")
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return to_jsonable(as_dict())
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return str(value)


def canonical_dumps(value: Any, *, indent: int | None = None) -> str:
    """Serialise ``value`` deterministically.

    >>> canonical_dumps({"b": 1, "a": [2, 3]})
    '{"a":[2,3],"b":1}'

    With ``indent`` the separators relax to the readable form; the ordering
    guarantee is unchanged.  Indented output is for humans only -- the event log
    always uses the compact form.
    """

    payload = to_jsonable(value)
    if indent is None:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return json.dumps(payload, sort_keys=True, indent=indent, allow_nan=False)


def canonical_loads(text: str, *, line: int | None = None) -> Any:
    """Parse JSON, translating failures into :class:`EventLogCorrupt`."""

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EventLogCorrupt(str(exc), line=line) from exc


def dump_lines(values: Iterable[Any]) -> str:
    """Render an iterable as JSONL text, including the trailing newline."""

    return "".join(canonical_dumps(value) + "\n" for value in values)


def load_lines(text: str) -> list[Any]:
    """Parse JSONL text, ignoring blank lines and reporting the line number."""

    parsed: list[Any] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        parsed.append(canonical_loads(stripped, line=number))
    return parsed


def json_equal(left: Any, right: Any) -> bool:
    """Compare two values by their canonical encodings."""

    return canonical_dumps(left) == canonical_dumps(right)
