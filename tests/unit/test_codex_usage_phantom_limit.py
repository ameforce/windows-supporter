"""Reset alerts only for limit windows the profile actually has (hotfix v0.35.1).

The ChatGPT usage page of a weekly-only plan (Codex Pro) shows a reset-credit
section whose description names the 5-hour limit and lists credits with
expiry times. The parser used to turn that description into a 5-hour limit
without a value, attached a credit expiry as its reset time and alerted
"5시간 사용 한도 초기화" with the fanfare whenever the expiry moved forward.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.apps.codex_local_usage import LocalCodexUsageSnapshot
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    advance_limit_reset_baselines,
    compute_usage_limit_resets,
    extract_reported_usage_metric_keys_from_semantic_blocks,
    extract_usage_metrics_from_semantic_blocks,
    extract_usage_reset_info_from_semantic_blocks,
    merge_snapshot_with_previous,
    reconcile_snapshot_with_local_codex_usage,
)

KST = timezone(timedelta(hours=9))
USAGE_URL = "https://chatgpt.com/codex/cloud/settings/analytics#usage"
# Korean UI text of the usage page's reset-credit section (ChatGPT bundle key
# chatgpt.settings.usage.reset_credits.section.description).
RESET_CREDIT_DESCRIPTION = "재설정을 사용해 5시간 한도, 주간 한도 또는 둘 다를 복원하세요."
RESET_CREDIT_DESCRIPTION_EN = "Use a reset to restore your 5-hour limit, weekly limit, or both."


def _captured(dt: datetime) -> str:
    # Same shape as CodexUsageMonitor.__now_iso().
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _js_iso(dt: datetime) -> str:
    # Same shape as the probe's Date.toISOString().
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _korean_dotted(dt: datetime) -> str:
    local = dt.astimezone(KST)
    marker = "오전" if local.hour < 12 else "오후"
    hour = local.hour % 12 or 12
    return f"{local.year}. {local.month}. {local.day}. {marker} {hour}:{local.minute:02d}"


def _reset_credit_block(expiry: datetime, label_text: str = RESET_CREDIT_DESCRIPTION) -> dict:
    return {
        "metric_key": "five_hour_limit",
        "label_text": label_text,
        "block_text": label_text,
        "heading_text": "사용량 한도 재설정",
        "value_candidates": [],
        "reset_candidates": [f"사용량 재설정 {_korean_dotted(expiry)}에 만료"],
        "reset_at_candidates": [_js_iso(expiry)],
    }


def _weekly_card(value: str = "100% 남음", reset_at: datetime | None = None) -> dict:
    block_text = f"주간 사용 한도 {value}"
    reset_candidates: list[str] = []
    reset_at_candidates: list[str] = []
    if reset_at is not None:
        reset_text = f"{_korean_dotted(reset_at)} 초기화"
        block_text = f"{block_text} {reset_text}"
        reset_candidates = [reset_text]
        reset_at_candidates = [_js_iso(reset_at)]
    return {
        "metric_key": "weekly_limit",
        "label_text": "주간 사용 한도",
        "block_text": block_text,
        "value_candidates": [value],
        "reset_candidates": reset_candidates,
        "reset_at_candidates": reset_at_candidates,
    }


def _five_hour_card(value: str, reset_at: datetime) -> dict:
    reset_text = f"{_korean_dotted(reset_at)} 초기화"
    return {
        "metric_key": "five_hour_limit",
        "label_text": "5시간 사용 한도",
        "block_text": f"5시간 사용 한도 {value} {reset_text}",
        "value_candidates": [value],
        "reset_candidates": [reset_text],
        "reset_at_candidates": [_js_iso(reset_at)],
    }


def _probe(blocks: list[dict]) -> dict:
    return {
        "url": USAGE_URL,
        "title": "Codex",
        "mainText": " ".join(str(block.get("block_text", "")) for block in blocks),
        "profileName": "",
        "metricBlocks": blocks,
    }


def _observe(blocks: list[dict], captured_at: datetime) -> UsageSnapshot:
    captured = _captured(captured_at)
    return UsageSnapshot.from_metrics(
        extract_usage_metrics_from_semantic_blocks(blocks),
        captured_at=captured,
        reset_info=extract_usage_reset_info_from_semantic_blocks(blocks, captured_at=captured),
        reported_metric_keys=extract_reported_usage_metric_keys_from_semantic_blocks(blocks),
    )


class PhantomLimitParsingTest(unittest.TestCase):
    def test_reset_credit_description_is_not_a_five_hour_limit(self) -> None:
        now = datetime(2026, 9, 30, 11, 25, tzinfo=KST)
        blocks = [_weekly_card(), _reset_credit_block(now + timedelta(minutes=21))]

        snapshot = _observe(blocks, now)

        self.assertEqual(snapshot.reported_metric_keys, ("weekly_limit",))
        self.assertEqual(snapshot.five_hour_limit, "")
        self.assertEqual(snapshot.five_hour_limit_reset_at, "")
        self.assertEqual(snapshot.weekly_limit, "100%")

    def test_reset_time_is_taken_only_from_the_block_that_measures_the_limit(self) -> None:
        # A bare limit name without a value (for example emphasised words in
        # a description) must not lend a neighbouring deadline to the limit.
        now = datetime(2026, 9, 30, 11, 25, tzinfo=KST)
        expiry = now + timedelta(minutes=21)
        label_only = _reset_credit_block(expiry, label_text="5시간 한도")

        self.assertEqual(
            extract_usage_reset_info_from_semantic_blocks([label_only], captured_at=_captured(now)),
            {},
        )
        deadline = now + timedelta(hours=3)
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [label_only, _five_hour_card("42% 남음", deadline)],
            captured_at=_captured(now),
        )
        self.assertEqual(parsed.get("five_hour_limit_reset_at"), _js_iso(deadline))

    def test_block_without_a_label_does_not_trust_the_probe_guess_for_prose(self) -> None:
        # The probe's metric_key is the same longest-alias guess the parser
        # now rejects for text naming several limits.
        block = {"metric_key": "five_hour_limit", "block_text": RESET_CREDIT_DESCRIPTION}

        self.assertEqual(extract_reported_usage_metric_keys_from_semantic_blocks([block]), ())

    def test_card_without_a_parsed_value_gives_local_usage_no_deadline_to_match(self) -> None:
        # Trade-off of taking deadlines only from measured cards: local Codex
        # values are combined only when the web deadline matches, so a web
        # card whose value does not parse keeps the web reading as is.
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        deadline = now + timedelta(hours=3)
        observed = _observe(
            [_five_hour_card("—", deadline), _weekly_card("64% 남음")],
            now,
        )
        local = LocalCodexUsageSnapshot(
            captured_at=_js_iso(now - timedelta(seconds=20)),
            account_id="acct-1",
            plan_type="plus",
            five_hour_limit="40%",
            five_hour_limit_reset_at=_js_iso(deadline),
            reported_metric_keys=("five_hour_limit",),
        )

        reconciled = reconcile_snapshot_with_local_codex_usage(
            observed,
            local,
            web_account_id="acct-1",
            web_plan_type="plus",
        )

        self.assertEqual(observed.five_hour_limit_reset_at, "")
        self.assertEqual(reconciled.five_hour_limit, "")
        self.assertEqual(reconciled.weekly_limit, "64%")

    def test_english_reset_credit_description_lends_no_weekly_deadline(self) -> None:
        now = datetime(2026, 9, 30, 11, 25, tzinfo=KST)
        blocks = [
            _reset_credit_block(now + timedelta(days=3), label_text=RESET_CREDIT_DESCRIPTION_EN),
            _weekly_card("64% left"),
        ]

        parsed = extract_usage_reset_info_from_semantic_blocks(blocks, captured_at=_captured(now))

        self.assertNotIn("weekly_limit_reset_at", parsed)
        self.assertEqual(extract_usage_metrics_from_semantic_blocks(blocks).get("weekly_limit"), "64%")


class MeasuredWindowDetectionTest(unittest.TestCase):
    def test_reset_time_jump_without_a_measured_value_is_not_a_reset(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now),
            reset_info={"five_hour_limit_reset_at": _js_iso(now + timedelta(hours=21))},
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
        )

        resets = compute_usage_limit_resets(
            {"five_hour_limit": _js_iso(now - timedelta(minutes=14))},
            observed,
            observed_snapshot=observed,
            now=now,
        )

        self.assertEqual(resets, [])

    def test_deadline_of_an_unmeasured_window_does_not_become_a_baseline(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now),
            reset_info={"five_hour_limit_reset_at": _js_iso(now + timedelta(hours=2))},
        )
        baselines: dict[str, str] = {}

        advance_limit_reset_baselines(baselines, observed)

        self.assertNotIn("five_hour_limit", baselines)

    def test_baseline_of_a_window_the_page_no_longer_reports_is_dropped(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        weekly_reset = now + timedelta(days=1)
        previous = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now - timedelta(minutes=1)),
            reset_info={"five_hour_limit_reset_at": "2026-09-25T20:57:00.000Z"},
        )
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now),
            reset_info={"weekly_limit_reset_at": _js_iso(weekly_reset)},
            reported_metric_keys=("weekly_limit",),
        )
        baselines = {"five_hour_limit": "2026-09-25T20:57:00.000Z"}

        advance_limit_reset_baselines(
            baselines,
            observed,
            observed_snapshot=observed,
            previous_snapshot=previous,
        )

        self.assertEqual(baselines, {"weekly_limit": _js_iso(weekly_reset)})

    def test_one_page_without_a_measured_window_keeps_its_baseline(self) -> None:
        # A page that briefly renders without the 5-hour card must not erase
        # the deadline the next reading's reset is compared against.
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        deadline = _js_iso(now - timedelta(minutes=5))
        previous = UsageSnapshot.from_metrics(
            {"five_hour_limit": "3%", "weekly_limit": "64%"},
            captured_at=_captured(now - timedelta(minutes=10)),
            reset_info={"five_hour_limit_reset_at": deadline},
        )
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "64%"},
            captured_at=_captured(now),
            reported_metric_keys=("weekly_limit",),
        )
        baselines = {"five_hour_limit": deadline}

        advance_limit_reset_baselines(
            baselines,
            observed,
            observed_snapshot=observed,
            previous_snapshot=previous,
        )

        self.assertEqual(baselines, {"five_hour_limit": deadline})

    def test_merge_carries_no_deadline_for_a_window_without_any_value(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        previous = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now - timedelta(minutes=1)),
            reset_info={"five_hour_limit_reset_at": _js_iso(now - timedelta(minutes=14))},
        )
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            captured_at=_captured(now),
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
        )

        merged = merge_snapshot_with_previous(observed, previous)

        self.assertEqual(merged.five_hour_limit, "")
        self.assertEqual(merged.five_hour_limit_reset_at, "")

    def test_label_only_window_keeps_its_baseline_for_a_transient_parse_gap(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=KST)
        observed = UsageSnapshot.from_metrics(
            {"weekly_limit": "80%"},
            captured_at=_captured(now),
            reset_info={"five_hour_limit_reset_at": _js_iso(now + timedelta(hours=4))},
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
        )
        baselines = {"five_hour_limit": _js_iso(now - timedelta(minutes=5))}

        advance_limit_reset_baselines(baselines, observed, observed_snapshot=observed)

        self.assertEqual(baselines, {"five_hour_limit": _js_iso(now - timedelta(minutes=5))})


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []

    def after(self, delay, fn):
        self.after_calls.append((delay, fn))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None


class WeeklyOnlyProfileAlertTest(unittest.TestCase):
    def _make_monitor(self, config_dir: str) -> CodexUsageMonitor:
        class _Session:
            def shutdown(self) -> bool:
                return True

        monitor = CodexUsageMonitor(
            config_dir=config_dir,
            profile_dir=os.path.join(config_dir, "profile"),
            browser_session_factory=lambda _config: _Session(),
        )
        monitor._CodexUsageMonitor__root = _FakeRoot()
        return monitor

    def _collect(self, monitor: CodexUsageMonitor, blocks: list[dict], at: datetime) -> None:
        captured = _captured(at)
        with patch.object(monitor, "_CodexUsageMonitor__now_iso", return_value=captured):
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(_probe(blocks))
        if snapshot is None:
            self.fail("usage probe was rejected")
        monitor.handle_snapshot(snapshot)

    def _run(self, monitor: CodexUsageMonitor, steps) -> tuple[list, int]:
        shown: list = []
        with patch.object(
            monitor,
            "_CodexUsageMonitor__ui_post",
            side_effect=lambda fn: fn(),
        ), patch.object(
            monitor,
            "_CodexUsageMonitor__get_last_input_tick",
            return_value=None,
            create=True,
        ), patch.object(
            monitor,
            "_CodexUsageMonitor__show_alert_tooltip",
            side_effect=lambda text, lines=None, duration_ms=None: shown.append(lines),
        ), patch(
            "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
        ) as play_mock:
            for blocks, at in steps:
                self._collect(monitor, blocks, at)
        return shown, play_mock.call_count

    def test_reset_credit_expiry_moving_forward_does_not_alert_a_five_hour_reset(self) -> None:
        now = datetime.now(KST).replace(microsecond=0)
        first_expiry = now - timedelta(minutes=5)
        next_expiry = now + timedelta(hours=20)
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)

            shown, sounds = self._run(
                monitor,
                [
                    ([_weekly_card(), _reset_credit_block(first_expiry)], now - timedelta(hours=2)),
                    ([_weekly_card(), _reset_credit_block(next_expiry)], now),
                ],
            )

            self.assertEqual(shown, [])
            self.assertEqual(sounds, 0)
            self.assertNotIn("five_hour_limit", monitor._CodexUsageMonitor__limit_reset_baselines)
            self.assertEqual(monitor.get_last_snapshot().five_hour_limit_reset_at, "")

    def test_stale_five_hour_baseline_from_before_the_fix_is_dropped_silently(self) -> None:
        now = datetime.now(KST).replace(microsecond=0)
        phantom_reset = _js_iso(now - timedelta(minutes=14))
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "codex_usage_state.json"), "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "snapshot_contract_version": 2,
                        "session_state": "logged_in",
                        "snapshot_backfill_allowed": True,
                        "account_id": "",
                        "last_snapshot": {
                            "weekly_limit": "100%",
                            "captured_at": _captured(now - timedelta(minutes=1)),
                            "five_hour_limit_reset_at": phantom_reset,
                        },
                        "usage_history": [],
                        "limit_reset_baselines": {"five_hour_limit": phantom_reset},
                    },
                    fp,
                )
            monitor = self._make_monitor(tmp)

            shown, sounds = self._run(
                monitor,
                [([_weekly_card(), _reset_credit_block(now + timedelta(hours=21))], now)],
            )

            self.assertEqual(shown, [])
            self.assertEqual(sounds, 0)
            self.assertNotIn("five_hour_limit", monitor._CodexUsageMonitor__limit_reset_baselines)
            self.assertEqual(monitor.get_last_snapshot().five_hour_limit_reset_at, "")

    def test_real_five_hour_window_next_to_reset_credits_still_alerts_once(self) -> None:
        now = datetime.now(KST).replace(microsecond=0)
        first_deadline = now - timedelta(minutes=10)
        next_deadline = now + timedelta(hours=4, minutes=50)
        credit_expiry = now + timedelta(hours=8)
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)

            shown, sounds = self._run(
                monitor,
                [
                    (
                        [
                            _reset_credit_block(credit_expiry),
                            _five_hour_card("3% 남음", first_deadline),
                            _weekly_card("64% 남음"),
                        ],
                        now - timedelta(hours=1),
                    ),
                    (
                        [
                            _reset_credit_block(credit_expiry),
                            _five_hour_card("97% 남음", next_deadline),
                            _weekly_card("63% 남음"),
                        ],
                        now,
                    ),
                ],
            )

            self.assertEqual(len(shown), 1)
            self.assertEqual(sounds, 1)
            joined = " | ".join(str(line[0]) for line in (shown[0] or []))
            self.assertIn("5시간 사용 한도 초기화됨", joined)
            self.assertNotIn("주간 사용 한도 초기화됨", joined)
            self.assertEqual(
                monitor.get_last_snapshot().five_hour_limit_reset_at,
                _js_iso(next_deadline),
            )

    def test_one_page_without_the_five_hour_card_does_not_lose_a_real_reset(self) -> None:
        now = datetime.now(KST).replace(microsecond=0)
        first_deadline = now - timedelta(minutes=10)
        for label, after_reset in (
            ("new deadline", _five_hour_card("100% 남음", now + timedelta(hours=5))),
            (
                "no deadline",
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5시간 사용 한도",
                    "block_text": "5시간 사용 한도 100% 남음",
                    "value_candidates": ["100% 남음"],
                },
            ),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                monitor = self._make_monitor(tmp)

                shown, sounds = self._run(
                    monitor,
                    [
                        (
                            [_five_hour_card("3% 남음", first_deadline), _weekly_card("64% 남음")],
                            now - timedelta(hours=1),
                        ),
                        ([_weekly_card("64% 남음")], now - timedelta(minutes=2)),
                        ([after_reset, _weekly_card("63% 남음")], now),
                    ],
                )

                self.assertEqual(len(shown), 1)
                self.assertEqual(sounds, 1)
                joined = " | ".join(str(line[0]) for line in (shown[0] or []))
                self.assertIn("5시간 사용 한도 초기화됨", joined)

    def test_unparseable_value_defers_a_real_reset_to_the_next_measured_reading(self) -> None:
        now = datetime.now(KST).replace(microsecond=0)
        first_deadline = now - timedelta(minutes=10)
        next_deadline = now + timedelta(hours=4, minutes=50)
        loading_card = _five_hour_card("—", next_deadline)
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)

            shown, sounds = self._run(
                monitor,
                [
                    (
                        [_five_hour_card("3% 남음", first_deadline), _weekly_card("64% 남음")],
                        now - timedelta(hours=1),
                    ),
                    ([loading_card, _weekly_card("64% 남음")], now - timedelta(minutes=1)),
                ],
            )
            self.assertEqual(shown, [])
            self.assertEqual(
                monitor._CodexUsageMonitor__limit_reset_baselines.get("five_hour_limit"),
                _js_iso(first_deadline),
            )

            shown, sounds = self._run(
                monitor,
                [([_five_hour_card("97% 남음", next_deadline), _weekly_card("63% 남음")], now)],
            )

            self.assertEqual(len(shown), 1)
            self.assertEqual(sounds, 1)

    def test_logout_forgets_the_released_accounts_reset_deadlines(self) -> None:
        # Another account may sign in after a logout; the old account's
        # elapsed deadline must not turn its first reading into a reset.
        class _ReleasableSession:
            def collect(self):
                raise AssertionError("not collected in this test")

            def request_cancel(self) -> bool:
                return True

            def close_session(self) -> None:
                return None

            def shutdown(self) -> bool:
                return True

        now = datetime.now(KST).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: _ReleasableSession(),
            )
            monitor._CodexUsageMonitor__root = _FakeRoot()
            self._run(
                monitor,
                [
                    (
                        [_five_hour_card("3% 남음", now - timedelta(minutes=10)), _weekly_card("64% 남음")],
                        now - timedelta(hours=1),
                    )
                ],
            )
            with patch.object(
                monitor,
                "_CodexUsageMonitor__clear_profile_directory",
                return_value=(True, "로그아웃되었습니다."),
            ):
                ok, message = monitor.release_profile_session()
            self.assertTrue(ok, message)
            self.assertEqual(monitor._CodexUsageMonitor__limit_reset_baselines, {})

            shown, sounds = self._run(
                monitor,
                [
                    (
                        [_five_hour_card("40% 남음", now + timedelta(hours=3)), _weekly_card("90% 남음")],
                        now,
                    )
                ],
            )

            self.assertEqual(shown, [])
            self.assertEqual(sounds, 0)

    def test_limit_name_in_its_own_element_stays_silent_on_a_weekly_only_page(self) -> None:
        # If the description renders "5시간 한도" in its own element, that
        # element is a label without a value: it may keep the window listed
        # but can neither carry a deadline nor raise an alert.
        now = datetime.now(KST).replace(microsecond=0)
        phantom_reset = _js_iso(now - timedelta(minutes=14))
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "codex_usage_state.json"), "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "snapshot_contract_version": 2,
                        "session_state": "logged_in",
                        "snapshot_backfill_allowed": True,
                        "account_id": "",
                        "last_snapshot": {
                            "weekly_limit": "100%",
                            "captured_at": _captured(now - timedelta(minutes=1)),
                            "five_hour_limit_reset_at": phantom_reset,
                        },
                        "usage_history": [],
                        "limit_reset_baselines": {"five_hour_limit": phantom_reset},
                    },
                    fp,
                )
            monitor = self._make_monitor(tmp)

            def page(expiry: datetime) -> list[dict]:
                return [
                    _weekly_card(),
                    _reset_credit_block(expiry),
                    _reset_credit_block(expiry, label_text="5시간 한도"),
                ]

            shown, sounds = self._run(
                monitor,
                [
                    (page(now + timedelta(hours=21)), now - timedelta(seconds=30)),
                    (page(now + timedelta(hours=30)), now),
                ],
            )

            self.assertEqual(shown, [])
            self.assertEqual(sounds, 0)
            self.assertEqual(monitor.get_last_snapshot().five_hour_limit_reset_at, "")


if __name__ == "__main__":
    unittest.main()
