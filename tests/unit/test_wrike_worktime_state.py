from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src.apps.wrike_worktime_state import STATE_VERSION, WorktimeStateStore


class WorktimeStateStoreV3Tests(unittest.TestCase):
    def _write_json(self, path: Path, value) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _empty_day(**extra) -> dict:
        value = {
            "manual_breaks": [],
            "active_break_started_at": None,
        }
        value.update(extra)
        return value

    def test_nested_v2_migrates_atomically_without_losing_plan_or_breaks(self) -> None:
        original = {
            "state_version": 2,
            "days": {
                "2026-04-06": self._empty_day(
                    manual_breaks=[
                        {
                            "start": "2026-04-06T12:00:00.125",
                            "end": "2026-04-06T12:30:00.625",
                        }
                    ],
                    plan={
                        "target_net_minutes": 450,
                        "clock_in": "08:35",
                    },
                ),
                "2026-04-07": self._empty_day(
                    active_break_started_at="2026-04-07T15:10:00"
                ),
            },
        }
        expected = copy.deepcopy(original)
        expected["state_version"] = STATE_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self._write_json(path, original)
            with mock.patch(
                "src.apps.wrike_worktime_state.os.replace",
                wraps=os.replace,
            ) as replace:
                store = WorktimeStateStore(path)

            self.assertEqual(store.snapshot(), expected)
            self.assertEqual(self._read_json(path), expected)
            replace.assert_called_once()
            temp_name, destination = replace.call_args.args
            self.assertEqual(Path(destination), path)
            self.assertEqual(Path(temp_name).parent, path.parent)
            self.assertFalse(Path(temp_name).exists())
            self.assertEqual(
                store.get_day_plan("2026-04-06"),
                {
                    "date": "2026-04-06",
                    "target_net_minutes": 450,
                    "clock_in": "08:35",
                    "explicit": True,
                },
            )

    def test_migration_save_failure_keeps_decoded_v2_but_blocks_later_writes(self) -> None:
        original = {
            "state_version": 2,
            "days": {
                "2026-04-06": self._empty_day(
                    plan={
                        "target_net_minutes": 480,
                        "clock_in": "09:00",
                    }
                )
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self._write_json(path, original)
            with mock.patch.object(
                WorktimeStateStore,
                "_save_locked",
                return_value=False,
            ):
                store = WorktimeStateStore(path)

            snapshot = store.snapshot()
            self.assertEqual(snapshot["state_version"], 3)
            self.assertEqual(snapshot["days"]["2026-04-06"]["plan"]["clock_in"], "09:00")
            ok, error = store.record_activity_prompt_pending(
                "2026-04-06",
                datetime(2026, 4, 6, 9, 10),
            )
            self.assertFalse(ok)
            self.assertIn("마이그레이션", error or "")
            self.assertEqual(self._read_json(path), original)

    def test_recognized_legacy_migrates_but_unknown_or_malformed_data_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_path = root / "legacy.json"
            self._write_json(
                legacy_path,
                {"first_seen_by_date": {"2026-04-06": "2026-04-06T08:59:00"}},
            )
            legacy_store = WorktimeStateStore(legacy_path)
            self.assertEqual(
                legacy_store.snapshot(),
                {"state_version": 3, "days": {}},
            )
            self.assertEqual(self._read_json(legacy_path), legacy_store.snapshot())

            rejected_values = {
                "published-v1": {"state_version": 1, "days": {}},
                "future-version": {"state_version": 4, "days": {}},
                "unknown-top-level": {
                    "state_version": 3,
                    "days": {},
                    "unknown": True,
                },
                "malformed-legacy": {
                    "first_seen_by_date": {
                        "2026-04-06": "2026-04-06T08:59:00+09:00"
                    }
                },
                "v2-with-v3-field": {
                    "state_version": 2,
                    "days": {
                        "2026-04-06": self._empty_day(
                            activity_prompt={
                                "status": "pending",
                                "detected_at": "2026-04-06T09:00:00",
                            }
                        )
                    },
                },
            }
            for name, raw in rejected_values.items():
                with self.subTest(name=name):
                    path = root / f"{name}.json"
                    self._write_json(path, raw)
                    store = WorktimeStateStore(path)
                    self.assertEqual(store.snapshot(), {"state_version": 3, "days": {}})
                    ok, error = store.update_day_plan("2026-04-06", 480, "09:00")
                    self.assertFalse(ok)
                    self.assertIn("덮어쓰기", error or "")
                    self.assertEqual(self._read_json(path), raw)

    def test_strict_json_loader_rejects_duplicate_keys_and_constants_fail_closed(self) -> None:
        non_finite_template = (
            '{"state_version":3,"days":{"2026-04-06":{'
            '"manual_breaks":[],"active_break_started_at":null,'
            '"plan":{"target_net_minutes":%s,"clock_in":"09:00"}}}}'
        )
        rejected_documents = {
            "duplicate-top-level-key": (
                '{"state_version":3,"state_version":3,"days":{}}'
            ),
            "duplicate-nested-day-key": (
                '{"state_version":3,"days":{'
                '"2026-04-06":{"manual_breaks":[],"active_break_started_at":null},'
                '"2026-04-06":{"manual_breaks":[],"active_break_started_at":null}'
                '}}'
            ),
            "duplicate-nested-day-field": (
                '{"state_version":3,"days":{"2026-04-06":{'
                '"manual_breaks":[],"manual_breaks":[],'
                '"active_break_started_at":null}}}'
            ),
            "duplicate-nested-plan-key": (
                '{"state_version":3,"days":{"2026-04-06":{'
                '"manual_breaks":[],"active_break_started_at":null,'
                '"plan":{"target_net_minutes":480,"target_net_minutes":480,'
                '"clock_in":"09:00"}}}}'
            ),
            "non-finite-NaN": non_finite_template % "NaN",
            "non-finite-Infinity": non_finite_template % "Infinity",
            "non-finite-negative-Infinity": non_finite_template % "-Infinity",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, raw in rejected_documents.items():
                with self.subTest(name=name):
                    path = root / f"{name}.json"
                    original = (raw + "\n").encode("utf-8")
                    path.write_bytes(original)

                    store = WorktimeStateStore(path)
                    ok, error = store.update_day_plan("2026-04-06", 480, "09:00")

                    self.assertFalse(ok)
                    self.assertIn("덮어쓰기", error or "")
                    self.assertEqual(
                        store.snapshot(),
                        {"state_version": 3, "days": {}},
                    )
                    self.assertEqual(path.read_bytes(), original)

    def test_activity_prompt_decoder_enforces_status_specific_second_precision_schema(self) -> None:
        valid = {
            "state_version": 3,
            "days": {
                "2026-04-06": self._empty_day(
                    activity_prompt={
                        "status": "snoozed",
                        "detected_at": "2026-04-06T09:00:00",
                        "snooze_until": "2026-04-06T09:30:00",
                    }
                )
            },
        }
        invalid_prompts = {
            "unknown-status": {
                "status": "done",
                "detected_at": "2026-04-06T09:00:00",
            },
            "fractional-detected-at": {
                "status": "pending",
                "detected_at": "2026-04-06T09:00:00.1",
            },
            "timezone-detected-at": {
                "status": "pending",
                "detected_at": "2026-04-06T09:00:00+09:00",
            },
            "wrong-owning-day": {
                "status": "skipped",
                "detected_at": "2026-04-07T00:00:00",
            },
            "pending-with-until": {
                "status": "pending",
                "detected_at": "2026-04-06T09:00:00",
                "snooze_until": "2026-04-06T09:30:00",
            },
            "snoozed-without-until": {
                "status": "snoozed",
                "detected_at": "2026-04-06T09:00:00",
            },
            "snoozed-until-not-later": {
                "status": "snoozed",
                "detected_at": "2026-04-06T09:00:00",
                "snooze_until": "2026-04-06T09:00:00",
            },
            "unknown-field": {
                "status": "skipped",
                "detected_at": "2026-04-06T09:00:00",
                "reason": "later",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid_path = root / "valid.json"
            self._write_json(valid_path, valid)
            valid_store = WorktimeStateStore(valid_path)
            self.assertEqual(
                valid_store.get_activity_prompt("2026-04-06"),
                valid["days"]["2026-04-06"]["activity_prompt"],
            )

            for name, prompt in invalid_prompts.items():
                with self.subTest(name=name):
                    raw = {
                        "state_version": 3,
                        "days": {
                            "2026-04-06": self._empty_day(
                                activity_prompt=prompt
                            )
                        },
                    }
                    path = root / f"{name}.json"
                    self._write_json(path, raw)
                    store = WorktimeStateStore(path)
                    ok, error = store.clear_activity_prompt("2026-04-06")
                    self.assertFalse(ok)
                    self.assertIn("덮어쓰기", error or "")
                    self.assertEqual(self._read_json(path), raw)

    def test_clock_in_plan_clears_prompt_while_clockless_plan_preserves_it(self) -> None:
        now = datetime(2026, 4, 11, 9, 0, 0, 987654)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = WorktimeStateStore(path, now_provider=lambda: now)

            self.assertEqual(
                store.get_day_plan("2026-04-11", default_target_minutes=0),
                {
                    "date": "2026-04-11",
                    "target_net_minutes": 0,
                    "clock_in": None,
                    "explicit": False,
                },
            )
            self.assertEqual(store.record_activity_prompt_pending(), (True, None))
            self.assertEqual(
                store.get_activity_prompt("2026-04-11"),
                {
                    "status": "pending",
                    "detected_at": "2026-04-11T09:00:00",
                },
            )
            self.assertEqual(
                store.snooze_activity_prompt(
                    "2026-04-11",
                    datetime(2026, 4, 11, 9, 30),
                ),
                (True, None),
            )
            self.assertEqual(
                store.get_activity_prompt("2026-04-11"),
                {
                    "status": "snoozed",
                    "detected_at": "2026-04-11T09:00:00",
                    "snooze_until": "2026-04-11T09:30:00",
                },
            )
            self.assertEqual(store.skip_activity_prompt("2026-04-11"), (True, None))
            skipped_prompt = {
                "status": "skipped",
                "detected_at": "2026-04-11T09:00:00",
            }
            self.assertEqual(
                store.get_activity_prompt("2026-04-11"),
                skipped_prompt,
            )

            self.assertEqual(
                store.update_day_plan("2026-04-11", 0, None),
                (True, None),
            )
            self.assertTrue(store.get_day_plan("2026-04-11")["explicit"])
            self.assertEqual(
                store.get_activity_prompt("2026-04-11"),
                skipped_prompt,
            )
            self.assertEqual(store.clear_day_plan("2026-04-11"), (True, None))
            self.assertIsNotNone(store.get_activity_prompt("2026-04-11"))
            self.assertFalse(store.get_day_plan("2026-04-11")["explicit"])

            self.assertEqual(
                store.update_day_plan("2026-04-11", 0, "10:00"),
                (True, None),
            )
            self.assertTrue(store.get_day_plan("2026-04-11")["explicit"])
            self.assertIsNone(store.get_activity_prompt("2026-04-11"))
            self.assertEqual(store.clear_day_plan("2026-04-11"), (True, None))
            self.assertNotIn("2026-04-11", store.snapshot()["days"])

    def test_prompt_and_clock_in_mutations_roll_back_when_atomic_save_fails(self) -> None:
        now = datetime(2026, 4, 6, 9, 0)
        with tempfile.TemporaryDirectory() as tmp:
            store = WorktimeStateStore(
                Path(tmp) / "state.json",
                now_provider=lambda: now,
            )
            empty = store.snapshot()
            with mock.patch.object(store, "_save_locked", return_value=False):
                ok, error = store.record_activity_prompt_pending()
            self.assertFalse(ok)
            self.assertTrue(error)
            self.assertEqual(store.snapshot(), empty)

            self.assertEqual(store.record_activity_prompt_pending(), (True, None))
            pending = store.snapshot()

            failed_actions = (
                lambda: store.snooze_activity_prompt(
                    "2026-04-06",
                    datetime(2026, 4, 6, 9, 30),
                ),
                lambda: store.skip_activity_prompt("2026-04-06"),
                lambda: store.clear_activity_prompt("2026-04-06"),
                lambda: store.update_day_plan("2026-04-06", 480, "09:00"),
            )
            for action in failed_actions:
                with self.subTest(action=action):
                    with mock.patch.object(store, "_save_locked", return_value=False):
                        ok, error = action()
                    self.assertFalse(ok)
                    self.assertTrue(error)
                    self.assertEqual(store.snapshot(), pending)


if __name__ == "__main__":
    unittest.main()
