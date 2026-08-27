import unittest
from datetime import date, datetime, timedelta

from src.apps.wrike_ical import (
    fetch_calendar_text,
    matching_break_events,
    occurrences_for_day,
    parse_ics,
    unfold_ics_lines,
)


def _ics(*lines):
    return "\r\n".join(lines)


class IcsParsingUnitTest(unittest.TestCase):
    def test_unfold_continuation_lines_join_previous(self):
        text = _ics(
            "BEGIN:VEVENT",
            "SUMMARY:월요일",
            "  PT 수업",
            "END:VEVENT",
        )
        joined = unfold_ics_lines(text)
        self.assertEqual(joined[1], "SUMMARY:월요일 PT 수업")

    def test_parse_basic_event_and_utc_conversion(self):
        text = _ics(
            "BEGIN:VEVENT",
            "DTSTART:20260824T010000Z",
            "DTEND:20260824T020000Z",
            "SUMMARY:헬스장 PT",
            "END:VEVENT",
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["summary"], "헬스장 PT")
        self.assertFalse(event["all_day"])
        self.assertIsNone(event["dtstart"].tzinfo)
        self.assertGreater(event["dtend"] - event["dtstart"], timedelta(minutes=59))

    def test_all_day_event_marked_and_skipped_by_occurrences(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART;VALUE=DATE:20260826",
                "DTEND;VALUE=DATE:20260827",
                "SUMMARY:운동의 날",
                "END:VEVENT",
            )
        )
        self.assertTrue(events[0]["all_day"])
        self.assertEqual(occurrences_for_day(events[0], date(2026, 8, 26)), [])

    def test_single_event_matches_target_day(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260826T183000",
                "DTEND:20260826T193000",
                "SUMMARY:헬스장 하체",
                "END:VEVENT",
            )
        )
        spans = occurrences_for_day(events[0], date(2026, 8, 26))
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertEqual((start.hour, start.minute), (18, 30))
        self.assertEqual(end.hour, 19)

    def test_weekly_byday_expands_to_correct_weekdays(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260824T190000",
                "DTEND:20260824T201500",
                "RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
                "SUMMARY:PT 수업",
                "END:VEVENT",
            )
        )
        monday = occurrences_for_day(events[0], date(2026, 8, 31))
        wednesday = occurrences_for_day(events[0], date(2026, 9, 2))
        tuesday = occurrences_for_day(events[0], date(2026, 9, 1))
        self.assertEqual([len(monday), len(wednesday), len(tuesday)], [1, 1, 0])

    def test_interval_two_weekly_skips_alternate_weeks(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260803T190000",
                "DTEND:20260803T200000",
                "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO",
                "SUMMARY:격주 헬스",
                "END:VEVENT",
            )
        )
        self.assertEqual(len(occurrences_for_day(events[0], date(2026, 8, 17))), 1)
        self.assertEqual(len(occurrences_for_day(events[0], date(2026, 8, 24))), 0)

    def test_until_rule_stops_expansion(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260803T190000",
                "DTEND:20260803T200000",
                "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260810T235959Z",
                "SUMMARY:단기 헬스",
                "END:VEVENT",
            )
        )
        within = occurrences_for_day(events[0], date(2026, 8, 10))
        beyond = occurrences_for_day(events[0], date(2026, 8, 24))
        self.assertEqual(len(within), 1)
        self.assertEqual(beyond, [])

    def test_count_rule_limits_instances(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260803T190000",
                "DTEND:20260803T200000",
                "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=2",
                "SUMMARY:두 번만 헬스",
                "END:VEVENT",
            )
        )
        third = occurrences_for_day(events[0], date(2026, 8, 17))
        self.assertEqual(third, [])

    def test_exdate_removes_specific_instance(self):
        events = parse_ics(
            _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260803T190000",
                "DTEND:20260803T200000",
                "RRULE:FREQ=WEEKLY;BYDAY=MO",
                "EXDATE:20260810T190000",
                "SUMMARY:쉬는 주 포함 헬스",
                "END:VEVENT",
            )
        )
        self.assertEqual(occurrences_for_day(events[0], date(2026, 8, 10)), [])
        self.assertEqual(len(occurrences_for_day(events[0], date(2026, 8, 17))), 1)

    def test_keyword_filter_fail_closed_on_empty_list(self):
        self.assertEqual(matching_break_events([], [], date.today()), [])
        self.assertEqual(matching_break_events(None, ["헬스"], date.today()), [])

    def test_matching_break_events_filters_summary_and_expands(self):
        events = [
            {
                "summary": "헬스장 PT 수업",
                "dtstart": datetime(2026, 8, 26, 18, 30),
                "dtend": datetime(2026, 8, 26, 19, 30),
                "all_day": False,
                "rrule": {},
                "exdates": [],
            },
            {
                "summary": "주간 회의",
                "dtstart": datetime(2026, 8, 26, 15, 0),
                "dtend": datetime(2026, 8, 26, 16, 0),
                "all_day": False,
                "rrule": {},
                "exdates": [],
            },
        ]
        matched = matching_break_events(events, ["헬스"], date(2026, 8, 26))
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["label"], "헬스장 PT 수업")
        start, end = matched[0]["intervals"][0]
        self.assertEqual(end - start, timedelta(hours=1))

    def test_fetch_calendar_text_rejects_non_http_url(self):
        self.assertIsNone(fetch_calendar_text("file:///etc/passwd"))
        self.assertIsNone(fetch_calendar_text(""))
        self.assertIsNone(fetch_calendar_text(None))


if __name__ == "__main__":
    unittest.main()


