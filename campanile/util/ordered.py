"""Order-preserving collections.

Determinism is the whole point of this engine, and the commonest way to lose it
is to iterate a ``set``.  These containers keep insertion order while still
offering set-like membership and algebra, so code can be written naturally
without reintroducing hash-order dependence.
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable, Iterator, MutableSet, TypeVar

__all__ = [
    "OrderedSet",
    "stable_unique",
    "index_map",
    "group_by",
    "partition",
]

T = TypeVar("T", bound=Hashable)
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class OrderedSet(MutableSet[T]):
    """A set that iterates in insertion order.

    >>> s = OrderedSet(["b", "a", "c"])
    >>> list(s)
    ['b', 'a', 'c']
    >>> s.add("a")
    >>> list(s)
    ['b', 'a', 'c']

    Equality follows set semantics -- two ordered sets with the same members in
    different orders compare equal -- because callers reason about membership.
    Use :meth:`ordered_eq` when the order itself matters.
    """

    __slots__ = ("_items",)

    def __init__(self, iterable: Iterable[T] = ()) -> None:
        self._items: dict[T, None] = {}
        for item in iterable:
            self._items[item] = None

    # -- MutableSet protocol -------------------------------------------------

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def add(self, value: T) -> None:
        self._items[value] = None

    def discard(self, value: T) -> None:
        self._items.pop(value, None)

    # -- conveniences --------------------------------------------------------

    def update(self, iterable: Iterable[T]) -> None:
        for item in iterable:
            self._items[item] = None

    def remove(self, value: T) -> None:
        if value not in self._items:
            raise KeyError(value)
        del self._items[value]

    def pop(self) -> T:
        """Remove and return the first item, FIFO rather than arbitrary."""

        if not self._items:
            raise KeyError("pop from an empty OrderedSet")
        first = next(iter(self._items))
        del self._items[first]
        return first

    def first(self) -> T | None:
        for item in self._items:
            return item
        return None

    def union(self, other: Iterable[T]) -> "OrderedSet[T]":
        merged = OrderedSet(self)
        merged.update(other)
        return merged

    def intersection(self, other: Iterable[T]) -> "OrderedSet[T]":
        keep = set(other)
        return OrderedSet(item for item in self if item in keep)

    def difference(self, other: Iterable[T]) -> "OrderedSet[T]":
        drop = set(other)
        return OrderedSet(item for item in self if item not in drop)

    def symmetric_difference(self, other: Iterable[T]) -> "OrderedSet[T]":
        other_set = OrderedSet(other)
        result = self.difference(other_set)
        result.update(other_set.difference(self))
        return result

    def sorted(self, key: Callable[[T], object] | None = None) -> "OrderedSet[T]":
        """Return a copy in sorted order."""

        return OrderedSet(sorted(self._items, key=key))  # type: ignore[arg-type]

    def ordered_eq(self, other: Iterable[T]) -> bool:
        """Compare membership *and* order."""

        return list(self) == list(other)

    def __or__(self, other: Iterable[T]) -> "OrderedSet[T]":  # type: ignore[override]
        return self.union(other)

    def __and__(self, other: Iterable[T]) -> "OrderedSet[T]":  # type: ignore[override]
        return self.intersection(other)

    def __sub__(self, other: Iterable[T]) -> "OrderedSet[T]":  # type: ignore[override]
        return self.difference(other)

    def __repr__(self) -> str:
        return f"OrderedSet({list(self._items)!r})"


def stable_unique(items: Iterable[T], key: Callable[[T], Hashable] | None = None) -> list[T]:
    """Drop duplicates, keeping the first occurrence of each.

    >>> stable_unique([3, 1, 3, 2, 1])
    [3, 1, 2]
    """

    seen: set[Hashable] = set()
    result: list[T] = []
    for item in items:
        marker = key(item) if key is not None else item
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def index_map(items: Iterable[T]) -> dict[T, int]:
    """Map each item to its position, first occurrence winning.

    The result is the canonical tie-breaker used by the dispatcher: two tasks
    that are equal on every scheduling criterion fall back to declaration order.
    """

    positions: dict[T, int] = {}
    for position, item in enumerate(items):
        positions.setdefault(item, position)
    return positions


def group_by(items: Iterable[V], key: Callable[[V], K]) -> dict[K, list[V]]:
    """Group ``items`` by ``key``, preserving encounter order within groups."""

    groups: dict[K, list[V]] = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def partition(items: Iterable[V], predicate: Callable[[V], bool]) -> tuple[list[V], list[V]]:
    """Split ``items`` into ``(matching, rest)`` in one pass."""

    matching: list[V] = []
    rest: list[V] = []
    for item in items:
        (matching if predicate(item) else rest).append(item)
    return matching, rest
