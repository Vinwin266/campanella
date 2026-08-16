"""A directory of JSONL files, one per run.

The on-disk format is deliberately boring: ``<root>/<run-id>.jsonl``, one
canonical JSON object per line, appended in sequence order.  It can be read with
``cat``, tailed while a run is in flight, and diffed between two runs to see
exactly where they diverged.

Writes go through a temporary file and a rename when a whole log is rewritten,
so a crash mid-write leaves the previous log intact rather than a half file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from ..errors import EventLogCorrupt, StoreError, UnknownRunError
from ..util.jsonio import canonical_dumps
from .events import Event
from .log import EventLog
from .replay import RunSnapshot, replay

__all__ = ["FileStore", "LOG_SUFFIX"]

LOG_SUFFIX = ".jsonl"

#: Run ids come from :class:`campanile.util.ids.IdFactory` and are already
#: filesystem-safe, but a store may be pointed at ids from elsewhere.
_FORBIDDEN = set('/\\:*?"<>|')


class FileStore:
    """A :class:`~campanile.store.memory.RunStore` backed by a directory."""

    __slots__ = ("root", "_cache", "_flushed")

    def __init__(self, root: str | os.PathLike[str], *, create: bool = True) -> None:
        self.root = Path(root)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.is_dir():
            raise StoreError(f"{self.root} is not a directory")
        self._cache: dict[str, EventLog] = {}
        self._flushed: dict[str, int] = {}

    # -- paths ---------------------------------------------------------------

    def path_for(self, run_id: str) -> Path:
        """Where ``run_id``'s log lives."""

        if not run_id or _FORBIDDEN & set(run_id):
            raise StoreError(f"run id {run_id!r} cannot be used as a file name")
        return self.root / f"{run_id}{LOG_SUFFIX}"

    # -- RunStore ------------------------------------------------------------

    def create(self, run_id: str) -> EventLog:
        path = self.path_for(run_id)
        if run_id in self._cache or path.exists():
            raise StoreError(f"run {run_id!r} already exists", run_id=run_id)
        log = EventLog(run_id)
        self._cache[run_id] = log
        self._flushed[run_id] = 0
        path.write_text("", encoding="utf-8")
        return log

    def log(self, run_id: str) -> EventLog:
        cached = self._cache.get(run_id)
        if cached is not None:
            return cached
        path = self.path_for(run_id)
        if not path.exists():
            raise UnknownRunError(run_id)
        log = EventLog.from_jsonl(path.read_text(encoding="utf-8"), run_id=run_id)
        self._cache[run_id] = log
        self._flushed[run_id] = len(log)
        return log

    def has(self, run_id: str) -> bool:
        if run_id in self._cache:
            return True
        try:
            return self.path_for(run_id).exists()
        except StoreError:
            return False

    def run_ids(self) -> tuple[str, ...]:
        """Every run id on disk, sorted.

        Run ids embed a timestamp, so sorted order is chronological order.
        """

        found = {
            path.name[: -len(LOG_SUFFIX)]
            for path in self.root.glob(f"*{LOG_SUFFIX}")
            if path.is_file()
        }
        found.update(self._cache)
        return tuple(sorted(found))

    def flush(self, run_id: str) -> None:
        """Append everything written since the last flush."""

        log = self._cache.get(run_id)
        if log is None:
            return
        path = self.path_for(run_id)
        already = self._flushed.get(run_id, 0)
        pending = log.events[already:]
        if not pending:
            return
        with path.open("a", encoding="utf-8") as handle:
            for event in pending:
                handle.write(canonical_dumps(event.as_dict()) + "\n")
        self._flushed[run_id] = len(log)

    def flush_all(self) -> None:
        for run_id in list(self._cache):
            self.flush(run_id)

    # -- conveniences --------------------------------------------------------

    def __len__(self) -> int:
        return len(self.run_ids())

    def __contains__(self, run_id: object) -> bool:
        return isinstance(run_id, str) and self.has(run_id)

    def __iter__(self) -> Iterator[EventLog]:
        for run_id in self.run_ids():
            yield self.log(run_id)

    def snapshot(self, run_id: str) -> RunSnapshot:
        return replay(self.log(run_id))

    def snapshots(self) -> tuple[RunSnapshot, ...]:
        return tuple(replay(log) for log in self if len(log))

    def write(self, log: EventLog) -> Path:
        """Rewrite a whole log atomically, replacing whatever was there."""

        path = self.path_for(log.run_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(log.to_jsonl(), encoding="utf-8")
        temporary.replace(path)
        self._cache[log.run_id] = log
        self._flushed[log.run_id] = len(log)
        return path

    def read_events(self, run_id: str) -> tuple[Event, ...]:
        return self.log(run_id).events

    def delete(self, run_id: str) -> None:
        path = self.path_for(run_id)
        existed = path.exists()
        if not existed and run_id not in self._cache:
            raise UnknownRunError(run_id)
        if existed:
            path.unlink()
        self._cache.pop(run_id, None)
        self._flushed.pop(run_id, None)

    def verify(self, run_id: str) -> None:
        """Re-read a log from disk and check it parses and replays.

        Raises :class:`EventLogCorrupt` if the file cannot be read back, which
        is what ``campanile store check`` reports.
        """

        path = self.path_for(run_id)
        if not path.exists():
            raise UnknownRunError(run_id)
        log = EventLog.from_jsonl(path.read_text(encoding="utf-8"), run_id=run_id)
        if not len(log):
            raise EventLogCorrupt(f"run {run_id!r} has an empty log")
        replay(log)

    def evict(self) -> None:
        """Drop the in-memory cache, keeping whatever has been flushed."""

        self.flush_all()
        self._cache.clear()
        self._flushed.clear()

    def __repr__(self) -> str:
        return f"FileStore({str(self.root)!r})"
