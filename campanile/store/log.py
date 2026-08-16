"""The append-only event log.

One log belongs to one run.  Sequence numbers are dense and start at zero, which
is what lets a reader detect a truncated or reordered file without a checksum
over the whole thing.

The log is the only writer of sequence numbers and timestamps -- callers hand in
a kind and a payload and the log stamps the rest, so two events can never share
a sequence number.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Sequence

from ..errors import EventLogCorrupt
from ..util.ids import content_hash
from ..util.jsonio import dump_lines, load_lines
from .events import Event, EventKind, make_event

__all__ = ["EventLog"]


class EventLog:
    """An ordered, append-only sequence of :class:`Event`."""

    __slots__ = ("run_id", "_events", "_listeners")

    def __init__(self, run_id: str, events: Iterable[Event] = ()) -> None:
        self.run_id = run_id
        self._events: list[Event] = []
        self._listeners: list[Callable[[Event], None]] = []
        for event in events:
            self.extend_one(event)

    # -- writing -------------------------------------------------------------

    def append(
        self,
        kind: EventKind,
        at: float,
        *,
        task: str | None = None,
        attempt: int | None = None,
        **payload: Any,
    ) -> Event:
        """Stamp and append an event, returning it."""

        event = make_event(
            len(self._events),
            at,
            kind,
            self.run_id,
            task=task,
            attempt=attempt,
            **payload,
        )
        self._events.append(event)
        for listener in self._listeners:
            listener(event)
        return event

    def extend_one(self, event: Event) -> Event:
        """Append an already-built event, checking it belongs here.

        Used when reading a log back from storage; the sequence number must
        match the position it lands at, which is what catches a file that lost
        a line in the middle.
        """

        if event.run_id != self.run_id:
            raise EventLogCorrupt(
                f"event belongs to run {event.run_id!r}, not {self.run_id!r}"
            )
        if event.seq != len(self._events):
            raise EventLogCorrupt(
                f"event sequence jumps from {len(self._events)} to {event.seq}"
            )
        self._events.append(event)
        return event

    def extend(self, events: Iterable[Event]) -> None:
        for event in events:
            self.extend_one(event)

    def subscribe(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        """Register a callback invoked on every appended event.

        Returns a function that removes the listener again.  Used by the CLI's
        live output; listeners never see replayed events, only new ones.
        """

        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    # -- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __getitem__(self, index: int) -> Event:
        return self._events[index]

    def __bool__(self) -> bool:
        return bool(self._events)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def next_seq(self) -> int:
        return len(self._events)

    def last(self) -> Event | None:
        return self._events[-1] if self._events else None

    def first(self) -> Event | None:
        return self._events[0] if self._events else None

    def of_kind(self, *kinds: EventKind) -> tuple[Event, ...]:
        wanted = set(kinds)
        return tuple(event for event in self._events if event.kind in wanted)

    def for_task(self, task: str) -> tuple[Event, ...]:
        return tuple(event for event in self._events if event.task == task)

    def tasks(self) -> tuple[str, ...]:
        """Every task named by some event, in first-mention order."""

        seen: list[str] = []
        for event in self._events:
            if event.task and event.task not in seen:
                seen.append(event.task)
        return tuple(seen)

    def since(self, seq: int) -> tuple[Event, ...]:
        """Events with a sequence number at or above ``seq``."""

        return tuple(event for event in self._events if event.seq >= seq)

    def span(self) -> tuple[float, float] | None:
        """``(first_at, last_at)``, or ``None`` for an empty log."""

        if not self._events:
            return None
        return (self._events[0].at, self._events[-1].at)

    def count(self, kind: EventKind) -> int:
        return sum(1 for event in self._events if event.kind is kind)

    # -- serialisation -------------------------------------------------------

    def to_jsonl(self) -> str:
        """Render the whole log as JSONL text."""

        return dump_lines(event.as_dict() for event in self._events)

    @classmethod
    def from_jsonl(cls, text: str, *, run_id: str | None = None) -> "EventLog":
        """Parse JSONL text back into a log.

        The run id is taken from the first event unless one is given, and every
        subsequent event must agree with it.
        """

        rows = load_lines(text)
        if not rows:
            if run_id is None:
                raise EventLogCorrupt("log is empty and no run id was supplied")
            return cls(run_id)

        events = [
            Event.from_dict(row, line=number) for number, row in enumerate(rows, start=1)
        ]
        resolved = run_id if run_id is not None else events[0].run_id
        log = cls(resolved)
        log.extend(events)
        return log

    def checksum(self) -> str:
        """A stable digest of the whole log, for comparing two runs."""

        return content_hash([event.as_dict() for event in self._events], length=16)

    def describe(self) -> str:
        return "\n".join(event.describe() for event in self._events)

    def __repr__(self) -> str:
        return f"EventLog({self.run_id!r}, events={len(self._events)})"


def merge_logs(logs: Sequence[EventLog]) -> list[Event]:
    """Interleave several runs' events by timestamp, then run id, then sequence.

    Used by reports that cover more than one run.  The ordering is total, so the
    result does not depend on the order the logs were passed in.
    """

    merged: list[Event] = []
    for log in logs:
        merged.extend(log.events)
    merged.sort(key=lambda event: (event.at, event.run_id, event.seq))
    return merged
