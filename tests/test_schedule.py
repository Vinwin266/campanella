"""Cron expressions, triggers, calendars and timetables."""

from __future__ import annotations

import unittest

from campanile.errors import CronSyntaxError, ScheduleError
from campanile.schedule.calendar import (
    ALWAYS_OPEN,
    BusinessCalendar,
    TimeWindow,
    merge_calendars,
    parse_date,
    parse_window,
)
from campanile.schedule.cron import parse_cron
from campanile.schedule.interval import (
    CronTrigger,
    IntervalTrigger,
    NeverTrigger,
    OnceTrigger,
    make_trigger,
)
from campanile.schedule.timetable import ScheduleEntry, Timetable
from campanile.util.ids import format_iso, parse_timestamp

MARCH = parse_timestamp("2026-03-11T08:59:00Z")  # a Wednesday


def iso(value: float | None) -> str | None:
    return format_iso(value) if value is not None else None


class CronParsingTests(unittest.TestCase):
    def test_wildcards(self) -> None:
        self.assertEqual(parse_cron("* * * * *").describe(), "* * * * *")

    def test_steps_ranges_and_lists(self) -> None:
        expression = parse_cron("*/15 9-17 1,15 * mon-fri")
        self.assertEqual(expression.minute.sorted_values(), (0, 15, 30, 45))
        self.assertEqual(expression.hour.sorted_values(), tuple(range(9, 18)))
        self.assertEqual(expression.day.sorted_values(), (1, 15))
        self.assertEqual(expression.weekday.sorted_values(), (1, 2, 3, 4, 5))

    def test_step_from_a_start_value(self) -> None:
        self.assertEqual(parse_cron("5/20 * * * *").minute.sorted_values(), (5, 25, 45))

    def test_month_and_weekday_names(self) -> None:
        self.assertEqual(parse_cron("0 0 1 jan *").month.sorted_values(), (1,))
        self.assertEqual(parse_cron("0 0 * * SUN").weekday.sorted_values(), (0,))

    def test_seven_is_sunday(self) -> None:
        self.assertEqual(parse_cron("0 0 * * 7").weekday.sorted_values(), (0,))

    def test_question_mark_means_wildcard(self) -> None:
        self.assertTrue(parse_cron("0 0 ? * *").day.unrestricted)

    def test_macros(self) -> None:
        self.assertEqual(parse_cron("@daily").describe(), "0 0 * * *")
        self.assertEqual(parse_cron("@hourly").describe(), "0 * * * *")

    def test_syntax_errors(self) -> None:
        for text in ("", "* * * *", "* * * * * *", "60 * * * *", "abc * * * *",
                     "*/0 * * * *", "5-1 * * * *", "* * * * xyz", "@nope"):
            with self.subTest(text=text), self.assertRaises(CronSyntaxError):
                parse_cron(text)

    def test_error_names_the_field(self) -> None:
        with self.assertRaises(CronSyntaxError) as caught:
            parse_cron("* 99 * * *")
        self.assertEqual(caught.exception.field, "hour")


