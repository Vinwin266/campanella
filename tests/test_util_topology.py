"""Graph algorithms over dependency maps."""

from __future__ import annotations

import unittest

from campanile.util.topology import (
    ancestors,
    depth_levels,
    descendants,
    find_cycle,
    leaves,
    longest_path_length,
    reachable_from,
    roots,
    topological_sort,
    transitive_reduction,
)

#: ``a -> b -> d``, ``a -> c -> d``, declared out of order on purpose.
DIAMOND_NODES = ["d", "b", "c", "a"]
DIAMOND_EDGES = {"d": ["b", "c"], "b": ["a"], "c": ["a"]}


class TopologicalSortTests(unittest.TestCase):
    def test_orders_dependencies_first(self) -> None:
        order = topological_sort(DIAMOND_NODES, DIAMOND_EDGES)
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("d"))
        self.assertLess(order.index("c"), order.index("d"))

    def test_ties_break_by_declaration_order(self) -> None:
        order = topological_sort(DIAMOND_NODES, DIAMOND_EDGES)
        self.assertEqual(order, ["a", "b", "c", "d"])

    def test_is_a_function_of_the_inputs(self) -> None:
        first = topological_sort(DIAMOND_NODES, DIAMOND_EDGES)
        second = topological_sort(list(DIAMOND_NODES), dict(DIAMOND_EDGES))
        self.assertEqual(first, second)

    def test_empty_graph(self) -> None:
        self.assertEqual(topological_sort([], {}), [])

    def test_unknown_upstreams_are_ignored(self) -> None:
        self.assertEqual(topological_sort(["a"], {"a": ["ghost"]}), ["a"])

    def test_cycle_raises(self) -> None:
        with self.assertRaises(ValueError):
            topological_sort(["a", "b"], {"a": ["b"], "b": ["a"]})


class CycleTests(unittest.TestCase):
    def test_acyclic_graph_has_no_cycle(self) -> None:
        self.assertIsNone(find_cycle(DIAMOND_NODES, DIAMOND_EDGES))

    def test_two_node_cycle(self) -> None:
        cycle = find_cycle(["a", "b"], {"a": ["b"], "b": ["a"]})
        self.assertIsNotNone(cycle)
        assert cycle is not None
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"a", "b"})

    def test_self_loop(self) -> None:
        cycle = find_cycle(["a"], {"a": ["a"]})
        self.assertEqual(cycle, ["a", "a"])

    def test_longer_cycle_is_reported_in_order(self) -> None:
        cycle = find_cycle(["a", "b", "c"], {"a": ["c"], "c": ["b"], "b": ["a"]})
        assert cycle is not None
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(len(cycle), 4)

    def test_cycle_detection_is_stable(self) -> None:
        nodes = ["a", "b", "c", "d"]
        edges = {"b": ["a"], "c": ["d"], "d": ["c"]}
        self.assertEqual(
            find_cycle(nodes, edges), find_cycle(list(nodes), dict(edges))
        )


class RelationshipTests(unittest.TestCase):
    def test_ancestors_are_transitive_and_ordered(self) -> None:
        self.assertEqual(
            list(ancestors("d", DIAMOND_NODES, DIAMOND_EDGES)), ["b", "c", "a"]
        )

    def test_descendants_are_transitive(self) -> None:
        self.assertEqual(
            set(descendants("a", DIAMOND_NODES, DIAMOND_EDGES)), {"b", "c", "d"}
        )

    def test_leaf_has_no_descendants(self) -> None:
        self.assertEqual(list(descendants("d", DIAMOND_NODES, DIAMOND_EDGES)), [])

    def test_unknown_node_yields_nothing(self) -> None:
        self.assertEqual(list(ancestors("ghost", DIAMOND_NODES, DIAMOND_EDGES)), [])

    def test_roots_and_leaves(self) -> None:
        self.assertEqual(roots(DIAMOND_NODES, DIAMOND_EDGES), ["a"])
        self.assertEqual(leaves(DIAMOND_NODES, DIAMOND_EDGES), ["d"])

    def test_reachable_from_includes_the_starts(self) -> None:
        reached = reachable_from(["b"], DIAMOND_NODES, DIAMOND_EDGES)
        self.assertEqual(set(reached), {"b", "d"})


class LevelTests(unittest.TestCase):
    def test_levels_count_the_longest_chain(self) -> None:
        levels = depth_levels(DIAMOND_NODES, DIAMOND_EDGES)
        self.assertEqual(levels, {"a": 0, "b": 1, "c": 1, "d": 2})

    def test_longest_path_length(self) -> None:
        self.assertEqual(longest_path_length(DIAMOND_NODES, DIAMOND_EDGES), 2)
        self.assertEqual(longest_path_length([], {}), 0)
        self.assertEqual(longest_path_length(["a", "b"], {}), 0)


class TransitiveReductionTests(unittest.TestCase):
    def test_drops_edges_implied_by_a_longer_path(self) -> None:
        nodes = ["a", "b", "c"]
        edges = {"b": ["a"], "c": ["a", "b"]}
        self.assertEqual(transitive_reduction(nodes, edges), {"a": [], "b": ["a"], "c": ["b"]})

    def test_leaves_a_genuine_diamond_alone(self) -> None:
        reduced = transitive_reduction(DIAMOND_NODES, DIAMOND_EDGES)
        self.assertEqual(sorted(reduced["d"]), ["b", "c"])

    def test_is_idempotent(self) -> None:
        once = transitive_reduction(DIAMOND_NODES, DIAMOND_EDGES)
        twice = transitive_reduction(DIAMOND_NODES, once)
        self.assertEqual(once, twice)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
