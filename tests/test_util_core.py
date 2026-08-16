"""Clocks, ordered collections, priority queues, canonical JSON and ids."""

from __future__ import annotations

import unittest

from campanile.errors import EventLogCorrupt
from campanile.util.clock import Deadline, ManualClock, OffsetClock, SystemClock
from campanile.util.ids import (
    IdFactory,
    content_hash,
    format_iso,
    format_timestamp,
    parse_timestamp,
    short_hash,
    slugify,
)
from campanile.util.jsonio import canonical_dumps, canonical_loads, json_equal, load_lines
from campanile.util.ordered import OrderedSet, group_by, index_map, partition, stable_unique
from campanile.util.priority import PriorityQueue


class ManualClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(1000.0)

    def test_starts_at_origin(self) -> None:
        self.assertEqual(self.clock.now(), 1000.0)
        self.assertEqual(self.clock.monotonic(), 0.0)

    def test_advance_moves_both_readings(self) -> None:
        self.clock.advance(5.0)
        self.assertEqual(self.clock.now(), 1005.0)
        self.assertEqual(self.clock.monotonic(), 5.0)

    def test_advance_zero_is_not_counted(self) -> None:
        self.clock.advance(0.0)
        self.assertEqual(self.clock.advances, 0)

    def test_advance_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            self.clock.advance(-1.0)

    def test_advance_to_rejects_going_backwards(self) -> None:
        with self.assertRaises(ValueError):
            self.clock.advance_to(999.0)

    def test_advance_to_is_idempotent(self) -> None:
        self.clock.advance_to(1000.0)
        self.assertEqual(self.clock.advances, 0)

    def test_reset(self) -> None:
        self.clock.advance(10.0)
        self.clock.reset()
        self.assertEqual(self.clock.now(), 1000.0)
        self.assertEqual(self.clock.advances, 0)

    def test_ticking_callable(self) -> None:
        tick = self.clock.ticking(2.0)
        self.assertEqual(tick(), 1002.0)
        self.assertEqual(tick(), 1004.0)


class SystemClockTests(unittest.TestCase):
    def test_readings_are_non_decreasing(self) -> None:
        clock = SystemClock()
        first = clock.monotonic()
        second = clock.monotonic()
        self.assertGreaterEqual(second, first)
        self.assertGreater(clock.now(), 0.0)


class OffsetClockTests(unittest.TestCase):
    def test_shifts_wall_clock_only(self) -> None:
        inner = ManualClock(500.0)
        skewed = OffsetClock(inner, 30.0)
        self.assertEqual(skewed.now(), 530.0)
        self.assertEqual(skewed.monotonic(), inner.monotonic())
        inner.advance(10.0)
        self.assertEqual(skewed.now(), 540.0)


class DeadlineTests(unittest.TestCase):
    def test_infinite_deadline_never_expires(self) -> None:
        clock = ManualClock(0.0)
        deadline = Deadline(clock, None)
        self.assertTrue(deadline.infinite)
        self.assertIsNone(deadline.remaining())
        clock.advance(10_000.0)
        self.assertFalse(deadline.expired())

    def test_expires_exactly_at_the_budget(self) -> None:
        clock = ManualClock(0.0)
        deadline = Deadline(clock, 10.0)
        clock.advance(9.999)
        self.assertFalse(deadline.expired())
        clock.advance(0.001)
        self.assertTrue(deadline.expired())

    def test_remaining_counts_down(self) -> None:
        clock = ManualClock(0.0)
        deadline = Deadline(clock, 10.0)
        clock.advance(4.0)
        self.assertAlmostEqual(deadline.remaining(), 6.0)
        self.assertAlmostEqual(deadline.elapsed(), 4.0)


class OrderedSetTests(unittest.TestCase):
    def test_preserves_insertion_order(self) -> None:
        items = OrderedSet(["b", "a", "c"])
        self.assertEqual(list(items), ["b", "a", "c"])

    def test_re_adding_does_not_reorder(self) -> None:
        items = OrderedSet(["b", "a"])
        items.add("b")
        self.assertEqual(list(items), ["b", "a"])

    def test_set_algebra_keeps_left_order(self) -> None:
        left = OrderedSet(["c", "a", "b"])
        right = OrderedSet(["b", "d"])
        self.assertEqual(list(left.union(right)), ["c", "a", "b", "d"])
        self.assertEqual(list(left.intersection(right)), ["b"])
        self.assertEqual(list(left.difference(right)), ["c", "a"])
        self.assertEqual(list(left.symmetric_difference(right)), ["c", "a", "d"])

    def test_pop_is_fifo(self) -> None:
        items = OrderedSet(["x", "y"])
        self.assertEqual(items.pop(), "x")
        self.assertEqual(items.pop(), "y")
        with self.assertRaises(KeyError):
            items.pop()

    def test_equality_ignores_order_but_ordered_eq_does_not(self) -> None:
        self.assertEqual(OrderedSet(["a", "b"]), OrderedSet(["b", "a"]))
        self.assertFalse(OrderedSet(["a", "b"]).ordered_eq(["b", "a"]))

    def test_remove_raises_for_absent(self) -> None:
        with self.assertRaises(KeyError):
            OrderedSet(["a"]).remove("z")

    def test_first_of_empty_is_none(self) -> None:
        self.assertIsNone(OrderedSet().first())


