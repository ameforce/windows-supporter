from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "qa_ai_usage_native_visual.py"


def _load_harness_module():
    spec = importlib.util.spec_from_file_location("qa_ai_usage_native_visual", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AiUsageNativeVisualHarnessUnitTest(unittest.TestCase):
    def test_scenarios_cover_required_images_and_capture_phases(self) -> None:
        harness = _load_harness_module()

        self.assertEqual(
            tuple(harness.SCENARIO_NAMES),
            (
                "zero-profiles",
                "mixed-ready-standard",
                "one-profile-125",
                "dynamic-three-profiles",
                "ten-mixed-profiles-150",
                "long-label-narrow",
                "cursor-long-amount-150",
                "cursor-logged-out",
                "cursor-stale-rate-limited",
            ),
        )
        fixtures = [
            harness.build_scenario_fixture(
                name,
                now=datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc),
            )
            for name in harness.SCENARIO_NAMES
        ]
        self.assertEqual({fixture["phase"] for fixture in fixtures}, {"initial", "interaction", "final"})
        self.assertEqual(len({fixture["screenshot_name"] for fixture in fixtures}), 9)
        by_name = {fixture["name"]: fixture for fixture in fixtures}
        self.assertEqual(by_name["zero-profiles"]["settings"]["profiles"], [])
        self.assertEqual(by_name["zero-profiles"]["runtime"]["profiles"], [])
        self.assertEqual(len(by_name["one-profile-125"]["settings"]["profiles"]), 1)
        self.assertEqual(by_name["one-profile-125"]["ui_scale_percent"], 125)
        dynamic = by_name["dynamic-three-profiles"]
        self.assertEqual(len(dynamic["settings"]["profiles"]), 3)
        self.assertFalse(dynamic["settings"]["profiles"][2]["taskbar_selected"])
        ten_profiles = by_name["ten-mixed-profiles-150"]
        self.assertEqual(len(ten_profiles["settings"]["profiles"]), 10)
        self.assertEqual(ten_profiles["ui_scale_percent"], 150)
        self.assertEqual(len(ten_profiles["settings"]["selected_profile_ids"]), 2)

    def test_fixture_is_provider_neutral_and_contains_no_machine_identity(self) -> None:
        harness = _load_harness_module()

        fixtures = [
            harness.build_scenario_fixture(
                name,
                now=datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc),
            )
            for name in harness.SCENARIO_NAMES
        ]
        serialized = json.dumps(fixtures, ensure_ascii=False).lower()

        self.assertIn('"provider": "codex"', serialized)
        self.assertIn('"provider": "cursor"', serialized)
        for forbidden in ("epapyrus", "appdata", "\\users\\", "@example.com"):
            self.assertNotIn(forbidden, serialized)
        by_name = {fixture["name"]: fixture for fixture in fixtures}
        long_label = by_name["long-label-narrow"]
        self.assertEqual(long_label["window_size"][0], 700)
        self.assertLess(long_label["window_size"][0], by_name["mixed-ready-standard"]["window_size"][0])
        self.assertGreater(len(long_label["settings"]["profiles"][0]["label"]), 40)
        long_amount = by_name["cursor-long-amount-150"]
        self.assertEqual(long_amount["ui_scale_percent"], 150)
        self.assertEqual(long_amount["window_size"][0], 960)
        self.assertEqual(
            long_amount["runtime"]["profiles"][1]["last_snapshot"]["on_demand_status"],
            "Enabled · US$1,234,567,890.12 used",
        )

        logged_out = by_name["cursor-logged-out"]["runtime"]["profiles"][1]["runtime"]
        self.assertEqual(logged_out["session_state"], "logged_out")
        stale = by_name["cursor-stale-rate-limited"]["runtime"]["profiles"][1]
        self.assertEqual(stale["runtime"]["provider_state"], "rate_limited")
        self.assertEqual(stale["runtime"]["failure_count"], 1)

    def test_output_directory_must_be_outside_repository(self) -> None:
        harness = _load_harness_module()

        with self.assertRaises(ValueError):
            harness.validate_output_dir(REPO_ROOT / "visual-evidence")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = harness.validate_output_dir(Path(tmp) / "ai-usage-native")
            self.assertFalse(output_dir.is_relative_to(REPO_ROOT.resolve()))

    def test_capture_provenance_records_exact_head_and_clean_tree(self) -> None:
        harness = _load_harness_module()

        with mock.patch.object(
            harness.subprocess,
            "check_output",
            side_effect=["a" * 40 + "\n", ""],
        ):
            provenance = harness.capture_provenance()

        self.assertEqual(provenance["git_sha"], "a" * 40)
        self.assertTrue(provenance["worktree_clean"])

    def test_capture_report_keeps_provenance_at_metadata_root(self) -> None:
        harness = _load_harness_module()

        def fake_capture(fixture, output_dir, *, settle_ms):
            screenshot = output_dir / fixture["screenshot_name"]
            screenshot.write_bytes(b"fixture")
            return {
                "ok": True,
                "scenario": fixture["name"],
                "phase": fixture["phase"],
                "screenshot": screenshot.name,
            }

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "ai-usage-native"
            with (
                mock.patch.object(
                    harness,
                    "capture_provenance",
                    return_value={"git_sha": "b" * 40, "worktree_clean": True},
                ),
                mock.patch.object(harness, "_enable_per_monitor_dpi_awareness", return_value="test"),
                mock.patch.object(harness, "capture_scenario", side_effect=fake_capture),
            ):
                report = harness.run_capture_matrix(output_dir)

            persisted = json.loads(
                (output_dir / harness.METADATA_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(report["git_sha"], "b" * 40)
        self.assertTrue(report["git_worktree_clean"])
        self.assertEqual(persisted["git_sha"], "b" * 40)
        self.assertTrue(persisted["git_worktree_clean"])

    def test_capture_report_fails_closed_for_dirty_worktree(self) -> None:
        harness = _load_harness_module()

        def fake_capture(fixture, output_dir, *, settle_ms):
            screenshot = output_dir / fixture["screenshot_name"]
            screenshot.write_bytes(b"fixture")
            return {
                "ok": True,
                "scenario": fixture["name"],
                "phase": fixture["phase"],
                "screenshot": screenshot.name,
            }

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "ai-usage-native"
            with (
                mock.patch.object(
                    harness,
                    "capture_provenance",
                    return_value={"git_sha": "c" * 40, "worktree_clean": False},
                ),
                mock.patch.object(harness, "_enable_per_monitor_dpi_awareness", return_value="test"),
                mock.patch.object(harness, "capture_scenario", side_effect=fake_capture),
            ):
                report = harness.run_capture_matrix(output_dir)

        self.assertFalse(report["ok"])
        self.assertFalse(report["git_worktree_clean"])

    def test_capture_report_fails_closed_when_provenance_changes_mid_capture(self) -> None:
        harness = _load_harness_module()

        def fake_capture(fixture, output_dir, *, settle_ms):
            screenshot = output_dir / fixture["screenshot_name"]
            screenshot.write_bytes(b"fixture")
            return {
                "ok": True,
                "scenario": fixture["name"],
                "phase": fixture["phase"],
                "screenshot": screenshot.name,
            }

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "ai-usage-native"
            with (
                mock.patch.object(
                    harness,
                    "capture_provenance",
                    side_effect=[
                        {"git_sha": "d" * 40, "worktree_clean": True},
                        {"git_sha": "e" * 40, "worktree_clean": True},
                    ],
                ),
                mock.patch.object(harness, "_enable_per_monitor_dpi_awareness", return_value="test"),
                mock.patch.object(harness, "capture_scenario", side_effect=fake_capture),
            ):
                report = harness.run_capture_matrix(output_dir)

        self.assertFalse(report["ok"])
        self.assertFalse(report["git_provenance_stable"])
        self.assertEqual(report["git_end_sha"], "e" * 40)


if __name__ == "__main__":
    unittest.main()
