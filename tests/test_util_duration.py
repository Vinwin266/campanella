"""Duration parsing and formatting."""

from __future__ import annotations

import unittest

from campanile.util.duration import (
    coerce_duration,
    format_duration,
    humanize_duration,
    parse_duration,
)


class ParseDurationTests(unittest.TestCase):
    def test_bare_number_is_seconds(self) -> None:
        self.assertEqual(parse_duration("30"), 30.0)

    def test_every_unit(self) -> None:
        self.assertEqual(parse_duration("1w"), 604800.0)
        self.assertEqual(parse_duration("1d"), 86400.0)
        self.assertEqual(parse_duration("1h"), 3600.0)
        self.assertEqual(parse_duration("1m"), 60.0)
        self.assertEqual(parse_duration("1s"), 1.0)
        self.assertEqual(parse_duration("1ms"), 0.001)
        self.assertEqual(parse_duration("1us"), 0.000001)

    def test_ms_wins_over_m(self) -> None:
        self.assertEqual(parse_duration("500ms"), 0.5)
        self.assertEqual(parse_duration("500m"), 30000.0)

    def test_components_accumulate(self) -> None:
        self.assertEqual(parse_duration("1h30m"), 5400.0)
        self.assertEqual(parse_duration("1h 30m"), 5400.0)
        self.assertEqual(parse_duration("2d3h4m5s"), 183845.0)

    def test_fractional_values(self) -> None:
        self.assertEqual(parse_duration("1.5h"), 5400.0)
        self.assertAlmostEqual(parse_duration("0.25s"), 0.25)

    def test_signs(self) -> None:
        self.assertEqual(parse_duration("-5m"), -300.0)
        self.assertEqual(parse_duration("+5m"), 300.0)

    def test_whitespace_is_insignificant(self) -> None:
        self.assertEqual(parse_duration("  1h   30m  "), 5400.0)

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration("")
        with self.assertRaises(ValueError):
            parse_duration("   ")

    def test_rejects_garbage(self) -> None:
        for text in ("abc", "1x", "1h!", "--5m", "-", "1_000"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)

    def test_rejects_non_strings(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration(30)  # type: ignore[arg-type]


class CoerceDurationTests(unittest.TestCase):
    def test_none_returns_default(self) -> None:
        self.assertIsNone(coerce_duration(None))
        self.assertEqual(coerce_duration(None, 5.0), 5.0)

    def test_numbers_pass_through(self) -> None:
        self.assertEqual(coerce_duration(90), 90.0)
        self.assertEqual(coerce_duration(1.5), 1.5)

    def test_strings_are_parsed(self) -> None:
        self.assertEqual(coerce_duration("2m"), 120.0)

    def test_booleans_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            coerce_duration(True)

    def test_other_types_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            coerce_duration([1])  # type: ignore[arg-type]


class FormatDurationTests(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(format_duration(0), "0s")

    def test_sub_second(self) -> None:
        self.assertEqual(format_duration(0.25), "250ms")
        self.assertEqual(format_duration(0.0005), "500us")

    def test_compound(self) -> None:
        self.assertEqual(format_duration(5400), "1h30m")
        self.assertEqual(format_duration(90), "1m30s")

    def test_precision_caps_units(self) -> None:
        self.assertEqual(format_duration(3661.5), "1h1m")
        self.assertEqual(format_duration(3661.5, precision=3), "1h1m1.5s")

    def test_negative(self) -> None:
        self.assertEqual(format_duration(-90), "-1m30s")

    def test_rejects_zero_precision(self) -> None:
        with self.assertRaises(ValueError):
            format_duration(10, precision=0)

    def test_round_trips_through_parse(self) -> None:
        for seconds in (1, 59, 60, 61, 3600, 5400, 86400, 604800):
            with self.subTest(seconds=seconds):
                self.assertEqual(parse_duration(format_duration(seconds)), float(seconds))


class HumanizeTests(unittest.TestCase):
    def test_singular_and_plural(self) -> None:
        self.assertEqual(humanize_duration(1), "1 second")
        self.assertEqual(humanize_duration(2), "2 seconds")

    def test_compound(self) -> None:
        self.assertEqual(humanize_duration(5400), "1 hour 30 minutes")

    def test_sub_second(self) -> None:
        self.assertEqual(humanize_duration(0.001), "1 millisecond")

    def test_negative(self) -> None:
        self.assertTrue(humanize_duration(-60).startswith("-"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
