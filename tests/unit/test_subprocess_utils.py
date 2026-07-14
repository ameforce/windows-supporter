from __future__ import annotations

import types
import unittest

from src.utils.subprocess_utils import (
    build_no_window_subprocess_kwargs,
    build_python_module_command,
    is_frozen_runtime,
    popen_no_window,
    run_no_window,
)


class FakeStartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = None


class SubprocessUtilsUnitTest(unittest.TestCase):
    def test_no_window_kwargs_use_windows_flags_when_available(self) -> None:
        fake_subprocess = types.SimpleNamespace(
            CREATE_NO_WINDOW=0x08000000,
            STARTF_USESHOWWINDOW=1,
            SW_HIDE=0,
            STARTUPINFO=FakeStartupInfo,
        )

        kwargs = build_no_window_subprocess_kwargs(fake_subprocess)

        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertIsInstance(kwargs["startupinfo"], FakeStartupInfo)
        self.assertEqual(kwargs["startupinfo"].dwFlags, 1)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)

    def test_run_no_window_merges_flags_with_existing_kwargs(self) -> None:
        class FakeSubprocess:
            CREATE_NO_WINDOW = 0x08000000
            STARTF_USESHOWWINDOW = 1
            SW_HIDE = 0
            STARTUPINFO = FakeStartupInfo

            def __init__(self) -> None:
                self.argv = None
                self.kwargs = None

            def run(self, argv, **kwargs):
                self.argv = list(argv)
                self.kwargs = dict(kwargs)
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()

        result = run_no_window(
            ["git", "status"],
            subprocess_module=fake_subprocess,
            cwd=r"C:\repo",
            timeout=3,
        )

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(fake_subprocess.argv, ["git", "status"])
        self.assertEqual(fake_subprocess.kwargs["cwd"], r"C:\repo")
        self.assertEqual(fake_subprocess.kwargs["timeout"], 3)
        self.assertEqual(fake_subprocess.kwargs["creationflags"], 0x08000000)
        self.assertIsInstance(fake_subprocess.kwargs["startupinfo"], FakeStartupInfo)

    def test_popen_no_window_passes_explicit_cwd(self) -> None:
        calls = []

        class FakePopen:
            def __init__(self, argv, **kwargs) -> None:
                calls.append((list(argv), dict(kwargs)))

        import src.utils.subprocess_utils as subprocess_utils

        original_popen = subprocess_utils.subprocess.Popen
        try:
            subprocess_utils.subprocess.Popen = FakePopen
            proc = popen_no_window(["app.exe"], cwd=r"C:\repo")
        finally:
            subprocess_utils.subprocess.Popen = original_popen

        self.assertIsNotNone(proc)
        self.assertEqual(calls[0][0], ["app.exe"])
        self.assertEqual(calls[0][1]["cwd"], r"C:\repo")

    def test_popen_no_window_passes_explicit_environment(self) -> None:
        # Given
        calls = []

        class FakePopen:
            def __init__(self, argv, **kwargs) -> None:
                calls.append((list(argv), dict(kwargs)))

        import src.utils.subprocess_utils as subprocess_utils

        original_popen = subprocess_utils.subprocess.Popen
        try:
            subprocess_utils.subprocess.Popen = FakePopen

            # When
            proc = popen_no_window(
                ["app.exe"],
                cwd=r"C:\repo",
                env={"PYINSTALLER_RESET_ENVIRONMENT": "1"},
            )
        finally:
            subprocess_utils.subprocess.Popen = original_popen

        # Then
        self.assertIsNotNone(proc)
        self.assertEqual(
            calls[0][1]["env"],
            {"PYINSTALLER_RESET_ENVIRONMENT": "1"},
        )

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
