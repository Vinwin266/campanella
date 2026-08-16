"""Filtering and sorting runs.

Once a store holds more than a handful of runs, ``campanile runs`` needs to answer
questions like "failed runs of the nightly workflow since Monday, worst first".
A :class:`RunQuery` is that question as a value, so the CLI can build one from
its flags and the tests can build one directly.

Queries operate on :class:`~campanile.store.replay.RunSnapshot` objects, never on
raw events, which keeps every filter expressible in one line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..errors import StoreError
from ..model.state import RunState, TaskState
from .replay import RunSnapshot

__all__ = ["RunQuery", "SORT_KEYS", "sort_snapshots"]

#: Field names ``campanile runs --sort`` accepts.
SORT_KEYS: tuple[str, ...] = ("started", "finished", "duration", "workflow", "run_id", "tasks")


@dataclass
class RunQuery:
    """A filter over runs."""

    workflow: str | None = None
    states: tuple[RunState, ...] = ()
    since: float | None = None
    until: float | None = None
    contains_task: str | None = None
    task_state: TaskState | None = None
    min_duration: float | None = None
    failed_only: bool = False
    limit: int | None = None
    sort_by: str = "started"
    descending: bool = False
    extra: list[Callable[[RunSnapshot], bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sort_by not in SORT_KEYS:
            known = ", ".join(SORT_KEYS)
            raise StoreError(f"unknown sort key {self.sort_by!r}; expected one of {known}")
        if self.limit is not None and self.limit < 1:
            raise StoreError("limit must be at least 1 when given")
        if self.since is not None and self.until is not None and self.until < self.since:
            raise StoreError("until must not precede since")

    # -- building ------------------------------------------------------------

    def where(self, predicate: Callable[[RunSnapshot], bool]) -> "RunQuery":
        """Add an arbitrary predicate.  Returns ``self`` so calls chain."""

        self.extra.append(predicate)
        return self

    def for_workflow(self, name: str) -> "RunQuery":
        self.workflow = name
        return self

    def in_state(self, *states: RunState) -> "RunQuery":
        self.states = tuple(states)
        return self

    # -- applying ------------------------------------------------------------

    def matches(self, snapshot: RunSnapshot) -> bool:
        """Whether one snapshot passes every filter."""

        if self.workflow is not None and snapshot.workflow != self.workflow:
            return False
        if self.states and snapshot.state not in self.states:
            return False
        if self.failed_only and not snapshot.failed:
            return False
        if self.since is not None:
            if snapshot.started_at is None or snapshot.started_at < self.since:
                return False
        if self.until is not None:
            if snapshot.started_at is None or snapshot.started_at > self.until:
                return False
        if self.contains_task is not None and self.contains_task not in snapshot.tasks:
            return False
        if self.task_state is not None:
            if not any(
                task.state is self.task_state for task in snapshot.tasks.values()
            ):
                return False
        if self.min_duration is not None:
            duration = snapshot.duration
            if duration is None or duration < self.min_duration:
                return False
        return all(predicate(snapshot) for predicate in self.extra)

    def apply(self, snapshots: Iterable[RunSnapshot]) -> list[RunSnapshot]:
        """Filter, sort and truncate."""

        kept = [snapshot for snapshot in snapshots if self.matches(snapshot)]
        ordered = sort_snapshots(kept, self.sort_by, descending=self.descending)
        if self.limit is not None:
            return ordered[: self.limit]
        return ordered

    def describe(self) -> str:
        bits: list[str] = []
        if self.workflow:
            bits.append(f"workflow={self.workflow}")
        if self.states:
            bits.append("state in " + ",".join(state.value for state in self.states))
        if self.failed_only:
            bits.append("failed only")
        if self.contains_task:
            bits.append(f"has task {self.contains_task}")
        if self.task_state:
            bits.append(f"some task {self.task_state}")
        if self.min_duration is not None:
            bits.append(f"duration >= {self.min_duration}s")
        if self.limit:
            bits.append(f"limit {self.limit}")
        order = "desc" if self.descending else "asc"
        bits.append(f"sorted by {self.sort_by} {order}")
        return ", ".join(bits)


def sort_snapshots(
    snapshots: Sequence[RunSnapshot], key: str = "started", *, descending: bool = False
) -> list[RunSnapshot]:
    """Sort snapshots deterministically by one of :data:`SORT_KEYS`.

    Runs missing the sort field -- an unfinished run has no duration -- sort
    last regardless of direction, so a partial run never displaces a complete
    one at the top of a report.
    """

    if key not in SORT_KEYS:
        known = ", ".join(SORT_KEYS)
        raise StoreError(f"unknown sort key {key!r}; expected one of {known}")

    def sort_key(snapshot: RunSnapshot) -> tuple[Any, ...]:
        value = _field(snapshot, key)
        missing = value is None
        if missing:
            value = _missing_placeholder(key)
        return (1 if missing else 0, value, snapshot.run_id)

    ordered = sorted(snapshots, key=sort_key)
    if descending:
        present = [item for item in ordered if _field(item, key) is not None]
        absent = [item for item in ordered if _field(item, key) is None]
        present.reverse()
        return present + absent
    return ordered


def _field(snapshot: RunSnapshot, key: str) -> Any:
    if key == "started":
        return snapshot.started_at
    if key == "finished":
        return snapshot.finished_at
    if key == "duration":
        return snapshot.duration
    if key == "workflow":
        return snapshot.workflow
    if key == "run_id":
        return snapshot.run_id
    return len(snapshot.tasks)


def _missing_placeholder(key: str) -> Any:
    return "" if key in ("workflow", "run_id") else 0.0
