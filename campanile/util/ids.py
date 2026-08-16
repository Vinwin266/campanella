"""Deterministic identifier generation.

Run ids must be unique, sortable, and reproducible.  ``uuid4`` gives up the last
of those, so ids here are derived from data the caller already has: the workflow
name, the logical time the run was requested, and a counter that disambiguates
runs requested at the same instant.

The resulting id looks like ``daily-rollup-20260311T090000Z-0001``, which sorts
chronologically as text and tells a human what they are looking at.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

__all__ = [
    "slugify",
    "short_hash",
    "content_hash",
    "IdFactory",
    "format_timestamp",
    "parse_timestamp",
]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def slugify(text: str, *, max_length: int = 48) -> str:
    """Reduce ``text`` to lowercase alphanumerics joined by hyphens.

    >>> slugify("Daily Rollup (EU)")
    'daily-rollup-eu'

    An empty result becomes ``"x"`` so an id never has an empty component.
    """

    lowered = text.strip().lower()
    slug = _SLUG_STRIP.sub("-", lowered).strip("-")
    if not slug:
        return "x"
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "x"


def short_hash(text: str, *, length: int = 8) -> str:
    """A stable short digest of ``text``.

    Used where a name would be too long or contains characters an id cannot
    carry.  Truncated SHA-256 is fine here: collisions cost a confusing report,
    not a correctness bug, because ids are also scoped by workflow and time.
    """

    if length < 4 or length > 64:
        raise ValueError("length must be between 4 and 64")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:length]


def content_hash(payload: object, *, length: int = 12) -> str:
    """Hash any JSON-encodable value through its canonical encoding."""

    from .jsonio import canonical_dumps

    return short_hash(canonical_dumps(payload), length=length)


def format_timestamp(epoch_seconds: float) -> str:
    """Render epoch seconds as a compact UTC stamp, ``20260311T090000Z``."""

    moment = datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    return moment.strftime(_TIMESTAMP_FORMAT)


def format_iso(epoch_seconds: float) -> str:
    """Render epoch seconds as ``2026-03-11T09:00:00Z``."""

    moment = datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    return moment.strftime(_ISO_FORMAT)


def parse_timestamp(text: str) -> float:
    """Inverse of :func:`format_timestamp`, also accepting the ISO form."""

    for fmt in (_TIMESTAMP_FORMAT, _ISO_FORMAT):
        try:
            moment = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return moment.timestamp()
    raise ValueError(f"cannot parse timestamp {text!r}")


class IdFactory:
    """Hands out run ids that are unique within a process and reproducible.

    >>> factory = IdFactory()
    >>> factory.run_id("daily rollup", 1772182800.0)
    'daily-rollup-20260227T090000Z-0001'
    >>> factory.run_id("daily rollup", 1772182800.0)
    'daily-rollup-20260227T090000Z-0002'

    The counter is keyed by ``(slug, timestamp)`` so two different workflows, or
    the same workflow at two different times, both start from ``0001``.
    """

    __slots__ = ("_counters", "_width")

    def __init__(self, *, width: int = 4) -> None:
        if width < 1:
            raise ValueError("width must be positive")
        self._counters: dict[tuple[str, str], int] = {}
        self._width = width

    def run_id(self, workflow: str, at: float) -> str:
        slug = slugify(workflow)
        stamp = format_timestamp(at)
        key = (slug, stamp)
        count = self._counters.get(key, 0) + 1
        self._counters[key] = count
        return f"{slug}-{stamp}-{count:0{self._width}d}"

    def attempt_id(self, run_id: str, task: str, attempt: int) -> str:
        """A stable id for one attempt of one task inside a run."""

        if attempt < 1:
            raise ValueError("attempt numbers start at 1")
        return f"{run_id}/{slugify(task, max_length=64)}#{attempt}"

    def reset(self) -> None:
        self._counters.clear()

    def __repr__(self) -> str:
        return f"IdFactory(issued={sum(self._counters.values())})"