class CronSearchTests(unittest.TestCase):
    def test_next_fire_is_strictly_after(self) -> None:
        expression = parse_cron("0 9 * * *")
        nine = parse_timestamp("2026-03-11T09:00:00Z")
        self.assertEqual(iso(expression.next_after(nine)), "2026-03-12T09:00:00Z")

    def test_successive_fires(self) -> None:
        expression = parse_cron("*/15 9-17 * * mon-fri")
        fires = [iso(value) for value in expression.iter_after(MARCH, 3)]
        self.assertEqual(
            fires,
            [
                "2026-03-11T09:00:00Z",
                "2026-03-11T09:15:00Z",
                "2026-03-11T09:30:00Z",
            ],
        )

    def test_weekday_restriction_skips_the_weekend(self) -> None:
        expression = parse_cron("0 9 * * mon-fri")
        friday = parse_timestamp("2026-03-13T09:00:00Z")
        self.assertEqual(iso(expression.next_after(friday)), "2026-03-16T09:00:00Z")

    def test_day_and_weekday_are_or_ed_when_both_restricted(self) -> None:
        expression = parse_cron("0 0 1 * mon")
        start = parse_timestamp("2026-03-28T00:00:00Z")
        fires = [iso(value) for value in expression.iter_after(start, 3)]
        self.assertEqual(
            fires,
            [
                "2026-03-30T00:00:00Z",
                "2026-04-01T00:00:00Z",
                "2026-04-06T00:00:00Z",
            ],
        )

    def test_impossible_expression_never_fires(self) -> None:
        self.assertIsNone(parse_cron("0 0 30 2 *").next_after(MARCH))

    def test_leap_day(self) -> None:
        expression = parse_cron("0 0 29 2 *")
        start = parse_timestamp("2026-01-01T00:00:00Z")
        self.assertEqual(iso(expression.next_after(start)), "2028-02-29T00:00:00Z")

    def test_previous_before(self) -> None:
        expression = parse_cron("*/15 9-17 * * mon-fri")
        self.assertEqual(iso(expression.previous_before(MARCH)), "2026-03-10T17:45:00Z")

    def test_matches(self) -> None:
        expression = parse_cron("0 9 * * *")
        self.assertTrue(expression.matches(parse_timestamp("2026-03-11T09:00:00Z")))
        self.assertFalse(expression.matches(parse_timestamp("2026-03-11T09:01:00Z")))

    def test_between_is_half_open_at_the_start(self) -> None:
        expression = parse_cron("0 * * * *")
        start = parse_timestamp("2026-03-11T00:00:00Z")
        end = parse_timestamp("2026-03-11T03:00:00Z")
        self.assertEqual(len(expression.between(start, end)), 3)

    def test_between_rejects_a_backwards_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_cron("@daily").between(10.0, 0.0)


class TriggerTests(unittest.TestCase):
    def test_interval_from_an_anchor(self) -> None:
        trigger = IntervalTrigger(every=60.0, anchor=0.0)
        self.assertEqual(trigger.next_after(0.0), 60.0)
        self.assertEqual(trigger.next_after(59.0), 60.0)
        self.assertEqual(trigger.next_after(60.0), 120.0)

    def test_interval_before_the_anchor(self) -> None:
        trigger = IntervalTrigger(every=60.0, anchor=100.0)
        self.assertEqual(trigger.next_after(0.0), 100.0)

    def test_interval_limit(self) -> None:
        trigger = IntervalTrigger(every=10.0, anchor=0.0, limit=2)
        self.assertEqual(trigger.next_after(0.0), 10.0)
        self.assertIsNone(trigger.next_after(10.0))

    def test_interval_occurrence_index(self) -> None:
        trigger = IntervalTrigger(every=10.0, anchor=0.0)
        self.assertEqual(trigger.occurrence_index(30.0), 3)
        self.assertIsNone(trigger.occurrence_index(35.0))

    def test_interval_must_be_positive(self) -> None:
        with self.assertRaises(ScheduleError):
            IntervalTrigger(every=0.0)

    def test_once_fires_exactly_once(self) -> None:
        trigger = OnceTrigger(at=100.0)
        self.assertEqual(trigger.next_after(0.0), 100.0)
        self.assertIsNone(trigger.next_after(100.0))

    def test_never(self) -> None:
        self.assertIsNone(NeverTrigger().next_after(0.0))

    def test_make_trigger_reads_every_form(self) -> None:
        self.assertIsInstance(make_trigger("*/5 * * * *"), CronTrigger)
        self.assertIsInstance(make_trigger("@daily"), CronTrigger)
        self.assertIsInstance(make_trigger("every 30s"), IntervalTrigger)
        self.assertIsInstance(make_trigger("30s"), IntervalTrigger)
        self.assertIsInstance(make_trigger(30), IntervalTrigger)
        self.assertIsInstance(make_trigger(None), NeverTrigger)
        self.assertIsInstance(make_trigger("never"), NeverTrigger)

    def test_make_trigger_passes_triggers_through(self) -> None:
        trigger = IntervalTrigger(every=5.0)
        self.assertIs(make_trigger(trigger), trigger)

    def test_make_trigger_rejects_nonsense(self) -> None:
        for value in (True, object(), "not a schedule at all"):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                make_trigger(value)


