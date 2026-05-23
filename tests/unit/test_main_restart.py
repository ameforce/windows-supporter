import os
import unittest
from unittest.mock import patch

import main


class MainRestartUnitTest(unittest.TestCase):
    def test_build_restart_command_reuses_frozen_executable_and_args(self) -> None:
        command = main._build_restart_command(
            executable=r"C:\app\windows-supporter.exe",
            argv=[r"C:\ignored\main.py", "--flag", "value"],
            frozen=True,
            main_file=r"C:\src\main.py",
        )

        self.assertEqual(
            command,
            [r"C:\app\windows-supporter.exe", "--flag", "value"],
        )

    def test_build_restart_command_uses_python_script_when_not_frozen(self) -> None:
        command = main._build_restart_command(
            executable=r"C:\Python\python.exe",
            argv=["main.py", "--flag"],
            frozen=False,
            main_file=r"C:\src\main.py",
        )

        self.assertEqual(
            command,
            [r"C:\Python\python.exe", os.path.abspath("main.py"), "--flag"],
        )

    def test_build_restart_command_falls_back_to_main_file_for_empty_argv(self) -> None:
        command = main._build_restart_command(
            executable=r"C:\Python\python.exe",
            argv=[],
            frozen=False,
            main_file=r"C:\src\main.py",
        )

        self.assertEqual(
            command,
            [r"C:\Python\python.exe", r"C:\src\main.py"],
        )

    def test_build_restart_environment_marks_frozen_restart_as_new_instance(self) -> None:
        env = main._build_restart_environment(
            environ={"_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI12345"},
            frozen=True,
        )

        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["_PYI_APPLICATION_HOME_DIR"], r"C:\Temp\_MEI12345")

    def test_build_restart_environment_does_not_mark_unfrozen_restart(self) -> None:
        env = main._build_restart_environment(environ={"PATH": "x"}, frozen=False)

        self.assertEqual(env, {"PATH": "x"})

    def test_build_restart_cwd_uses_executable_dir_when_frozen(self) -> None:
        cwd = main._build_restart_cwd(
            executable=r"C:\apps\windows-supporter\windows-supporter.exe",
            current_cwd=r"C:\Users\enmso\AppData\Local\Temp\_MEI323362",
            frozen=True,
        )

        self.assertEqual(cwd, r"C:\apps\windows-supporter")

    def test_build_restart_cwd_preserves_current_cwd_when_unfrozen(self) -> None:
        cwd = main._build_restart_cwd(
            executable=r"C:\Python\python.exe",
            current_cwd=r"C:\workspace\epapyrus\git\tools\windows-supporter",
            frozen=False,
        )

        self.assertEqual(cwd, r"C:\workspace\epapyrus\git\tools\windows-supporter")

    def test_restart_current_process_passes_reset_environment_and_safe_cwd(self) -> None:
        with patch.object(main, "_build_restart_command", return_value=["app.exe"]):
            with patch.object(
                main,
                "_build_restart_environment",
                return_value={"PYINSTALLER_RESET_ENVIRONMENT": "1"},
            ):
                with patch.object(
                    main,
                    "_build_restart_cwd",
                    return_value=r"C:\apps\windows-supporter",
                ):
                    with patch.object(main.os, "name", "nt"):
                        with patch.object(main.subprocess, "STARTUPINFO") as startupinfo:
                            startupinfo.return_value.dwFlags = 0
                            with patch.object(main.subprocess, "Popen") as popen:
                                main._restart_current_process()

        popen.assert_called_once()
        _, kwargs = popen.call_args
        self.assertEqual(kwargs["env"], {"PYINSTALLER_RESET_ENVIRONMENT": "1"})
        self.assertEqual(kwargs["cwd"], r"C:\apps\windows-supporter")


if __name__ == "__main__":
    unittest.main()
