import unittest
from datetime import datetime

from src.apps.wrike_worktime import (
    BreakInterval,
    DEFAULT_LUNCH_END_MIN,
    DEFAULT_LUNCH_START_MIN,
    RefreshableLines,
    build_lunch_interval,
    build_workday_overview,
    clock_in_candidate,
    compute_net_elapsed_minutes,
    earliest_clock_in_from_items,
    format_minutes,
    overlap_minutes,
    parse_iso_datetime,
    project_quit_at,
    total_break_minutes_within,
)


class WorktimeComputationUnitTest(unittest.TestCase):
    def setUp(self):
        self.day = datetime(2026, 8, 27)

    def _dt(self, hour, minute=0):
        return datetime(2026, 8, 27, hour, minute)

    def test_overlap_minutes_counts_only_shared_span(self):
        a_start = self._dt(12, 0)
        a_end = self._dt(13, 0)
        b_start = self._dt(9, 0)
        b_end = self._dt(12, 30)
        self.assertEqual(overlap_minutes(a_start, a_end, b_start, b_end), 30)

    def test_build_lunch_interval_defaults_and_disable(self):
        lunch = build_lunch_interval(self.day.replace(hour=1), True, DEFAULT_LUNCH_START_MIN, DEFAULT_LUNCH_END_MIN)
        self.assertEqual(lunch.start.hour, 12)
        self.assertEqual(lunch.end.hour, 13)
        self.assertEqual(lunch.label, "점심")
        self.assertIsNone(build_lunch_interval(self.day, False, 720, 780))

    def test_total_break_minutes_within_excludes_before_clock_in(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        early = BreakInterval(start=self._dt(7, 0), end=self._dt(8, 0), label="아침")
        total = total_break_minutes_within([lunch, early], self._dt(9), self._dt(17))
        self.assertEqual(total, 60)

    def test_compute_net_elapsed_minutes_subtracts_ongoing_pause(self):
        intervals = [BreakInterval(start=self._dt(11), end=None, label="수동")]
        net = compute_net_elapsed_minutes(
            self._dt(14, 30), self._dt(9), intervals,
        )
        # 5.5h elapsed minus 3.5h ongoing = 2h
        self.assertEqual(net, 120)

    def test_project_quit_without_breaks_is_straight_sum(self):
        quit_at = project_quit_at(
            self._dt(10), self._dt(9), 9 * 60, [],
        )
        self.assertEqual((quit_at.hour, quit_at.minute), (18, 0))

    def test_project_quit_adds_fixed_lunch_iteratively(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        quit_at = project_quit_at(
            self._dt(15), self._dt(9), 9 * 60, [lunch],
        )
        # Naive 18:00 + one hour lunch consumed between 09:00 and 18:00 -> 19:00
        self.assertEqual((quit_at.hour, quit_at.minute), (19, 0))

    def test_project_quit_pushes_out_of_active_break_window(self):
        gym = BreakInterval(start=self._dt(17, 45), end=self._dt(18, 45), label="PT 수업")
        quit_at = project_quit_at(
            self._dt(15), self._dt(9), 9 * 60, [gym],
        )
        # The 15 minutes of quota window (17:45~18:00) is paused too,
        # so the whole hour moves behind the gym session.
        self.assertEqual((quit_at.hour, quit_at.minute), (19, 0))

    def test_project_quit_starts_after_clock_in_gap_not_counted(self):
        late_entry = BreakInterval(start=self._dt(23), end=self._dt(23, 59), label="야식")
        quit_at = project_quit_at(self._dt(16), self._dt(9), 60, [late_entry])
        self.assertEqual((quit_at.hour, quit_at.minute), (10, 0))

    def test_parse_iso_datetime_handles_offset_and_z(self):
        parsed = parse_iso_datetime("2026-08-27T01:15:00Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, None)

    def test_clock_in_candidate_prefers_non_midnight_tracked_date(self):
        item = {"trackedDate": "2026-08-27T09:04:00", "createdDate": "2026-08-27T11:40:00"}
        candidate = clock_in_candidate(item)
        self.assertEqual((candidate.hour, candidate.minute), (9, 4))

    def test_clock_in_candidate_falls_back_to_created_date(self):
        item = {"trackedDate": "2026-08-27", "createdDate": "2026-08-27T12:02:30"}
        candidate = clock_in_candidate(item)
        self.assertEqual((candidate.hour, candidate.minute), (12, 2))

    def test_earliest_clock_in_from_items_picks_minimum(self):
        items = [
            {"trackedDate": "2026-08-27", "createdDate": "2026-08-27T10:30:00"},
            {"trackedDate": "2026-08-27T08:55:00", "createdDate": "2026-08-27T10:29:00"},
        ]
        candidate = earliest_clock_in_from_items(items)
        self.assertEqual(candidate.hour, 8)

    def test_format_minutes_korean_units(self):
        self.assertEqual(format_minutes(65), "1시간 5분")
        self.assertEqual(format_minutes(60), "1시간")
        self.assertEqual(format_minutes(7), "7분")
        self.assertEqual(format_minutes(0), "0분")

    def test_overview_lines_report_lunch_and_shortage(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        now = self._dt(14, 0)
        overview = build_workday_overview(
            now=now,
            clock_in=self._dt(9),
            recorded_minutes=int((now - self._dt(9)).total_seconds() // 60) - 60,
            target_minutes=9 * 60,
            intervals=[lunch],
        )
        rows = overview.as_lines(now)
        joined = "\n".join(text for text, _color in rows)
        self.assertIn("출근 09:00", joined)
        self.assertIn("점심", joined)
        self.assertIn("예상 퇴근", joined)
        self.assertIn("잔여 부족", joined)

    def test_refreshable_lines_length_guard_falls_back(self):
        base_rows = [("a", None), ("b", None)]
        refreshed = RefreshableLines(base_rows, lambda: [("x", "#fff")])
        self.assertEqual(refreshed.refresh(), list(base_rows))


if __name__ == "__main__":
    unittest.main()