class SequenceHelperTests(unittest.TestCase):
    def test_stable_unique(self) -> None:
        self.assertEqual(stable_unique([3, 1, 3, 2, 1]), [3, 1, 2])

    def test_stable_unique_with_key(self) -> None:
        self.assertEqual(stable_unique(["aa", "ab", "b"], key=lambda s: s[0]), ["aa", "b"])

    def test_index_map_keeps_first_position(self) -> None:
        self.assertEqual(index_map(["a", "b", "a"]), {"a": 0, "b": 1})

    def test_group_by_preserves_order(self) -> None:
        grouped = group_by([1, 2, 3, 4], lambda value: value % 2)
        self.assertEqual(grouped, {1: [1, 3], 0: [2, 4]})

    def test_partition(self) -> None:
        evens, odds = partition([1, 2, 3, 4], lambda value: value % 2 == 0)
        self.assertEqual((evens, odds), ([2, 4], [1, 3]))


class PriorityQueueTests(unittest.TestCase):
    def test_equal_priorities_keep_arrival_order(self) -> None:
        queue: PriorityQueue[str] = PriorityQueue()
        queue.push("b", 1)
        queue.push("a", 1)
        queue.push("c", 0)
        self.assertEqual(queue.drain(), ["c", "b", "a"])

    def test_tuple_priorities_compare_elementwise(self) -> None:
        queue: PriorityQueue[str] = PriorityQueue()
        queue.push("late", (1, 0))
        queue.push("early", (0, 99))
        self.assertEqual(queue.pop(), "early")

    def test_iteration_does_not_consume(self) -> None:
        queue: PriorityQueue[int] = PriorityQueue()
        for value in (3, 1, 2):
            queue.push(value, value)
        self.assertEqual(list(queue), [1, 2, 3])
        self.assertEqual(len(queue), 3)

    def test_pop_and_peek_of_empty_raise(self) -> None:
        queue: PriorityQueue[int] = PriorityQueue()
        with self.assertRaises(IndexError):
            queue.pop()
        with self.assertRaises(IndexError):
            queue.peek()


class CanonicalJsonTests(unittest.TestCase):
    def test_keys_are_sorted_and_compact(self) -> None:
        self.assertEqual(canonical_dumps({"b": 1, "a": [2, 3]}), '{"a":[2,3],"b":1}')

    def test_sets_become_sorted_lists(self) -> None:
        self.assertEqual(canonical_dumps({"s": {3, 1, 2}}), '{"s":[1,2,3]}')

    def test_non_finite_floats_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_dumps({"x": float("nan")})

    def test_unknown_objects_fall_back_to_str(self) -> None:
        class Thing:
            def __repr__(self) -> str:
                return "<thing>"

        self.assertEqual(canonical_dumps({"t": Thing()}), '{"t":"<thing>"}')

    def test_objects_with_as_dict_are_expanded(self) -> None:
        class Thing:
            def as_dict(self) -> dict[str, int]:
                return {"n": 1}

        self.assertEqual(canonical_dumps(Thing()), '{"n":1}')

    def test_bad_json_becomes_a_store_error(self) -> None:
        with self.assertRaises(EventLogCorrupt):
            canonical_loads("{not json", line=7)

    def test_load_lines_skips_blanks_and_reports_line_numbers(self) -> None:
        self.assertEqual(load_lines('{"a":1}\n\n{"b":2}\n'), [{"a": 1}, {"b": 2}])
        with self.assertRaises(EventLogCorrupt) as caught:
            load_lines('{"a":1}\nnope\n')
        self.assertEqual(caught.exception.line, 2)

    def test_json_equal_ignores_key_order(self) -> None:
        self.assertTrue(json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}))


class IdentifierTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Daily Rollup (EU)"), "daily-rollup-eu")
        self.assertEqual(slugify("!!!"), "x")
        self.assertEqual(slugify("a" * 100, max_length=10), "a" * 10)

    def test_short_hash_is_stable_and_bounded(self) -> None:
        self.assertEqual(short_hash("x"), short_hash("x"))
        self.assertNotEqual(short_hash("x"), short_hash("y"))
        self.assertEqual(len(short_hash("x", length=16)), 16)
        with self.assertRaises(ValueError):
            short_hash("x", length=2)

    def test_content_hash_ignores_key_order(self) -> None:
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_timestamp_round_trip(self) -> None:
        stamp = format_timestamp(1772323200.0)
        self.assertEqual(stamp, "20260301T000000Z")
        self.assertEqual(parse_timestamp(stamp), 1772323200.0)
        self.assertEqual(format_iso(1772323200.0), "2026-03-01T00:00:00Z")
        self.assertEqual(parse_timestamp("2026-03-01T00:00:00Z"), 1772323200.0)

    def test_parse_timestamp_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            parse_timestamp("yesterday")

    def test_run_ids_increment_within_the_same_instant(self) -> None:
        factory = IdFactory()
        first = factory.run_id("daily rollup", 1772323200.0)
        second = factory.run_id("daily rollup", 1772323200.0)
        self.assertEqual(first, "daily-rollup-20260301T000000Z-0001")
        self.assertEqual(second, "daily-rollup-20260301T000000Z-0002")

    def test_counters_are_scoped_by_name_and_instant(self) -> None:
        factory = IdFactory()
        factory.run_id("a", 0.0)
        self.assertTrue(factory.run_id("b", 0.0).endswith("-0001"))
        self.assertTrue(factory.run_id("a", 3600.0).endswith("-0001"))

    def test_run_ids_sort_chronologically(self) -> None:
        factory = IdFactory()
        early = factory.run_id("flow", 1772323200.0)
        late = factory.run_id("flow", 1772409600.0)
        self.assertLess(early, late)

    def test_attempt_ids(self) -> None:
        factory = IdFactory()
        self.assertEqual(factory.attempt_id("run-1", "extract", 2), "run-1/extract#2")
        with self.assertRaises(ValueError):
            factory.attempt_id("run-1", "extract", 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
