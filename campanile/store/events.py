"""The event vocabulary.

A run is defined by its events.  Nothing else is authoritative: the scheduler's
in-memory state is a cache of what the log already says, reports are folds over
the log, and :mod:`campanile.store.replay` reconstructs the whole run from nothing
but the events.

Events are immutable, ordered by a per-run sequence number, and carry a
timestamp taken from the run's clock rather than the host's.  Two runs of the
same workflow on a manual clock therefore produce byte-identical logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from ..errors import EventLogCorrupt
from ..util.ids import format_iso

__all__ = ["EventKind", "Event", "TASK_EVENTS", "TERMINAL_TASK_EVENTS", "make_event"]


class EventKind(Enum):
    """Every kind of thing that can happen during a run."""

    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_CANCELLED = "run_cancelled"

    TASK_SCHEDULED = "task_scheduled"
    TASK_STARTED = "task_started"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TASK_TIMED_OUT = "task_timed_out"
    TASK_SKIPPED = "task_skipped"
    TASK_UPSTREAM_FAILED = "task_upstream_failed"
    TASK_CANCELLED = "task_cancelled"

    CONDITION_EVALUATED = "condition_evaluated"
    RESOURCE_ACQUIRED = "resource_acquired"
    RESOURCE_RELEASED = "resource_released"
    RESOURCE_BLOCKED = "resource_blocked"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Events that concern one task rather than the run as a whole.
TASK_EVENTS: frozenset[EventKind] = frozenset(
    {
        EventKind.TASK_SCHEDULED,
        EventKind.TASK_STARTED,
        EventKind.TASK_SUCCEEDED,
        EventKind.TASK_FAILED,
        EventKind.TASK_RETRY_SCHEDULED,
        EventKind.TASK_TIMED_OUT,
        EventKind.TASK_SKIPPED,
        EventKind.TASK_UPSTREAM_FAILED,
        EventKind.TASK_CANCELLED,
        EventKind.CONDITION_EVALUATED,
        EventKind.RESOURCE_ACQUIRED,
        EventKind.RESOURCE_RELEASED,
        EventKind.RESOURCE_BLOCKED,
    }
)

#: Events after which a task will not change state again.
TERMINAL_TASK_EVENTS: frozenset[EventKind] = frozenset(
    {
        EventKind.TASK_SUCCEEDED,
        EventKind.TASK_FAILED,
        EventKind.TASK_SKIPPED,
        EventKind.TASK_UPSTREAM_FAILED,
        EventKind.TASK_CANCELLED,
    }
)


@dataclass(frozen=True)
class Event:
    """One thing that happened, at one moment, in one run."""

    seq: int
    at: float
    kind: EventKind
    run_id: str
    task: str | None = None
    attempt: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError("event sequence numbers start at 0")
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt numbers start at 1")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def is_task_event(self) -> bool:
        return self.kind in TASK_EVENTS

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_TASK_EVENTS

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def describe(self) -> str:
        """One line, as the CLI's ``campanile events`` prints it."""

        head = f"{format_iso(self.at)} #{self.seq:04d} {self.kind}"
        if self.task:
            head += f" {self.task}"
            if self.attempt is not None:
                head += f"#{self.attempt}"
        detail = self._detail()
        return f"{head} {detail}".rstrip()

    def _detail(self) -> str:
        interesting = {
            key: value
            for key, value in sorted(self.payload.items())
            if key not in ("run_id",) and value is not None
        }
        if not interesting:
            return ""
        rendered = " ".join(f"{key}={value!r}" for key, value in interesting.items())
        return f"({rendered})"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind.value,
            "run_id": self.run_id,
        }
        if self.task is not None:
            payload["task"] = self.task
        if self.attempt is not None:
            payload["attempt"] = self.attempt
        if self.payload:
            payload["payload"] = dict(self.payload)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, line: int | None = None) -> "Event":
        """Rebuild an event from its stored form, rejecting anything malformed."""

        if not isinstance(data, Mapping):
            raise EventLogCorrupt(
                f"expected an object, found {type(data).__name__}", line=line
            )
        for required in ("seq", "at", "kind", "run_id"):
            if required not in data:
                raise EventLogCorrupt(f"event is missing {required!r}", line=line)
        try:
            kind = EventKind(data["kind"])
        except ValueError as exc:
            raise EventLogCorrupt(
                f"unknown event kind {data['kind']!r}", line=line
            ) from exc

        seq = data["seq"]
        at = data["at"]
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise EventLogCorrupt(f"seq must be an integer, found {seq!r}", line=line)
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise EventLogCorrupt(f"at must be a number, found {at!r}", line=line)

        attempt = data.get("attempt")
        if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int)):
            raise EventLogCorrupt(
                f"attempt must be an integer, found {attempt!r}", line=line
            )

        payload = data.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise EventLogCorrupt(
                f"payload must be an object, found {type(payload).__name__}", line=line
            )

        return cls(
            seq=seq,
            at=float(at),
            kind=kind,
            run_id=str(data["run_id"]),
            task=data.get("task"),
            attempt=attempt,
            payload=dict(payload),
        )


def make_event(
    seq: int,
    at: float,
    kind: EventKind,
    run_id: str,
    *,
    task: str | None = None,
    attempt: int | None = None,
    **payload: Any,
) -> Event:
    """Build an event with its payload given as keyword arguments."""

    return Event(
        seq=seq,
        at=at,
        kind=kind,
        run_id=run_id,
        task=task,
        attempt=attempt,
        payload={key: value for key, value in payload.items() if value is not None},
    )


def kinds_of(events: Iterable[Event]) -> tuple[EventKind, ...]:
    """The kinds of ``events``, in order.  Convenient in assertions."""

    return tuple(event.kind for event in events)
