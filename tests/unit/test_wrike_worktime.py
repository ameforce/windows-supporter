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
    composed_vacation_credit_minutes,
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
            recorded_minutes=3 * 60,
            target_minutes=9 * 60,
            intervals=[lunch],
        )
        rows = overview.as_lines(now)
        joined = "\n".join(text for text, _color in rows)
        self.assertEqual(len(rows), 5)
        self.assertIn("Wrike 기록 3시간 · 현재 기대 4시간", rows[0][0])
        self.assertIn("출근 09:00", joined)
        self.assertIn("출근 후 순경과 4시간", joined)
        self.assertIn("점심", joined)
        self.assertIn("예상 퇴근", joined)
        self.assertIn("현재 기준 부족 1시간", rows[-1][0])

    def test_late_wall_clock_reference_does_not_replace_expected_now(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        now = self._dt(23, 55)
        overview = build_workday_overview(
            now=now,
            clock_in=self._dt(8),
            recorded_minutes=8 * 60 + 30,
            target_minutes=9 * 60,
            intervals=[lunch],
        )

        self.assertEqual(overview.net_elapsed_minutes, 14 * 60 + 55)
        self.assertEqual(overview.expected_now_minutes, 9 * 60)
        self.assertEqual(overview.recorded_minutes, 8 * 60 + 30)
        self.assertEqual(overview.actual_minutes, 8 * 60 + 30)
        self.assertEqual(overview.realtime_delta_minutes, -30)
        self.assertTrue(overview.recorded_available)
        self.assertEqual(overview.remaining_net_minutes, -(5 * 60 + 55))
        self.assertEqual((overview.projected_quit.hour, overview.projected_quit.minute), (18, 0))

        rows = overview.as_lines(now)
        rendered = "\n".join(text for text, _color in rows)
        self.assertEqual(len(rows), 5)
        self.assertIn("Wrike 기록 8시간 30분 · 현재 기대 9시간", rows[0][0])
        self.assertIn("출근 후 순경과 14시간 55분", rows[1][0])
        self.assertIn("적용 목표 9시간", rows[3][0])
        self.assertIn("예상 퇴근 18:00", rows[3][0])
        self.assertIn("현재 기준 부족 30분", rows[-1][0])
        self.assertNotIn("현재 기준 초과", rows[-1][0])
        self.assertNotIn("5시간 55분", rendered)

    def test_shortage_grows_when_recorded_minutes_stay_fixed(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        common = {
            "clock_in": self._dt(8),
            "recorded_minutes": 5 * 60 + 30,
            "target_minutes": 9 * 60,
            "intervals": [lunch],
        }
        at_1500 = build_workday_overview(now=self._dt(15), **common)
        at_1510 = build_workday_overview(now=self._dt(15, 10), **common)

        self.assertEqual(at_1500.expected_now_minutes, 6 * 60)
        self.assertEqual(at_1510.expected_now_minutes, 6 * 60 + 10)
        self.assertEqual(at_1500.realtime_delta_minutes, -30)
        self.assertEqual(at_1510.realtime_delta_minutes, -40)
        self.assertEqual(
            -at_1510.realtime_delta_minutes - (-at_1500.realtime_delta_minutes),
            10,
        )
        self.assertIn("현재 기준 부족 40분", at_1510.as_lines(self._dt(15, 10))[-1][0])

    def test_expected_now_stops_while_break_is_in_progress(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        common = {
            "clock_in": self._dt(8),
            "recorded_minutes": 4 * 60,
            "target_minutes": 9 * 60,
            "intervals": [lunch],
        }
        early = build_workday_overview(now=self._dt(12, 10), **common)
        late = build_workday_overview(now=self._dt(12, 50), **common)

        self.assertEqual(early.break_total_minutes, 10)
        self.assertEqual(late.break_total_minutes, 50)
        self.assertEqual(early.expected_now_minutes, 4 * 60)
        self.assertEqual(late.expected_now_minutes, 4 * 60)
        self.assertEqual(early.realtime_delta_minutes, 0)
        self.assertEqual(late.realtime_delta_minutes, 0)

    def test_expected_now_stops_at_effective_target_and_projected_quit(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        common = {
            "clock_in": self._dt(8),
            "recorded_minutes": 9 * 60,
            "target_minutes": 9 * 60,
            "intervals": [lunch],
        }
        at_quit = build_workday_overview(now=self._dt(18), **common)
        after_quit = build_workday_overview(now=self._dt(23), **common)

        self.assertEqual(at_quit.expected_now_minutes, 9 * 60)
        self.assertEqual(after_quit.expected_now_minutes, 9 * 60)
        self.assertGreater(after_quit.net_elapsed_minutes, after_quit.expected_now_minutes)
        self.assertEqual(at_quit.realtime_delta_minutes, 0)
        self.assertEqual(after_quit.realtime_delta_minutes, 0)
        self.assertEqual(at_quit.projected_quit, after_quit.projected_quit)
        self.assertEqual((after_quit.projected_quit.hour, after_quit.projected_quit.minute), (18, 0))

    def test_realtime_delta_reports_exact_ahead_and_behind(self):
        cases = [
            (90, -30, "현재 기준 부족 30분"),
            (120, 0, "현재 기준 딱 맞음"),
            (150, 30, "현재 기준 초과 30분"),
        ]
        for recorded, expected_delta, expected_text in cases:
            with self.subTest(recorded=recorded):
                overview = build_workday_overview(
                    now=self._dt(10),
                    clock_in=self._dt(8),
                    recorded_minutes=recorded,
                    target_minutes=9 * 60,
                    intervals=[],
                )
                self.assertEqual(overview.expected_now_minutes, 120)
                self.assertEqual(overview.realtime_delta_minutes, expected_delta)
                self.assertTrue(overview.recorded_available)
                self.assertIn(expected_text, overview.as_lines(self._dt(10))[-1][0])

    def test_recorded_none_reports_query_unavailable(self):
        now = self._dt(10)
        overview = build_workday_overview(
            now=now,
            clock_in=self._dt(8),
            recorded_minutes=None,
            target_minutes=9 * 60,
            intervals=[],
        )

        self.assertIsNone(overview.recorded_minutes)
        self.assertIsNone(overview.actual_minutes)
        self.assertIsNone(overview.realtime_delta_minutes)
        self.assertFalse(overview.recorded_available)
        self.assertEqual(overview.expected_now_minutes, 120)
        rows = overview.as_lines(now)
        self.assertEqual(len(rows), 5)
        self.assertIn("Wrike 기록 조회 불가 · 현재 기대 2시간", rows[0][0])
        self.assertIn("현재 기준 조회 불가", rows[-1][0])

    def test_expected_now_is_zero_before_clock_in(self):
        now = self._dt(7, 30)
        overview = build_workday_overview(
            now=now,
            clock_in=self._dt(8),
            recorded_minutes=0,
            target_minutes=9 * 60,
            intervals=[],
        )

        self.assertEqual(overview.net_elapsed_minutes, 0)
        self.assertEqual(overview.expected_now_minutes, 0)
        self.assertEqual(overview.realtime_delta_minutes, 0)
        self.assertIn("현재 기준 딱 맞음", overview.as_lines(now)[-1][0])

    def test_projected_quit_does_not_move_with_wrike_recorded_minutes(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        common = {
            "now": self._dt(15),
            "clock_in": self._dt(8),
            "target_minutes": 9 * 60,
            "intervals": [lunch],
        }
        far_behind = build_workday_overview(recorded_minutes=0, **common)
        far_ahead = build_workday_overview(recorded_minutes=20 * 60, **common)

        self.assertEqual(far_behind.projected_quit, far_ahead.projected_quit)
        self.assertEqual((far_behind.projected_quit.hour, far_behind.projected_quit.minute), (18, 0))

    def test_timed_vacation_credit_excludes_existing_break_overlap(self):
        lunch = build_lunch_interval(self.day, True, 720, 780)
        vacation = BreakInterval(
            start=self._dt(12, 30),
            end=self._dt(14),
            label="휴가",
        )

        overview = build_workday_overview(
            now=self._dt(14),
            clock_in=self._dt(8),
            recorded_minutes=4 * 60,
            target_minutes=8 * 60,
            intervals=[lunch],
            vacation_intervals=[vacation],
            vacation_all_day=False,
        )

        self.assertEqual(overview.vacation_minutes, 60)
        self.assertEqual(overview.effective_target_minutes, 7 * 60)
        self.assertEqual(overview.break_total_minutes, 2 * 60)
        self.assertEqual(overview.net_elapsed_minutes, 4 * 60)
        self.assertEqual(overview.expected_now_minutes, 4 * 60)
        self.assertEqual(overview.realtime_delta_minutes, 0)
        self.assertEqual(overview.projected_quit, self._dt(17))
        self.assertIn("점심/휴가 2시간", overview.break_labels)

    def test_composed_vacation_credit_excludes_lunch_calendar_and_manual_overlap(self):
        cases = (
            (
                "점심",
                BreakInterval(self._dt(12), self._dt(13), "점심"),
                BreakInterval(self._dt(12, 30), self._dt(14), "휴가"),
            ),
            (
                "캘린더",
                BreakInterval(self._dt(10), self._dt(11), "캘린더"),
                BreakInterval(self._dt(10, 30), self._dt(12), "휴가"),
            ),
            (
                "수동",
                BreakInterval(self._dt(15), self._dt(16), "수동"),
                BreakInterval(self._dt(15, 30), self._dt(17), "휴가"),
            ),
        )

        for source, break_interval, vacation_interval in cases:
            with self.subTest(source=source):
                self.assertEqual(
                    composed_vacation_credit_minutes(
                        8 * 60,
                        [break_interval],
                        [vacation_interval],
                        self._dt(18),
                    ),
                    60,
                )

    def test_composed_vacation_credit_floors_only_after_union_difference(self):
        break_interval = BreakInterval(
            self.day.replace(hour=10, minute=0, second=0, microsecond=0),
            self.day.replace(hour=10, minute=1, second=0, microsecond=900_000),
            "점심",
        )
        vacation_interval = BreakInterval(
            self.day.replace(hour=10, minute=1, second=0, microsecond=900_000),
            self.day.replace(hour=10, minute=2, second=0, microsecond=100_000),
            "휴가",
        )

        self.assertEqual(
            composed_vacation_credit_minutes(
                8 * 60,
                [break_interval],
                [vacation_interval],
                self._dt(18),
            ),
            0,
        )

    def test_refreshable_lines_length_guard_falls_back(self):
        base_rows = [("a", None), ("b", None)]
        refreshed = RefreshableLines(base_rows, lambda: [("x", "#fff")])
        self.assertEqual(refreshed.refresh(), list(base_rows))


if __name__ == "__main__":
    unittest.main()
