import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import qa_codex_usage_taskbar_overlay_smoke as smoke


class CodexUsageTaskbarOverlaySmokeScriptTest(unittest.TestCase):
    def test_smoke_script_writes_snapshot_with_rca_summary(self):
        snapshot = {
            "coordinate_basis": "physical_px",
            "fallback_reason": "",
            "rca_class": "displayable_horizontal_taskbar",
            "rca_class_summary": {"displayable_horizontal_taskbar": 1},
            "target_decisions": [{"taskbar_hwnd": 11}],
            "fullscreen_decisions": [{"taskbar_hwnd": 11, "fullscreen": False}],
        }

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            smoke,
            "capture_local_taskbar_overlay_geometry_snapshot",
            return_value=snapshot,
        ):
            output_path = Path(tmp) / "taskbar-overlay.json"
            exit_code = smoke.main(
                [
                    "--output",
                    str(output_path),
                    "--samples",
                    "2",
                    "--interval",
                    "0",
                ]
            )

            self.assertEqual(exit_code, 0)
            data = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(data["rca_class"], "displayable_horizontal_taskbar")
        self.assertEqual(data["rca_class_summary"]["displayable_horizontal_taskbar"], 1)
        self.assertEqual(data["target_decisions"][0]["taskbar_hwnd"], 11)


if __name__ == "__main__":
    unittest.main()
