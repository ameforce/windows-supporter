import gzip
import io
import socket
import ssl
import unittest
import urllib.error
from datetime import date, datetime, timedelta

from src.apps.wrike_ical import (
    MAX_ICAL_BYTES,
    CalendarError,
    CalendarErrorCode,
    CalendarSuccess,
    compile_vacation_calendar,
    decode_calendar_response,
    fetch_calendar_text,
    matching_break_events,
    occurrences_for_day,
    parse_calendar,
    parse_calendar_document,
    parse_ics,
    read_calendar_response_text,
    unfold_ics_lines,
    vacation_events_for_day,
)


def _ics(*lines):
    return "\r\n".join(lines)


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        headers=None,
        max_chunk: int | None = None,
        fail_after: int | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.headers = headers or {}
        self._stream = io.BytesIO(body)
        self._max_chunk = max_chunk
        self._fail_after = fail_after
        self._read_error = read_error
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self._fail_after is not None and len(self.read_sizes) > self._fail_after:
            if self._read_error is not None:
                raise self._read_error
            raise OSError("synthetic read failure")
        if self._max_chunk is not None:
            size = min(size, self._max_chunk)
        return self._stream.read(size)


class CalendarResponseDecodingUnitTest(unittest.TestCase):
    def assert_error(self, result, code: CalendarErrorCode) -> None:
        self.assertIsInstance(result, CalendarError)
        self.assertEqual(result.code, code)

    def test_identity_absent_or_case_insensitive_and_bom_are_accepted(self):
        document = _ics("BEGIN:VCALENDAR", "END:VCALENDAR")
        payload = b"\xef\xbb\xbf" + document.encode("utf-8")
        for encoding in (None, "identity", "IDENTITY"):
            with self.subTest(encoding=encoding):
                headers = {} if encoding is None else {"Content-Encoding": encoding}
                response = _Response(payload, headers=headers, max_chunk=3)
                result = decode_calendar_response(response)
                self.assertIsInstance(result, CalendarSuccess)
                self.assertEqual(result.value, document)
                self.assertGreater(len(response.read_sizes), 1)

        self.assertEqual(
            read_calendar_response_text(_Response(payload, max_chunk=2)),
            document,
        )

    def test_case_insensitive_gzip_is_stream_decoded(self):
        document = _ics(
            "BEGIN:VCALENDAR",
            "NAME:휴가 캘린더",
            "END:VCALENDAR",
        )
        response = _Response(
            gzip.compress(document.encode("utf-8")),
            headers={"Content-Encoding": "GZiP"},
            max_chunk=5,
        )
        result = decode_calendar_response(response)
        self.assertIsInstance(result, CalendarSuccess)
        self.assertEqual(result.value, document)
        self.assertGreater(len(response.read_sizes), 1)

    def test_declared_wire_and_decoded_limits_are_enforced(self):
        declared = _Response(
            b"ignored",
            headers={"content-length": str(MAX_ICAL_BYTES + 1)},
        )
        self.assert_error(
            decode_calendar_response(declared),
            CalendarErrorCode.BODY_TOO_LARGE,
        )
        self.assertEqual(declared.read_sizes, [])

        self.assert_error(
            decode_calendar_response(_Response(b"123456789"), limit_bytes=8),
            CalendarErrorCode.BODY_TOO_LARGE,
        )

        compressed = gzip.compress(b"A" * 65)
        self.assertLessEqual(len(compressed), 64)
        self.assert_error(
            decode_calendar_response(
                _Response(compressed, headers={"Content-Encoding": "gzip"}),
                limit_bytes=64,
            ),
            CalendarErrorCode.BODY_TOO_LARGE,
        )

    def test_corrupt_and_truncated_gzip_are_rejected(self):
        candidates = (
            b"not-a-gzip-stream",
            gzip.compress(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR")[:-4],
        )
        for body in candidates:
            with self.subTest(body_length=len(body)):
                self.assert_error(
                    decode_calendar_response(
                        _Response(body, headers={"Content-Encoding": "gzip"})
                    ),
                    CalendarErrorCode.INVALID_ICAL,
                )

    def test_invalid_utf8_is_distinct(self):
        self.assert_error(
            decode_calendar_response(_Response(b"\xff\xfe\xfa")),
            CalendarErrorCode.UTF8_DECODE,
        )

    def test_unsupported_and_multiple_content_encodings_are_rejected(self):
        for encoding in ("br", "gzip, identity", "identity,gzip"):
            with self.subTest(encoding=encoding):
                self.assert_error(
                    decode_calendar_response(
                        _Response(b"payload", headers={"Content-Encoding": encoding})
                    ),
                    CalendarErrorCode.UNSUPPORTED_ENCODING,
                )

    def test_empty_body_and_generic_read_failure_are_closed_errors(self):
        self.assert_error(
            decode_calendar_response(_Response(b"")),
            CalendarErrorCode.EMPTY_BODY,
        )
        self.assert_error(
            decode_calendar_response(
                _Response(b"", headers={"Content-Encoding": "gzip"})
            ),
            CalendarErrorCode.EMPTY_BODY,
        )
        self.assert_error(
            decode_calendar_response(
                _Response(
                    gzip.compress(b""),
                    headers={"Content-Encoding": "gzip"},
                )
            ),
            CalendarErrorCode.EMPTY_BODY,
        )
        failed = _Response(b"unused", fail_after=0)
        result = decode_calendar_response(failed)
        self.assert_error(result, CalendarErrorCode.DNS_OR_CONNECT)
        self.assertNotIn("synthetic read failure", repr(result))
        self.assertIsNone(
            read_calendar_response_text(_Response(b"unused", fail_after=0))
        )

    def test_read_transport_failures_are_classified_without_details(self):
        sentinel = "private-read-exception-sentinel"
        failures = (
            (
                "timeout",
                TimeoutError(f"{sentinel}-timeout"),
                CalendarErrorCode.TIMEOUT,
            ),
            (
                "socket-timeout",
                socket.timeout(f"{sentinel}-socket-timeout"),
                CalendarErrorCode.TIMEOUT,
            ),
            (
                "certificate",
                ssl.CertificateError(f"{sentinel}-certificate"),
                CalendarErrorCode.TLS_VALIDATION,
            ),
            (
                "ssl",
                ssl.SSLError(f"{sentinel}-ssl"),
                CalendarErrorCode.TLS_VALIDATION,
            ),
            (
                "http-4xx",
                urllib.error.HTTPError(
                    f"https://example.invalid/{sentinel}",
                    403,
                    f"{sentinel}-http-4xx",
                    {},
                    None,
                ),
                CalendarErrorCode.HTTP_4XX,
            ),
            (
                "http-5xx",
                urllib.error.HTTPError(
                    f"https://example.invalid/{sentinel}",
                    503,
                    f"{sentinel}-http-5xx",
                    {},
                    None,
                ),
                CalendarErrorCode.HTTP_5XX,
            ),
            (
                "url-timeout",
                urllib.error.URLError(TimeoutError(f"{sentinel}-url-timeout")),
                CalendarErrorCode.TIMEOUT,
            ),
            (
                "url-tls",
                urllib.error.URLError(ssl.SSLError(f"{sentinel}-url-tls")),
                CalendarErrorCode.TLS_VALIDATION,
            ),
            (
                "url-connect",
                urllib.error.URLError(ConnectionError(f"{sentinel}-url-connect")),
                CalendarErrorCode.DNS_OR_CONNECT,
            ),
            (
                "connection",
                ConnectionError(f"{sentinel}-connection"),
                CalendarErrorCode.DNS_OR_CONNECT,
            ),
            (
                "os-error",
                OSError(f"{sentinel}-os-error"),
                CalendarErrorCode.DNS_OR_CONNECT,
            ),
            (
                "remaining",
                RuntimeError(f"{sentinel}-remaining"),
                CalendarErrorCode.DNS_OR_CONNECT,
            ),
        )
        for name, failure, expected_code in failures:
            with self.subTest(name=name):
                result = decode_calendar_response(
                    _Response(b"unused", fail_after=0, read_error=failure)
                )
                self.assert_error(result, expected_code)
                self.assertNotIn(sentinel, repr(result))
                self.assertNotIn(str(failure), repr(result))
                close_failure = getattr(failure, "close", None)
                if callable(close_failure):
                    close_failure()


class StrictCalendarDocumentUnitTest(unittest.TestCase):
    def assert_invalid(self, text: str) -> None:
        result = parse_calendar_document(text)
        self.assertIsInstance(result, CalendarError)
        self.assertEqual(result.code, CalendarErrorCode.INVALID_ICAL)

    def test_malformed_component_structures_are_rejected(self):
        documents = {
            "missing root": _ics(
                "BEGIN:VEVENT",
                "DTSTART:20260824T090000",
                "END:VEVENT",
            ),
            "property before root": _ics(
                "VERSION:2.0",
                "BEGIN:VCALENDAR",
                "END:VCALENDAR",
            ),
            "mismatched close": _ics(
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "END:VTODO",
                "END:VCALENDAR",
            ),
            "premature root close": _ics(
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "END:VCALENDAR",
                "END:VEVENT",
            ),
            "unclosed child": _ics(
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "END:VCALENDAR",
            ),
            "property after root": _ics(
                "BEGIN:VCALENDAR",
                "END:VCALENDAR",
                "VERSION:2.0",
            ),
            "malformed property": _ics(
                "BEGIN:VCALENDAR",
                "NOT-A-CONTENT-LINE",
                "END:VCALENDAR",
            ),
            "nested calendar": _ics(
                "BEGIN:VCALENDAR",
                "BEGIN:VCALENDAR",
                "END:VCALENDAR",
                "END:VCALENDAR",
            ),
        }
        for name, document in documents.items():
            with self.subTest(name=name):
                self.assert_invalid(document)

    def test_duplicate_calendar_roots_are_rejected(self):
        self.assert_invalid(
            _ics(
                "BEGIN:VCALENDAR",
                "END:VCALENDAR",
                "BEGIN:VCALENDAR",
                "END:VCALENDAR",
            )
        )

    def test_valid_empty_calendar_is_accepted(self):
        result = parse_calendar_document(
            _ics("BEGIN:VCALENDAR", "END:VCALENDAR")
        )
        self.assertIsInstance(result, CalendarSuccess)
        self.assertEqual(
            result.value,
            {"calendar_name": "", "timezone": "", "events": []},
        )

    def test_empty_calendar_text_has_a_distinct_error(self):
        for text in ("", "\ufeff", "\r\n\t"):
            with self.subTest(text=repr(text)):
                result = parse_calendar_document(text)
                self.assertIsInstance(result, CalendarError)
                self.assertEqual(result.code, CalendarErrorCode.EMPTY_BODY)

    def test_name_is_fallback_with_folding_and_nested_name_is_ignored(self):
        parsed = parse_calendar(
            _ics(
                "BEGIN:VCALENDAR",
                "NAME:Fallback Cal",
                " endar",
                "BEGIN:VTIMEZONE",
                "NAME:Nested timezone name",
                "END:VTIMEZONE",
                "BEGIN:VEVENT",
                "NAME:Nested event name",
                "DTSTART:20260824T090000",
                "END:VEVENT",
                "END:VCALENDAR",
            )
        )
        self.assertEqual(parsed["calendar_name"], "Fallback Calendar")

    def test_x_wr_calname_wins_before_or_after_name(self):
        property_orders = (
            ("X-WR-CALNAME:Preferred", "NAME:Fallback"),
            ("NAME:Fallback", "X-WR-CALNAME:Preferred"),
        )
        for properties in property_orders:
            with self.subTest(properties=properties):
                result = parse_calendar_document(
                    _ics(
                        "BEGIN:VCALENDAR",
                        *properties,
                        "END:VCALENDAR",
                    )
                )
                self.assertIsInstance(result, CalendarSuccess)
                self.assertEqual(result.value["calendar_name"], "Preferred")

    def test_vacation_compile_discards_private_identity_title_and_uid(self):
        calendar_name = "김종인-ePapyrus"
        private_uid = "private-vacation-uid@example.invalid"
        private_title = "private vacation title 휴가"
        document = _ics(
            "BEGIN:VCALENDAR",
            f"X-WR-CALNAME:{calendar_name}",
            "BEGIN:VEVENT",
            f"UID:{private_uid}",
            "DTSTART:20260406T090000",
            "DTEND:20260406T100000",
            "RRULE:FREQ=DAILY;COUNT=2",
            f"SUMMARY:{private_title}",
            "END:VEVENT",
            "BEGIN:VEVENT",
            f"UID:{private_uid}",
            "RECURRENCE-ID:20260407T090000",
            "DTSTART:20260407T130000",
            "DTEND:20260407T140000",
            "END:VEVENT",
            "END:VCALENDAR",
        )

        compiled = compile_vacation_calendar(
            parse_calendar(document),
            calendar_name,
        )

        self.assertEqual(
            set(compiled),
            {"vacation_schema_version", "calendar_matched", "events"},
        )
        self.assertTrue(compiled["calendar_matched"])
        self.assertNotIn(calendar_name, repr(compiled))
        self.assertNotIn(private_title, repr(compiled))
        self.assertNotIn(private_uid, repr(compiled))
        self.assertTrue(compiled["events"])
        self.assertTrue(all(event["vacation_match"] for event in compiled["events"]))
        self.assertTrue(
            all(
                "summary" not in event and "uid" not in event
                for event in compiled["events"]
            )
        )

        result = vacation_events_for_day(
            compiled,
            calendar_name,
            date(2026, 4, 7),
        )
        self.assertEqual(
            set(result),
            {"calendar_matched", "all_day", "intervals", "event_count"},
        )
        self.assertEqual(
            result["intervals"],
            [(datetime(2026, 4, 7, 13, 0), datetime(2026, 4, 7, 14, 0))],
        )
        self.assertEqual(result["event_count"], 1)

    def test_result_representations_do_not_expose_calendar_payload(self):
        private_markers = (
            "https://calendar.google.com/private.ics?secret=query-token",
            "Location: https://redirect.example/private-target",
            "socket exploded with private detail",
            "private response body marker",
            "private calendar name",
            "private vacation event title",
        )
        document = _ics(
            "BEGIN:VCALENDAR",
            f"NAME:{private_markers[4]}",
            f"X-PRIVATE-URL:{private_markers[0]}",
            f"X-PRIVATE-LOCATION:{private_markers[1]}",
            f"X-PRIVATE-ERROR:{private_markers[2]}",
            f"X-PRIVATE-BODY:{private_markers[3]}",
            "BEGIN:VEVENT",
            "DTSTART:20260824T090000",
            f"SUMMARY:{private_markers[5]}",
            "END:VEVENT",
            "END:VCALENDAR",
        )
        decoded = decode_calendar_response(_Response(document.encode("utf-8")))
        parsed = parse_calendar_document(document)
        invalid = parse_calendar_document(f"X-PRIVATE:{private_markers[3]}")
        for result in (decoded, parsed, invalid):
            with self.subTest(result_type=type(result).__name__):
                self.assertNotIn(document, repr(result))
                for marker in private_markers:
                    self.assertNotIn(marker, repr(result))

    def test_error_code_set_is_closed_and_stable(self):
        self.assertEqual(
            {code.value for code in CalendarErrorCode},
            {
                "invalid_endpoint",
                "redirect_rejected",
                "http_4xx",
                "http_5xx",
                "dns_or_connect",
                "timeout",
                "tls_validation",
                "body_too_large",
                "unsupported_encoding",
                "utf8_decode",
                "empty_body",
                "invalid_ical",
            },
        )


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
