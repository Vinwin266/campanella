"""Graph algorithms over the task dependency graph.

Everything here takes the same shape: ``nodes`` is an ordered sequence of names
and ``edges`` maps each node to the nodes it depends on (its *upstreams*).  All
results are deterministic -- where a genuine choice exists it is resolved by the
position of the node in ``nodes``, never by hash order.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .ordered import OrderedSet, index_map

__all__ = [
    "topological_sort",
    "find_cycle",
    "ancestors",
    "descendants",
    "roots",
    "leaves",
    "depth_levels",
    "transitive_reduction",
    "longest_path_length",
    "reachable_from",
]


def _normalise(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    """Return a dense upstream map restricted to known nodes."""

    known = set(nodes)
    dense: dict[str, list[str]] = {}
    for node in nodes:
        parents = [parent for parent in upstreams.get(node, ()) if parent in known]
        dense[node] = list(dict.fromkeys(parents))
    return dense


def _downstream_map(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    dense = _normalise(nodes, upstreams)
    children: dict[str, list[str]] = {node: [] for node in nodes}
    for node in nodes:
        for parent in dense[node]:
            children[parent].append(node)
    return children


def topological_sort(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> list[str]:
    """Return ``nodes`` ordered so every node follows its upstreams.

    Ties are broken by declaration order, so the result is a function of the
    inputs alone.  Raises :class:`ValueError` if the graph has a cycle; callers
    that want the cycle itself should call :func:`find_cycle` first.
    """

    dense = _normalise(nodes, upstreams)
    position = index_map(nodes)
    remaining = {node: len(parents) for node, parents in dense.items()}
    children = _downstream_map(nodes, upstreams)

    ready = sorted((node for node, count in remaining.items() if count == 0), key=position.__getitem__)
    order: list[str] = []

    while ready:
        node = ready.pop(0)
        order.append(node)
        newly_ready: list[str] = []
        for child in children[node]:
            remaining[child] -= 1
            if remaining[child] == 0:
                newly_ready.append(child)
        if newly_ready:
            ready.extend(newly_ready)
            ready.sort(key=position.__getitem__)

    if len(order) != len(nodes):
        raise ValueError("graph contains at least one cycle")
    return order


def find_cycle(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> list[str] | None:
    """Return one cycle as a node list, or ``None`` if the graph is acyclic.

    The returned list starts and ends with the same node, so ``["a", "b", "a"]``
    reads as "a depends on b depends on a".  When several cycles exist the one
    reachable from the earliest declared node is reported, which keeps error
    messages stable as unrelated tasks are added.
    """

    dense = _normalise(nodes, upstreams)
    state: dict[str, int] = {node: 0 for node in nodes}  # 0 unseen, 1 open, 2 done
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for parent in dense[node]:
            if state[parent] == 1:
                start = stack.index(parent)
                return stack[start:] + [parent]
            if state[parent] == 0:
                found = visit(parent)
                if found is not None:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in nodes:
        if state[node] == 0:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


def ancestors(
    node: str, nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> OrderedSet[str]:
    """Every node ``node`` transitively depends on, in declaration order."""

    dense = _normalise(nodes, upstreams)
    if node not in dense:
        return OrderedSet()
    seen: OrderedSet[str] = OrderedSet()
    frontier = list(dense[node])
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(dense.get(current, ()))
    return OrderedSet(item for item in nodes if item in seen)


def descendants(
    node: str, nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> OrderedSet[str]:
    """Every node that transitively depends on ``node``."""

    children = _downstream_map(nodes, upstreams)
    if node not in children:
        return OrderedSet()
    seen: OrderedSet[str] = OrderedSet()
    frontier = list(children[node])
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(children.get(current, ()))
    return OrderedSet(item for item in nodes if item in seen)


def reachable_from(
    starts: Iterable[str], nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> OrderedSet[str]:
    """Union of ``starts`` and all their descendants."""

    result: OrderedSet[str] = OrderedSet()
    for start in starts:
        result.add(start)
        result.update(descendants(start, nodes, upstreams))
    return OrderedSet(item for item in nodes if item in result)


def roots(nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]) -> list[str]:
    """Nodes with no upstreams, in declaration order."""

    dense = _normalise(nodes, upstreams)
    return [node for node in nodes if not dense[node]]


def leaves(nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]) -> list[str]:
    """Nodes nothing depends on, in declaration order."""

    children = _downstream_map(nodes, upstreams)
    return [node for node in nodes if not children[node]]


def depth_levels(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> dict[str, int]:
    """Map each node to its longest distance from a root.

    Level 0 holds the roots; a node's level is one more than the deepest of its
    upstreams.  Reports use this to lay a graph out in columns.
    """

    dense = _normalise(nodes, upstreams)
    levels: dict[str, int] = {}
    for node in topological_sort(nodes, upstreams):
        parents = dense[node]
        levels[node] = 0 if not parents else 1 + max(levels[parent] for parent in parents)
    return levels


def longest_path_length(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> int:
    """Number of edges on the longest chain, zero for an empty or flat graph."""

    if not nodes:
        return 0
    return max(depth_levels(nodes, upstreams).values())


def transitive_reduction(
    nodes: Sequence[str], upstreams: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    """Drop edges implied by a longer path.

    If ``c`` depends on both ``a`` and ``b``, and ``b`` already depends on
    ``a``, then the ``c -> a`` edge carries no information and is removed.  The
    graph must be acyclic.
    """

    dense = _normalise(nodes, upstreams)
    order = topological_sort(nodes, upstreams)
    closure: dict[str, set[str]] = {node: set() for node in nodes}
    reduced: dict[str, list[str]] = {node: [] for node in nodes}

    for node in order:
        parents = dense[node]
        implied: set[str] = set()
        for parent in parents:
            implied |= closure[parent]
        for parent in parents:
            if parent not in implied:
                reduced[node].append(parent)
        closure[node] = implied | set(parents)

    return reduced
