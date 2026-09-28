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
        self.assertEqual(schedule.regular_work_minutes, 480)
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

    def test_parses_annual_leave_and_company_day_off_from_daily_schedules(self) -> None:
        payloads = [
            {
                "userIdHash": "opaque-user-id",
                "dailySchedules": [
                    {
                        "date": "2026-09-21",
                        "timeBlocks": [
                            {
                                "type": "ANNUAL_TIME_OFF",
                                "value": {
                                    "allDay": True,
                                    "usedMinutes": 480,
                                    "status": "APPROVAL_COMPLETED",
                                    "approval": {"status": "APPROVED"},
                                    "cancelApprovals": [],
                                },
                            }
                        ],
                        "dayOffs": [],
                    },
                    {
                        "date": "2026-09-24",
                        "timeBlocks": [],
                        "dayOffs": [{"type": "CUSTOM_HOLIDAY"}],
                    },
                ],
            },
            {"employeeNumber": "E-42"},
        ]

        schedules, employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number="",
            now=datetime(2026, 9, 21, 9, 0),
        )

        leave = schedules[date(2026, 9, 21)]
        self.assertTrue(leave.leave_all_day)
        self.assertEqual(leave.leave_minutes, 480)
        self.assertEqual(leave.target_minutes, 480)
        self.assertEqual(leave.regular_work_minutes, 0)
        self.assertEqual(leave.day_off_types, ())
        holiday = schedules[date(2026, 9, 24)]
        self.assertEqual(holiday.day_off_types, ("CUSTOM_HOLIDAY",))
        self.assertEqual(holiday.target_minutes, 0)
        self.assertEqual(employee_number, "E-42")

    def test_ignores_pending_or_cancelled_leave_blocks(self) -> None:
        payloads = [
            {
                "userIdHash": "opaque-user-id",
                "dailySchedules": [
                    {
                        "date": "2026-09-21",
                        "timeBlocks": [
                            {
                                "type": "ANNUAL_TIME_OFF",
                                "value": {
                                    "allDay": True,
                                    "usedMinutes": 480,
                                    "status": "APPROVAL_IN_PROGRESS",
                                    "approval": {"status": "PENDING"},
                                },
                            }
                        ],
                    },
                    {
                        "date": "2026-09-22",
                        "timeBlocks": [
                            {
                                "type": "ANNUAL_TIME_OFF",
                                "value": {
                                    "allDay": True,
                                    "usedMinutes": 480,
                                    "status": "APPROVAL_COMPLETED",
                                    "approval": {"status": "APPROVED"},
                                    "cancelApprovals": [
                                        {"status": "APPROVED"}
                                    ],
                                },
                            }
                        ],
                    },
                ],
            },
        ]

        schedules, _employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number="",
            now=datetime(2026, 9, 21, 9, 0),
        )

        self.assertNotIn(date(2026, 9, 21), schedules)
        self.assertNotIn(date(2026, 9, 22), schedules)

    def test_parses_half_day_leave_interval_and_minutes(self) -> None:
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
                                    "endTimestampExclusive": self._flex_timestamp(13),
                                    "workFormId": "basic-work-form",
                                },
                            },
                            {
                                "type": "ANNUAL_TIME_OFF",
                                "value": {
                                    "allDay": False,
                                    "usedMinutes": 240,
                                    "timeOffRegisterUnit": "HALF_PM",
                                    "status": "APPROVAL_COMPLETED",
                                    "approval": {"status": "APPROVED"},
                                    "startTimestamp": self._flex_timestamp(14),
                                    "endTimestampExclusive": self._flex_timestamp(18),
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
                ]
            },
        ]

        schedules, _employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number="",
            now=datetime(2026, 9, 10, 19, 0),
        )
        schedule = schedules[date(2026, 9, 10)]

        self.assertFalse(schedule.leave_all_day)
        self.assertEqual(schedule.leave_minutes, 240)
        self.assertEqual(schedule.target_minutes, 480)
        self.assertEqual(schedule.regular_work_minutes, 240)
        self.assertEqual(len(schedule.leave_intervals), 1)
        start, end = schedule.leave_intervals[0]
        self.assertEqual((start.hour, start.minute), (14, 0))
        self.assertEqual((end.hour, end.minute), (18, 0))

    def test_all_day_leave_without_used_minutes_keeps_the_day(self) -> None:
        payload = {
            "userWorkSchedules": [
                {
                    "employeeNumber": "E-42",
                    "days": [
                        {
                            "date": "2026-09-21",
                            "workBlocks": [],
                            "timeOffBlocks": [
                                {
                                    "type": "TIME_OFF",
                                    "value": {
                                        "allDay": True,
                                        "status": "APPROVAL_COMPLETED",
                                        "approval": {"status": "APPROVED"},
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        result = parse_flex_schedule_response(
            payload,
            employee_number="E-42",
            now=datetime(2026, 9, 21, 9, 0),
        )
        schedule = result[datetime(2026, 9, 21).date()]

        self.assertTrue(schedule.leave_all_day)
        self.assertEqual(schedule.leave_minutes, 0)
        self.assertEqual(schedule.target_minutes, 0)

    def test_time_off_under_work_blocks_is_not_counted_as_work(self) -> None:
        payload = {
            "userWorkSchedules": [
                {
                    "employeeNumber": "E-42",
                    "days": [
                        {
                            "date": "2026-09-10",
                            "workBlocks": [
                                {
                                    "type": "ANNUAL_TIME_OFF",
                                    "blockFrom": "2026-09-10T09:00:00",
                                    "blockTo": "2026-09-10T18:00:00",
                                    "value": {
                                        "allDay": True,
                                        "usedMinutes": 480,
                                        "status": "APPROVAL_COMPLETED",
                                    },
                                },
                                {
                                    "type": "WORK_RECORD",
                                    "formName": "기본 근무",
                                    "blockFrom": "2026-09-10T09:00:00",
                                    "blockTo": "2026-09-10T17:00:00",
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

        self.assertTrue(schedule.leave_all_day)
        self.assertEqual(schedule.leave_minutes, 480)
        self.assertEqual(schedule.target_minutes, 960)
        self.assertEqual(schedule.regular_work_minutes, 480)

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
        # The app adds the recovery path (its login window or the settings
        # guidance); the client only states the fact.
        self.assertEqual(str(ctx.exception), "Flex 로그인이 필요합니다.")

    def test_schedule_client_defaults_to_headless(self) -> None:
        client = FlexBrowserClient("C:/temp/flex-profile-test")

        self.assertTrue(client._headless)

    def test_sync_result_keeps_schedule_compatible_metadata_separate(self) -> None:
        result = FlexBrowserSyncResult(schedules={}, employee_number="E-42")

        self.assertEqual(result.schedules, {})
        self.assertEqual(result.employee_number, "E-42")


class FlexLoginWindowClientTests(unittest.TestCase):
    """The app's login window: completion check and focus behaviour."""

    class _Page:
        def __init__(self, url, *, text="", password_fields=0, closed=False):
            self.url = url
            self._text = text
            self._password_fields = password_fields
            self._closed = closed
            self.brought_to_front = 0
            self.gotos = []

        def is_closed(self):
            return self._closed

        def title(self):
            return "Flex"

        def locator(self, selector):
            page = self

            class _Locator:
                def inner_text(self, **_kwargs):
                    return page._text

                def count(self):
                    return page._password_fields if "password" in selector else 0

            return _Locator()

        def bring_to_front(self):
            self.brought_to_front += 1

        def goto(self, url, **_kwargs):
            self.gotos.append(url)

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def set_default_timeout(self, *_args):
            return None

    class _Context:
        def __init__(self, pages):
            self.pages = list(pages)

    def _client(self, *pages, work_url=None):
        kwargs = {"headless": False}
        if work_url is not None:
            kwargs["work_url"] = work_url
        client = FlexBrowserClient("C:/temp/flex-profile-test", **kwargs)
        client._context = self._Context(pages)
        return client

    def test_logged_in_work_record_page_completes_the_login(self) -> None:
        page = self._Page(
            "https://flex.team/time-tracking/my-work-record",
            text="내 근무 기록 09:00 - 18:00",
        )
        self.assertTrue(self._client(page).login_completed())

    def test_login_and_sso_pages_do_not_complete_the_login(self) -> None:
        cases = {
            "flex login route": self._Page(
                "https://flex.team/auth/login?nextUrl=%2Ftime-tracking%2Fmy-work-record"
            ),
            "flex auth callback": self._Page("https://flex.team/auth/callback?code=x"),
            "google sso": self._Page(
                "https://accounts.google.com/v3/signin/identifier?continue=flex"
            ),
            "microsoft sso": self._Page("https://login.microsoftonline.com/common/oauth2"),
            "flex landing page": self._Page(
                "https://flex.team/", text="flex 로그인 이메일"
            ),
            "work record still asking for a password": self._Page(
                "https://flex.team/time-tracking/my-work-record", password_fields=1
            ),
            "post-login interstitial": self._Page(
                "https://flex.team/select-company?next=%2Ftime-tracking"
            ),
            "another flex page": self._Page("https://flex.team/home"),
            "flex subdomain": self._Page(
                "https://help.flex.team/time-tracking/my-work-record"
            ),
            "other host": self._Page("https://example.com/time-tracking"),
            "closed page": self._Page(
                "https://flex.team/time-tracking/my-work-record", closed=True
            ),
        }
        for name, page in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self._client(page).login_completed())

    def test_page_mid_navigation_is_not_logged_in_yet(self) -> None:
        page = self._Page("https://flex.team/time-tracking/my-work-record")

        def navigating():
            raise RuntimeError("Execution context was destroyed")

        page.title = navigating
        self.assertFalse(self._client(page).login_completed())

    def test_any_logged_in_page_of_the_context_counts(self) -> None:
        sso_popup = self._Page("https://accounts.google.com/signin/oauth")
        work_page = self._Page("https://flex.team/time-tracking/my-work-record")
        self.assertTrue(self._client(sso_popup, work_page).login_completed())

    def test_trailing_slash_and_query_on_the_work_record_still_count(self) -> None:
        page = self._Page("https://flex.team/time-tracking/my-work-record/?tab=week")
        self.assertTrue(self._client(page).login_completed())

    def test_logged_in_text_on_the_work_record_does_not_block_completion(self) -> None:
        # A rendered, logged-in page can mention "로그인" (e.g. login history).
        page = self._Page(
            "https://flex.team/time-tracking/my-work-record",
            text="flex 내 근무 기록 · 최근 로그인 기록",
        )
        self.assertTrue(self._client(page).login_completed())

    def test_completion_host_follows_the_configured_work_url(self) -> None:
        page = self._Page("http://127.0.0.1:8123/time-tracking/my-work-record")
        self.assertTrue(
            self._client(
                page,
                work_url="http://127.0.0.1:8123/time-tracking/my-work-record",
            ).login_completed()
        )
        self.assertFalse(self._client(page).login_completed())

    def test_no_context_is_not_logged_in(self) -> None:
        self.assertFalse(
            FlexBrowserClient("C:/temp/flex-profile-test").login_completed()
        )

    def test_login_step_on_screen_is_in_progress(self) -> None:
        cases = {
            "google sso": (self._Page("https://accounts.google.com/signin"), True),
            "flex login route": (self._Page("https://flex.team/auth/login"), True),
            "password form": (
                self._Page("https://flex.team/workspace", password_fields=1),
                True,
            ),
            "blank page before navigation": (self._Page("about:blank"), True),
            "logged in elsewhere on flex": (self._Page("https://flex.team/home"), False),
            "logged-in work record": (
                self._Page("https://flex.team/time-tracking/my-work-record"),
                False,
            ),
        }
        for name, (page, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self._client(page).login_in_progress(), expected)

    def test_navigating_page_counts_as_a_login_in_progress(self) -> None:
        page = self._Page("https://flex.team/home")

        def navigating():
            raise RuntimeError("Execution context was destroyed")

        page.title = navigating
        self.assertTrue(self._client(page).login_in_progress())
        self.assertFalse(
            FlexBrowserClient("C:/temp/flex-profile-test").login_in_progress()
        )

    def test_background_login_window_does_not_take_the_foreground(self) -> None:
        page = self._Page("https://flex.team/auth/login")
        client = self._client(page)

        client.open_login_page(activate=False)

        self.assertEqual(page.gotos, ["https://flex.team/time-tracking/my-work-record"])
        self.assertEqual(page.brought_to_front, 0)

    def test_explicit_login_window_is_brought_to_the_front_once(self) -> None:
        page = self._Page("https://flex.team/auth/login")
        client = self._client(page)

        client.open_login_page(activate=True)

        self.assertEqual(page.brought_to_front, 1)

    def test_focus_window_brings_the_first_open_page_forward(self) -> None:
        closed = self._Page("https://flex.team/auth/login", closed=True)
        page = self._Page("https://flex.team/auth/login")
        client = self._client(closed, page)

        client.focus_window()

        self.assertEqual((closed.brought_to_front, page.brought_to_front), (0, 1))
