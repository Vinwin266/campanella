"""An in-process run store.

The default store.  It keeps every run's log in memory, which is what tests and
one-shot CLI invocations want, and it defines the protocol the file-backed store
implements as well.

Stores are deliberately dumb: they hold logs and hand them back.  Every question
more interesting than "give me run X" is answered by replaying, which keeps a
new kind of store from having to reimplement any semantics.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, runtime_checkable

from ..errors import StoreError, UnknownRunError
from .events import Event
from .log import EventLog
from .replay import RunSnapshot, replay

__all__ = ["RunStore", "InMemoryStore"]


@runtime_checkable
class RunStore(Protocol):
    """What the engine needs from a place to keep runs."""

    def create(self, run_id: str) -> EventLog:
        """Start a new log, failing if the run id is taken."""

    def log(self, run_id: str) -> EventLog:
        """The log for ``run_id``, raising :class:`UnknownRunError` if absent."""

    def has(self, run_id: str) -> bool:
        """Whether ``run_id`` exists."""

    def run_ids(self) -> tuple[str, ...]:
        """Every run id, oldest first."""

    def flush(self, run_id: str) -> None:
        """Persist anything buffered for ``run_id``.  A no-op in memory."""


class InMemoryStore:
    """Keeps every log in a dictionary, in creation order."""

    __slots__ = ("_logs",)

    def __init__(self) -> None:
        self._logs: dict[str, EventLog] = {}

    # -- RunStore ------------------------------------------------------------

    def create(self, run_id: str) -> EventLog:
        if run_id in self._logs:
            raise StoreError(f"run {run_id!r} already exists", run_id=run_id)
        log = EventLog(run_id)
        self._logs[run_id] = log
        return log

    def log(self, run_id: str) -> EventLog:
        try:
            return self._logs[run_id]
        except KeyError:
            raise UnknownRunError(run_id) from None

    def has(self, run_id: str) -> bool:
        return run_id in self._logs

    def run_ids(self) -> tuple[str, ...]:
        return tuple(self._logs)

    def flush(self, run_id: str) -> None:
        self.log(run_id)

    # -- conveniences --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._logs)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._logs

    def __iter__(self) -> Iterator[EventLog]:
        return iter(self._logs.values())

    def logs(self) -> tuple[EventLog, ...]:
        return tuple(self._logs.values())

    def snapshot(self, run_id: str) -> RunSnapshot:
        """Replay one run."""

        return replay(self.log(run_id))

    def snapshots(self) -> tuple[RunSnapshot, ...]:
        """Replay every run that has at least one event."""

        return tuple(replay(log) for log in self._logs.values() if len(log))

    def adopt(self, log: EventLog) -> EventLog:
        """Take ownership of an externally built log."""

        if log.run_id in self._logs:
            raise StoreError(f"run {log.run_id!r} already exists", run_id=log.run_id)
        self._logs[log.run_id] = log
        return log

    def ingest(self, events: Iterable[Event]) -> tuple[EventLog, ...]:
        """Add events for one or more runs, creating logs as needed.

        Events for a given run must arrive in sequence order; events for
        different runs may be interleaved.
        """

        touched: list[EventLog] = []
        for event in events:
            log = self._logs.get(event.run_id)
            if log is None:
                log = self.create(event.run_id)
            if log not in touched:
                touched.append(log)
            log.extend_one(event)
        return tuple(touched)

    def delete(self, run_id: str) -> None:
        if run_id not in self._logs:
            raise UnknownRunError(run_id)
        del self._logs[run_id]

    def clear(self) -> None:
        self._logs.clear()

    def __repr__(self) -> str:
        return f"InMemoryStore(runs={len(self._logs)})"
