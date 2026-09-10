from __future__ import annotations

from datetime import datetime
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from src.apps.flex_worktime import (
    FlexApiError,
    FlexCredentials,
    FlexOpenApiClient,
    parse_flex_schedule_response,
)


class FlexScheduleParserTests(unittest.TestCase):
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


class FlexOpenApiClientTests(unittest.TestCase):
    def test_refreshes_token_then_fetches_schedule(self) -> None:
        calls = []
        responses = [
            {
                "access_token": "access-1",
                "refresh_token": "refresh-rotated",
            },
            {
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
                                        "blockFrom": "2026-09-10T09:00:00",
                                        "blockTo": "2026-09-10T18:00:00",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        ]

        def transport(method, url, headers, data, timeout):
            calls.append((method, url, headers, data, timeout))
            return 200, json.dumps(responses.pop(0)).encode("utf-8")

        client = FlexOpenApiClient(
            FlexCredentials(refresh_token="refresh-original"),
            base_url="https://example.test",
            transport=transport,
        )
        result = client.fetch_schedule_period(
            datetime(2026, 9, 7).date(),
            datetime(2026, 9, 13).date(),
            employee_number="E-42",
            now=datetime(2026, 9, 10, 12, 0),
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "POST")
        self.assertIn(b"grant_type=refresh_token", calls[0][3])
        self.assertEqual(calls[1][0], "GET")
        self.assertEqual(calls[1][2]["Authorization"], "Bearer access-1")
        self.assertEqual(parse_qs(urlsplit(calls[1][1]).query)["employeeNumbers"], ["E-42"])
        self.assertEqual(client.credentials.refresh_token, "refresh-rotated")
        self.assertIn(datetime(2026, 9, 10).date(), result)

    def test_missing_credentials_fails_without_network_call(self) -> None:
        with self.assertRaises(FlexApiError) as ctx:
            FlexOpenApiClient(FlexCredentials()).fetch_schedule_period(
                datetime(2026, 9, 7).date(),
                datetime(2026, 9, 13).date(),
                employee_number="E-42",
            )
        self.assertEqual(ctx.exception.code, "credentials_missing")
