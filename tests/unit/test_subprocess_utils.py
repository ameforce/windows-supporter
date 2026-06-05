from __future__ import annotations

import types
import unittest

from src.utils.subprocess_utils import build_python_module_command, is_frozen_runtime


class SubprocessUtilsUnitTest(unittest.TestCase):
    def test_build_python_module_command_skips_frozen_runtime(self) -> None:
        logs: list[str] = []
        runtime = types.SimpleNamespace(
            frozen=True,
            executable=r"C:\app\windows-supporter.exe",
        )

        command = build_python_module_command(
            "playwright",
            ["install", "chromium"],
            sys_module=runtime,
            log=logs.append,
        )

        self.assertIsNone(command)
        self.assertTrue(is_frozen_runtime(runtime))
        self.assertIn("frozen runtime", logs[0])

    def test_build_python_module_command_uses_python_when_not_frozen(self) -> None:
        runtime = types.SimpleNamespace(
            frozen=False,
            executable=r"C:\Python\python.exe",
        )

        command = build_python_module_command(
            "playwright",
            ["install", "chromium"],
            sys_module=runtime,
        )

        self.assertEqual(
            command,
            [r"C:\Python\python.exe", "-m", "playwright", "install", "chromium"],
        )


if __name__ == "__main__":
    unittest.main()
