"""A stable priority queue.

``heapq`` is not stable: two entries with equal priority come out in whatever
order the heap invariant happens to produce.  The dispatcher needs the opposite
guarantee -- equal-priority tasks must run in declaration order -- so every
entry carries a monotonically increasing sequence number that breaks ties.
"""

from __future__ import annotations

import heapq
from typing import Any, Generic, Iterator, Sequence, TypeVar

__all__ = ["PriorityQueue", "PriorityEntry"]

T = TypeVar("T")


class PriorityEntry(Generic[T]):
    """One queued item together with the key that ordered it."""

    __slots__ = ("priority", "sequence", "value")

    def __init__(self, priority: Sequence[Any], sequence: int, value: T) -> None:
        self.priority = tuple(priority)
        self.sequence = sequence
        self.value = value

    def sort_key(self) -> tuple[Any, ...]:
        return self.priority + (self.sequence,)

    def __lt__(self, other: "PriorityEntry[T]") -> bool:
        return self.sort_key() < other.sort_key()

    def __repr__(self) -> str:
        return f"PriorityEntry({self.priority!r}, {self.sequence}, {self.value!r})"


class PriorityQueue(Generic[T]):
    """A min-heap where equal priorities preserve insertion order.

    >>> q = PriorityQueue()
    >>> q.push("b", 1)
    >>> q.push("a", 1)
    >>> q.push("c", 0)
    >>> [q.pop() for _ in range(3)]
    ['c', 'b', 'a']

    Priorities may be a scalar or a tuple; tuples compare element-wise, which is
    how the dispatcher expresses "priority first, then declaration order".
    """

    __slots__ = ("_heap", "_counter")

    def __init__(self) -> None:
        self._heap: list[PriorityEntry[T]] = []
        self._counter = 0

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def __iter__(self) -> Iterator[T]:
        """Iterate in pop order without consuming the queue."""

        for entry in sorted(self._heap):
            yield entry.value

    def push(self, value: T, priority: Any) -> None:
        key = priority if isinstance(priority, tuple) else (priority,)
        entry = PriorityEntry(key, self._counter, value)
        self._counter += 1
        heapq.heappush(self._heap, entry)

    def pop(self) -> T:
        if not self._heap:
            raise IndexError("pop from an empty PriorityQueue")
        return heapq.heappop(self._heap).value

    def pop_entry(self) -> PriorityEntry[T]:
        if not self._heap:
            raise IndexError("pop from an empty PriorityQueue")
        return heapq.heappop(self._heap)

    def peek(self) -> T:
        if not self._heap:
            raise IndexError("peek at an empty PriorityQueue")
        return self._heap[0].value

    def peek_priority(self) -> tuple[Any, ...]:
        if not self._heap:
            raise IndexError("peek at an empty PriorityQueue")
        return self._heap[0].priority

    def drain(self) -> list[T]:
        """Pop everything, in order."""

        return [self.pop() for _ in range(len(self._heap))]

    def clear(self) -> None:
        self._heap.clear()

    def __repr__(self) -> str:
        return f"PriorityQueue({list(self)!r})"
