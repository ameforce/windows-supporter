from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


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
                "mixed-ready-standard",
                "long-label-narrow",
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
        self.assertEqual(len({fixture["screenshot_name"] for fixture in fixtures}), 4)

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
        self.assertLess(fixtures[1]["window_size"][0], fixtures[0]["window_size"][0])
        self.assertGreater(len(fixtures[1]["settings"]["profiles"][0]["label"]), 40)

        logged_out = fixtures[2]["runtime"]["profiles"][1]["runtime"]
        self.assertEqual(logged_out["session_state"], "logged_out")
        stale = fixtures[3]["runtime"]["profiles"][1]
        self.assertEqual(stale["runtime"]["provider_state"], "rate_limited")
        self.assertEqual(stale["runtime"]["failure_count"], 1)

    def test_output_directory_must_be_outside_repository(self) -> None:
        harness = _load_harness_module()

        with self.assertRaises(ValueError):
            harness.validate_output_dir(REPO_ROOT / "visual-evidence")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = harness.validate_output_dir(Path(tmp) / "ai-usage-native")
            self.assertFalse(output_dir.is_relative_to(REPO_ROOT.resolve()))


if __name__ == "__main__":
    unittest.main()