class WindowTests(unittest.TestCase):
    def test_parse(self) -> None:
        window = parse_window("09:00-17:30")
        self.assertEqual((window.start, window.end), (540, 1050))
        self.assertEqual(window.describe(), "09:00-17:30")

    def test_contains(self) -> None:
        window = parse_window("09:00-17:00")
        self.assertTrue(window.contains(540))
        self.assertFalse(window.contains(1020))
        self.assertFalse(window.contains(539))

    def test_wrapping_window(self) -> None:
        window = parse_window("22:00-02:00")
        self.assertTrue(window.wraps)
        self.assertTrue(window.contains(23 * 60))
        self.assertTrue(window.contains(60))
        self.assertFalse(window.contains(12 * 60))

    def test_next_open_minute(self) -> None:
        window = parse_window("09:00-17:00")
        self.assertIsNone(window.next_open_minute(600))
        self.assertEqual(window.next_open_minute(480), 60)
        self.assertEqual(window.next_open_minute(1020), 24 * 60 - 1020 + 540)

    def test_bad_windows(self) -> None:
        for text in ("nonsense", "9-17", "09:60-10:00", "25:00-26:00", "09:00-09:00"):
            with self.subTest(text=text), self.assertRaises(ScheduleError):
                parse_window(text)

    def test_parse_date(self) -> None:
        self.assertEqual(parse_date("2026-03-11").isoformat(), "2026-03-11")
        with self.assertRaises(ScheduleError):
            parse_date("2026-02-30")
        with self.assertRaises(ScheduleError):
            parse_date("11/03/2026")


class CalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = BusinessCalendar.of(
            "biz",
            weekend=["sat", "sun"],
            holidays=["2026-03-13"],
            windows=["09:00-17:00"],
        )

    def test_always_open_calendar(self) -> None:
        self.assertTrue(ALWAYS_OPEN.unrestricted)
        self.assertTrue(ALWAYS_OPEN.is_open(MARCH))

    def test_weekend_is_closed(self) -> None:
        saturday = parse_timestamp("2026-03-14T10:00:00Z")
        self.assertFalse(self.calendar.is_business_day(saturday))

    def test_holiday_is_closed(self) -> None:
        friday = parse_timestamp("2026-03-13T10:00:00Z")
        self.assertFalse(self.calendar.is_business_day(friday))

    def test_outside_the_window_is_closed(self) -> None:
        early = parse_timestamp("2026-03-11T08:00:00Z")
        self.assertFalse(self.calendar.is_open(early))

    def test_align_moves_to_the_next_open_moment(self) -> None:
        early = parse_timestamp("2026-03-11T08:00:00Z")
        self.assertEqual(iso(self.calendar.align(early)), "2026-03-11T09:00:00Z")

    def test_align_skips_holiday_and_weekend(self) -> None:
        friday = parse_timestamp("2026-03-13T10:00:00Z")
        self.assertEqual(iso(self.calendar.align(friday)), "2026-03-16T09:00:00Z")

    def test_align_is_a_noop_when_already_open(self) -> None:
        open_moment = parse_timestamp("2026-03-11T10:00:00Z")
        self.assertEqual(self.calendar.align(open_moment), open_moment)

    def test_next_business_day(self) -> None:
        thursday = parse_timestamp("2026-03-12T10:00:00Z")
        self.assertEqual(iso(self.calendar.next_business_day(thursday)), "2026-03-16T00:00:00Z")

    def test_business_days_between(self) -> None:
        start = parse_timestamp("2026-03-09T00:00:00Z")
        end = parse_timestamp("2026-03-16T00:00:00Z")
        self.assertEqual(self.calendar.business_days_between(start, end), 4)

    def test_calendar_that_never_opens_raises(self) -> None:
        closed = BusinessCalendar.of(
            "closed", weekend=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        )
        with self.assertRaises(ScheduleError):
            closed.align(MARCH)

    def test_merge_unions_closures_and_intersects_windows(self) -> None:
        morning = BusinessCalendar.of("am", weekend=["sat"], windows=["08:00-12:00"])
        afternoon = BusinessCalendar.of("pm", weekend=["sun"], windows=["10:00-16:00"])
        merged = merge_calendars("both", [morning, afternoon])
        self.assertEqual(merged.weekend, frozenset({5, 6}))
        self.assertEqual([window.describe() for window in merged.windows], ["10:00-12:00"])

    def test_unknown_weekday_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            BusinessCalendar.of("x", weekend=["funday"])


