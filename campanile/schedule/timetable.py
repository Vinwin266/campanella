"""Merging many schedules into one ordered stream of fires.

A timetable holds named entries -- each a workflow, a trigger, a calendar and a
set of parameters -- and answers the only question a scheduler daemon really
asks: *what fires next, and when?*

Ties are broken by declaration order, never by name or hash, so two entries that
fire at the same instant always run in the order they were declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from ..errors import ScheduleError
from ..model.identifiers import validate_task_name, validate_workflow_name
from ..util.ids import format_iso
from .calendar import ALWAYS_OPEN, BusinessCalendar
from .interval import Trigger, make_trigger

__all__ = [
    "ScheduleEntry",
    "ScheduledFire",
    "Timetable",
    "CLOSED_POLICIES",
]

#: What to do when a trigger fires while its calendar is closed.
CLOSED_POLICIES: tuple[str, ...] = ("skip", "defer")

#: Bound on how many closed fires are skipped before giving up.  A calendar that
#: rejects a thousand consecutive fires is misconfigured.
_SKIP_LIMIT = 1000


@dataclass(frozen=True)
class ScheduleEntry:
    """One line of a timetable."""

    name: str
    workflow: str
    trigger: Trigger
    calendar: BusinessCalendar = ALWAYS_OPEN
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    on_closed: str = "skip"

    def __post_init__(self) -> None:
        validate_task_name(self.name)
        validate_workflow_name(self.workflow)
        if self.on_closed not in CLOSED_POLICIES:
            known = ", ".join(CLOSED_POLICIES)
            raise ScheduleError(
                f"schedule {self.name!r} has unknown closed-calendar policy "
                f"{self.on_closed!r}; expected one of {known}"
            )
        object.__setattr__(self, "params", dict(self.params))

    def next_after(self, timestamp: float) -> float | None:
        """The next moment this entry actually starts a run.

        Fires that land while the calendar is closed are either skipped or
        deferred to the next open moment, depending on :attr:`on_closed`.
        """

        if not self.enabled:
            return None

        current = float(timestamp)
        for _ in range(_SKIP_LIMIT):
            candidate = self.trigger.next_after(current)
            if candidate is None:
                return None
            if self.calendar.is_open(candidate):
                return candidate
            if self.on_closed == "defer":
                return self.calendar.align(candidate)
            current = candidate
        raise ScheduleError(
            f"schedule {self.name!r} skipped {_SKIP_LIMIT} consecutive fires; "
            f"calendar {self.calendar.name!r} is probably wrong"
        )

    def describe(self) -> str:
        bits = [f"{self.name} -> {self.workflow}", self.trigger.describe()]
        if not self.calendar.unrestricted:
            bits.append(self.calendar.describe())
            bits.append(f"on closed: {self.on_closed}")
        if self.params:
            rendered = ",".join(f"{key}={value!r}" for key, value in sorted(self.params.items()))
            bits.append(f"params({rendered})")
        if not self.enabled:
            bits.append("[disabled]")
        return " | ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workflow": self.workflow,
            "trigger": getattr(self.trigger, "as_dict", lambda: {"kind": "custom"})(),
            "calendar": self.calendar.as_dict(),
            "params": dict(self.params),
            "enabled": self.enabled,
            "on_closed": self.on_closed,
        }


@dataclass(frozen=True)
class ScheduledFire:
    """One entry firing at one moment."""

    at: float
    entry: ScheduleEntry

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def workflow(self) -> str:
        return self.entry.workflow

    @property
    def params(self) -> Mapping[str, Any]:
        return self.entry.params

    def describe(self) -> str:
        return f"{format_iso(self.at)} {self.entry.name} -> {self.entry.workflow}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "at_iso": format_iso(self.at),
            "schedule": self.entry.name,
            "workflow": self.entry.workflow,
            "params": dict(self.entry.params),
        }


class Timetable:
    """An ordered collection of :class:`ScheduleEntry`."""

    def __init__(self, entries: Sequence[ScheduleEntry] = ()) -> None:
        self._entries: list[ScheduleEntry] = []
        for entry in entries:
            self.add_entry(entry)

    # -- declaration ---------------------------------------------------------

    def add_entry(self, entry: ScheduleEntry) -> ScheduleEntry:
        if any(existing.name == entry.name for existing in self._entries):
            raise ScheduleError(f"schedule {entry.name!r} is declared twice")
        self._entries.append(entry)
        return entry

    def add(
        self,
        name: str,
        workflow: str,
        trigger: object,
        *,
        calendar: BusinessCalendar | None = None,
        params: Mapping[str, Any] | None = None,
        enabled: bool = True,
        on_closed: str = "skip",
    ) -> ScheduleEntry:
        """Declare an entry, building the trigger from a loose spec."""

        return self.add_entry(
            ScheduleEntry(
                name=name,
                workflow=workflow,
                trigger=make_trigger(trigger),
                calendar=calendar or ALWAYS_OPEN,
                params=dict(params or {}),
                enabled=enabled,
                on_closed=on_closed,
            )
        )

    def remove(self, name: str) -> ScheduleEntry:
        for index, entry in enumerate(self._entries):
            if entry.name == name:
                return self._entries.pop(index)
        raise ScheduleError(f"no schedule named {name!r}")

    def enable(self, name: str, enabled: bool = True) -> ScheduleEntry:
        for index, entry in enumerate(self._entries):
            if entry.name == name:
                updated = ScheduleEntry(
                    name=entry.name,
                    workflow=entry.workflow,
                    trigger=entry.trigger,
                    calendar=entry.calendar,
                    params=entry.params,
                    enabled=enabled,
                    on_closed=entry.on_closed,
                )
                self._entries[index] = updated
                return updated
        raise ScheduleError(f"no schedule named {name!r}")

    # -- queries -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[ScheduleEntry]:
        return iter(self._entries)

    def __contains__(self, name: object) -> bool:
        return any(entry.name == name for entry in self._entries)

    @property
    def entries(self) -> tuple[ScheduleEntry, ...]:
        return tuple(self._entries)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)

    def entry(self, name: str) -> ScheduleEntry:
        for entry in self._entries:
            if entry.name == name:
                return entry
        raise ScheduleError(f"no schedule named {name!r}")

    def for_workflow(self, workflow: str) -> tuple[ScheduleEntry, ...]:
        return tuple(entry for entry in self._entries if entry.workflow == workflow)

    def next_fire(self, after: float) -> ScheduledFire | None:
        """The earliest fire strictly after ``after``, or ``None``."""

        best: ScheduledFire | None = None
        for entry in self._entries:
            moment = entry.next_after(after)
            if moment is None:
                continue
            if best is None or moment < best.at:
                best = ScheduledFire(at=moment, entry=entry)
        return best

    def upcoming(self, after: float, count: int) -> list[ScheduledFire]:
        """The next ``count`` fires across every entry, in order.

        Entries that fire at the same instant appear in declaration order.
        """

        if count < 0:
            raise ValueError("count must not be negative")

        cursors: dict[str, float] = {entry.name: float(after) for entry in self._entries}
        pending: dict[str, float] = {}
        fires: list[ScheduledFire] = []

        for entry in self._entries:
            moment = entry.next_after(cursors[entry.name])
            if moment is not None:
                pending[entry.name] = moment

        while pending and len(fires) < count:
            chosen: ScheduleEntry | None = None
            chosen_at = 0.0
            for entry in self._entries:
                moment = pending.get(entry.name)
                if moment is None:
                    continue
                if chosen is None or moment < chosen_at:
                    chosen, chosen_at = entry, moment
            if chosen is None:  # pragma: no cover - pending was non-empty
                break
            fires.append(ScheduledFire(at=chosen_at, entry=chosen))
            following = chosen.next_after(chosen_at)
            if following is None:
                pending.pop(chosen.name, None)
            else:
                pending[chosen.name] = following

        return fires

    def between(self, start: float, end: float, *, limit: int = 10_000) -> list[ScheduledFire]:
        """Every fire in ``(start, end]``, across every entry, in order.

        Two entries firing at the same instant both appear, in declaration
        order; ``limit`` caps the result so a one-second interval over a year
        cannot exhaust memory.
        """

        if end < start:
            raise ValueError("end must not precede start")

        pending: dict[str, float] = {}
        for entry in self._entries:
            moment = entry.next_after(float(start))
            if moment is not None and moment <= end:
                pending[entry.name] = moment

        collected: list[ScheduledFire] = []
        while pending and len(collected) < limit:
            chosen: ScheduleEntry | None = None
            chosen_at = 0.0
            for entry in self._entries:
                moment = pending.get(entry.name)
                if moment is None:
                    continue
                if chosen is None or moment < chosen_at:
                    chosen, chosen_at = entry, moment
            if chosen is None:  # pragma: no cover - pending was non-empty
                break
            collected.append(ScheduledFire(at=chosen_at, entry=chosen))
            following = chosen.next_after(chosen_at)
            if following is None or following > end:
                pending.pop(chosen.name, None)
            else:
                pending[chosen.name] = following
        return collected

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        if not self._entries:
            return "timetable: no schedules"
        return "\n".join(entry.describe() for entry in self._entries)

    def as_dict(self) -> dict[str, Any]:
        return {"schedules": [entry.as_dict() for entry in self._entries]}

    def __repr__(self) -> str:
        return f"Timetable(entries={len(self._entries)})"
