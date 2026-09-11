from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from src.apps.flex_worktime import (
    FlexBrowserClient,
    FlexBrowserError,
    FlexBrowserSyncResult,
    FlexScheduleError,
    _parse_browser_response_payloads,
    extract_flex_employee_number,
    parse_flex_schedule_response,
    parse_flex_work_record_text,
)


class FlexScheduleParserTests(unittest.TestCase):
    @staticmethod
    def _flex_timestamp(hour: int, minute: int = 0) -> dict[str, object]:
        value = datetime(
            2026,
            9,
            10,
            hour,
            minute,
            tzinfo=timezone(timedelta(hours=9)),
        )
        return {
            "zoneId": "Asia/Seoul",
            "timestamp": int(value.timestamp() * 1000),
        }

    def test_normalizes_planned_break_overtime_and_actual_clock_blocks(self) -> None:
        payload = {
            "userWorkSchedules": [
                {
                    "employeeNumber": "E-42",
                    "days": [
                        {
                            "date": "2026-09-10",
                            "workBlocks": [
                                {
                                    "type": "WORK_RECORD",
                                    "formName": "기본 근무",
                                    "blockFrom": "2026-09-10T09:00:00+09:00",
                                    "blockTo": "2026-09-10T18:00:00+09:00",
                                },
                                {
                                    "type": "WORK_RECORD",
                                    "formName": "점심 휴게",
                                    "blockFrom": "2026-09-10T12:00:00+09:00",
                                    "blockTo": "2026-09-10T13:00:00+09:00",
                                },
                                {
                                    "type": "WORK_RECORD",
                                    "formName": "연장 근무",
                                    "blockFrom": "2026-09-10T18:00:00+09:00",
                                    "blockTo": "2026-09-10T20:00:00+09:00",
                                },
                                {
                                    "type": "WORK_CLOCK",
                                    "formName": "출퇴근 기록",
                                    "blockFrom": "2026-09-10T09:07:00+09:00",
                                    "blockTo": "2026-09-10T18:12:00+09:00",
                                },
                            ],
                        }
                    ],
                }
            ]
        }

        result = parse_flex_schedule_response(
            payload,
            employee_number="E-42",
            now=datetime(2026, 9, 10, 19, 0),
        )
        schedule = result[datetime(2026, 9, 10).date()]

        self.assertEqual(schedule.target_minutes, 600)
        self.assertEqual(schedule.overtime_assigned_minutes, 120)
        self.assertEqual(schedule.scheduled_start.strftime("%H:%M"), "09:00")
        self.assertEqual(schedule.regular_quit.strftime("%H:%M"), "18:00")
        self.assertEqual(schedule.scheduled_quit.strftime("%H:%M"), "20:00")
        self.assertEqual(schedule.actual_start.strftime("%H:%M"), "09:07")
        self.assertEqual(schedule.actual_quit.strftime("%H:%M"), "18:12")
        self.assertEqual(
            [(start.strftime("%H:%M"), end.strftime("%H:%M")) for start, end in schedule.break_intervals],
            [("12:00", "13:00")],
        )

    def test_ignores_other_employee_and_keeps_ongoing_block_live(self) -> None:
        payload = {
            "userWorkSchedules": [
                {
                    "employeeNumber": "OTHER",
                    "days": [{"date": "2026-09-10", "workBlocks": []}],
                },
                {
                    "employeeNumber": "E-42",
                    "days": [
                        {
                            "date": "2026-09-10",
                            "workBlocks": [
                                {
                                    "type": "WORK_RECORD",
                                    "formName": "기본 근무",
                                    "blockFrom": "2026-09-10T09:00:00",
                                    "blockTo": None,
                                }
                            ],
                        }
                    ],
                },
            ]
        }
        result = parse_flex_schedule_response(
            payload,
            employee_number="E-42",
            now=datetime(2026, 9, 10, 12, 30),
        )
        self.assertEqual(result[datetime(2026, 9, 10).date()].target_minutes, 210)

    def test_parses_visible_work_record_page_without_api_credentials(self) -> None:
        text = """
        2026.09.10
        근무 예정 09:00 - 18:00
        점심 휴게 12:00 - 13:00
        실제 출퇴근 기록 09:05 - 18:12
        연장 근무 18:00 - 20:00
        """

        result = parse_flex_work_record_text(
            text,
            target_day=date(2026, 9, 10),
            employee_number="E-42",
            now=datetime(2026, 9, 10, 19, 0),
        )
        schedule = result[date(2026, 9, 10)]

        self.assertEqual(schedule.target_minutes, 600)
        self.assertEqual(schedule.overtime_assigned_minutes, 120)
        self.assertEqual(schedule.scheduled_start.strftime("%H:%M"), "09:00")
        self.assertEqual(schedule.regular_quit.strftime("%H:%M"), "18:00")
        self.assertEqual(schedule.scheduled_quit.strftime("%H:%M"), "20:00")
        self.assertEqual(schedule.actual_start.strftime("%H:%M"), "09:05")
        self.assertEqual(schedule.actual_quit.strftime("%H:%M"), "18:12")

    def test_schedule_parser_rejects_non_mapping_payload(self) -> None:
        with self.assertRaises(FlexScheduleError) as ctx:
            parse_flex_schedule_response([])  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_detects_employee_number_from_identity_and_schedule_payloads(self) -> None:
        payloads = [
            {"data": {"user": {"employeeNumber": "E-42"}}},
            {
                "userWorkSchedules": [
                    {
                        "employeeNumber": "OTHER",
                        "days": [],
                    },
                    {
                        "employeeNumber": "E-42",
                        "days": [
                            {
                                "date": "2026-09-10",
                                "workBlocks": [
                                    {
                                        "type": "WORK_RECORD",
                                        "blockFrom": "2026-09-10T09:00:00",
                                        "blockTo": "2026-09-10T18:00:00",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
        ]

        schedules, employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number="",
            now=datetime(2026, 9, 10, 12, 0),
        )

        self.assertIn(date(2026, 9, 10), schedules)
        self.assertEqual(employee_number, "E-42")

    def test_parses_current_flex_daily_schedule_envelope_and_work_forms(self) -> None:
        payloads = [
            {
                "userIdHash": "opaque-user-id",
                "dailySchedules": [
                    {
                        "date": "2026-09-10",
                        "timeBlocks": [
                            {
                                "type": "WORK",
                                "value": {
                                    "startTimestamp": self._flex_timestamp(9),
                                    "endTimestampExclusive": self._flex_timestamp(18),
                                    "workFormId": "basic-work-form",
                                },
                            },
                            {
                                "type": "REST",
                                "value": {
                                    "startTimestamp": self._flex_timestamp(12),
                                    "endTimestampExclusive": self._flex_timestamp(13),
                                    "workFormId": "rest-form",
                                },
                            },
                            {
                                "type": "WORK",
                                "value": {
                                    "startTimestamp": self._flex_timestamp(18),
                                    "endTimestampExclusive": self._flex_timestamp(20),
                                    "workFormId": "overtime-form",
                                },
                            },
                        ],
                    }
                ],
            },
            {
                "workForms": [
                    {
                        "customerWorkFormId": "basic-work-form",
                        "type": "WORK",
                        "display": {"name": "기본 근무"},
                    },
                    {
                        "customerWorkFormId": "rest-form",
                        "type": "REST",
                        "display": {"name": "점심 휴게"},
                    },
                    {
                        "customerWorkFormId": "overtime-form",
                        "type": "WORK",
                        "display": {"name": "연장 근무"},
                    },
                ]
            },
            {"employeeNumber": "E-42"},
        ]

        schedules, employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number="",
            now=datetime(2026, 9, 10, 19, 0),
        )
        schedule = schedules[date(2026, 9, 10)]

        self.assertEqual(employee_number, "E-42")
        self.assertEqual(schedule.target_minutes, 600)
        self.assertEqual(schedule.overtime_assigned_minutes, 120)
        self.assertEqual(schedule.scheduled_start.strftime("%H:%M"), "09:00")
        self.assertEqual(schedule.regular_quit.strftime("%H:%M"), "18:00")
        self.assertEqual(schedule.scheduled_quit.strftime("%H:%M"), "20:00")
        self.assertEqual(schedule.overtime_scheduled_quit.strftime("%H:%M"), "20:00")

    def test_extracts_labelled_employee_number_from_visible_text(self) -> None:
        self.assertEqual(
            extract_flex_employee_number("내 계정 · 사번: 12345"),
            "12345",
        )
        self.assertEqual(extract_flex_employee_number("사용자 id: abc"), "")


class FlexBrowserClientTests(unittest.TestCase):
    def test_invalid_period_fails_before_opening_browser(self) -> None:
        client = FlexBrowserClient("C:/temp/flex-profile-test")
        with self.assertRaises(FlexBrowserError) as ctx:
            client.fetch_schedule_period(
                date(2026, 9, 13),
                date(2026, 9, 7),
                employee_number="E-42",
            )
        self.assertEqual(ctx.exception.code, "invalid_period")

    def test_headless_session_requires_explicit_login_instead_of_waiting(self) -> None:
        client = FlexBrowserClient("C:/temp/flex-profile-test", headless=True)

        class _LoginPage:
            url = "https://flex.team/auth/login"

            def is_closed(self):
                return False

            def locator(self, *_args):
                return self

            def count(self):
                return 0

            def inner_text(self, **_kwargs):
                return ""

        with self.assertRaises(FlexBrowserError) as ctx:
            client._wait_for_login(_LoginPage())

        self.assertEqual(ctx.exception.code, "login_required")
        self.assertIn("Flex 웹 열기", str(ctx.exception))

    def test_schedule_client_defaults_to_headless(self) -> None:
        client = FlexBrowserClient("C:/temp/flex-profile-test")

        self.assertTrue(client._headless)

    def test_sync_result_keeps_schedule_compatible_metadata_separate(self) -> None:
        result = FlexBrowserSyncResult(schedules={}, employee_number="E-42")

        self.assertEqual(result.schedules, {})
        self.assertEqual(result.employee_number, "E-42")