class TimetableTests(unittest.TestCase):
    def build(self) -> Timetable:
        timetable = Timetable()
        timetable.add("quarterly", "flow_a", "*/15 * * * *")
        timetable.add("nightly", "flow_b", "@daily")
        return timetable

    def test_next_fire_picks_the_earliest(self) -> None:
        timetable = self.build()
        start = parse_timestamp("2026-03-11T23:40:00Z")
        fire = timetable.next_fire(start)
        assert fire is not None
        self.assertEqual(fire.name, "quarterly")
        self.assertEqual(iso(fire.at), "2026-03-11T23:45:00Z")

    def test_ties_go_to_the_earlier_declaration(self) -> None:
        timetable = self.build()
        start = parse_timestamp("2026-03-11T23:50:00Z")
        fire = timetable.next_fire(start)
        assert fire is not None
        self.assertEqual(iso(fire.at), "2026-03-12T00:00:00Z")
        self.assertEqual(fire.name, "quarterly")

    def test_upcoming_is_ordered(self) -> None:
        timetable = self.build()
        start = parse_timestamp("2026-03-11T23:50:00Z")
        moments = [fire.at for fire in timetable.upcoming(start, 5)]
        self.assertEqual(moments, sorted(moments))

    def test_simultaneous_fires_use_declaration_order(self) -> None:
        timetable = Timetable()
        timetable.add("second", "b", "@daily")
        timetable.add("first", "a", "@daily")
        start = parse_timestamp("2026-03-11T12:00:00Z")
        fires = timetable.upcoming(start, 2)
        self.assertEqual([fire.name for fire in fires], ["second", "first"])
        self.assertEqual(fires[0].at, fires[1].at)

    def test_between_includes_both_of_a_simultaneous_pair(self) -> None:
        timetable = Timetable()
        timetable.add("a", "a", "@daily")
        timetable.add("b", "b", "@daily")
        start = parse_timestamp("2026-03-11T00:00:00Z")
        end = parse_timestamp("2026-03-12T00:00:00Z")
        self.assertEqual(len(timetable.between(start, end)), 2)

    def test_disabled_entries_never_fire(self) -> None:
        timetable = Timetable()
        timetable.add("off", "flow", "@daily", enabled=False)
        self.assertIsNone(timetable.next_fire(MARCH))
        timetable.enable("off")
        self.assertIsNotNone(timetable.next_fire(MARCH))

    def test_closed_calendar_skips_by_default(self) -> None:
        calendar = BusinessCalendar.of("biz", weekend=["sat", "sun"], windows=[])
        entry = ScheduleEntry(
            name="daily", workflow="flow", trigger=make_trigger("@daily"), calendar=calendar
        )
        friday = parse_timestamp("2026-03-13T12:00:00Z")
        self.assertEqual(iso(entry.next_after(friday)), "2026-03-16T00:00:00Z")

    def test_closed_calendar_can_defer_instead(self) -> None:
        calendar = BusinessCalendar.of("biz", weekend=["sat", "sun"], windows=[])
        entry = ScheduleEntry(
            name="daily",
            workflow="flow",
            trigger=make_trigger("@daily"),
            calendar=calendar,
            on_closed="defer",
        )
        friday = parse_timestamp("2026-03-13T12:00:00Z")
        self.assertEqual(iso(entry.next_after(friday)), "2026-03-16T00:00:00Z")

    def test_unknown_closed_policy_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            ScheduleEntry(
                name="x", workflow="f", trigger=make_trigger("@daily"), on_closed="explode"
            )

    def test_duplicate_names_are_rejected(self) -> None:
        timetable = self.build()
        with self.assertRaises(ScheduleError):
            timetable.add("nightly", "other", "@daily")

    def test_lookup_and_removal(self) -> None:
        timetable = self.build()
        self.assertIn("nightly", timetable)
        self.assertEqual(timetable.entry("nightly").workflow, "flow_b")
        timetable.remove("nightly")
        self.assertNotIn("nightly", timetable)
        with self.assertRaises(ScheduleError):
            timetable.entry("nightly")

    def test_for_workflow(self) -> None:
        timetable = self.build()
        self.assertEqual(
            [entry.name for entry in timetable.for_workflow("flow_a")], ["quarterly"]
        )

    def test_params_travel_with_the_fire(self) -> None:
        timetable = Timetable()
        timetable.add("nightly", "flow", "@daily", params={"region": "eu"})
        fire = timetable.next_fire(MARCH)
        assert fire is not None
        self.assertEqual(fire.params, {"region": "eu"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
