from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import src.utils.update_monitor as update_monitor_module
from src.utils.update_monitor import (
    DEFAULT_CLEAN_ALLOWLIST,
    GIT_COMMAND_TIMEOUT_SECONDS,
    UPDATE_HANDOFF_ACK_TIMEOUT_SECONDS,
    UPDATE_HANDOFF_ARG,
    UPDATE_CLEANUP_ONLY_NOTICE,
    UPDATE_FORCE_CLEAN_APPROVAL_TEXT,
    UPDATE_FORCE_CLEAN_REJECTED_NOTICE,
    UPDATE_PROGRESS_LOG_BUTTON_TEXT,
    UPDATE_PROGRESS_MANUAL_ACTION_TEXT,
    UPDATE_PROGRESS_RETRY_BUTTON_TEXT,
    UPDATE_SOURCE_CHANGE_NOTICE,
    UpdateCandidate,
    UpdateHandoffProgressUi,
    UpdatePromptSession,
    UpdateWorkingTreeState,
    WindowsSupporterUpdater,
    build_allowed_clean_command,
    build_allowed_clean_probe_command,
    build_divergence_count_command,
    build_divergence_log_command,
    build_backup_branch_command,
    build_backup_branch_name,
    build_fast_forward_main_command,
    build_fetch_origin_command,
    build_force_clean_approval_message,
    build_update_build_output_progress_snapshot,
    build_remote_tag_check_command,
    build_reset_main_command,
    build_short_head_command,
    build_stash_command,
    build_switch_main_command,
    build_update_handoff_command,
    build_update_handoff_payload,
    build_update_progress_snapshot,
    close_running_git_gui_processes,
    cleanup_update_handoff_executable,
    get_update_handoff_executable_path,
    get_update_progress_step,
    is_git_checkout_root,
    parse_clean_probe_output,
    parse_divergence_counts,
    parse_git_status_porcelain,
    parse_left_right_log,
    parse_remote_tag_refs,
    parse_semver_tag,
    read_update_handoff_state,
    resolve_current_tag,
    run_no_window_with_progress,
    run_update_handoff,
    select_update_candidate,
    start_update_handoff_cleanup_thread,
    wait_for_update_handoff_ack,
)


def _primary_worktree_runner(repo_root: str | os.PathLike[str] = "."):
    resolved = os.path.abspath(os.fspath(repo_root)).replace(os.sep, "/")
    output = "\n".join(
        [
            f"worktree {resolved}",
            "HEAD 9cc7cc8",
            "branch refs/heads/main",
            "",
        ]
    )
    return lambda _argv, **_kwargs: types.SimpleNamespace(
        returncode=0,
        stdout=output,
        stderr="",
    )


class StablePopen:
    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd=["windows-supporter.exe"], timeout=timeout)


class UpdateMonitorCoreUnitTest(unittest.TestCase):
    def test_semantic_tags_sort_numerically_and_ignore_non_semver(self) -> None:
        self.assertEqual(parse_semver_tag("v0.5.10"), (0, 5, 10))
        self.assertGreater(parse_semver_tag("v0.5.10"), parse_semver_tag("v0.5.6"))
        self.assertIsNone(parse_semver_tag("not-a-release"))
        self.assertIsNone(parse_semver_tag("v1.2"))

    def test_remote_tag_refs_are_parsed_without_dereferenced_tag_lines(self) -> None:
        output = "\n".join(
            [
                "aaa111\trefs/tags/v0.5.6",
                "bbb222\trefs/tags/v0.5.7",
                "ccc333\trefs/tags/v0.5.7^{}",
                "ddd444\trefs/heads/main",
                "eee555\trefs/tags/test-build",
            ]
        )

        self.assertEqual(parse_remote_tag_refs(output), ["v0.5.6", "v0.5.7"])

    def test_update_candidate_uses_highest_newer_semver_tag(self) -> None:
        candidate = select_update_candidate(
            current_tag="v0.5.6",
            remote_tags=["v0.5.6", "v0.5.10", "v0.5.7", "nightly"],
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.tag, "v0.5.10")

    def test_update_candidate_is_absent_without_current_tag(self) -> None:
        self.assertIsNone(
            select_update_candidate(current_tag="", remote_tags=["v0.5.7"])
        )

    def test_current_tag_prefers_build_info_and_falls_back_to_git_describe(self) -> None:
        app_version = types.SimpleNamespace(source_tag="v0.5.6")

        self.assertEqual(
            resolve_current_tag(app_version=app_version, git_describe="v0.5.7-2-gabc123"),
            "v0.5.6",
        )
        self.assertEqual(
            resolve_current_tag(
                app_version=types.SimpleNamespace(source_tag=""),
                git_describe="v0.5.7-2-gabc123",
            ),
            "v0.5.7",
        )
        self.assertEqual(
            resolve_current_tag(
                app_version=types.SimpleNamespace(source_tag=""),
                git_describe="not-tag-based",
            ),
            "",
        )

    def test_prompt_session_suppresses_same_dismissed_tag_only(self) -> None:
        session = UpdatePromptSession()

        self.assertTrue(session.should_prompt("v0.5.7"))
        session.dismiss("v0.5.7")
        self.assertFalse(session.should_prompt("v0.5.7"))
        self.assertTrue(session.should_prompt("v0.5.8"))

    def test_remote_check_command_is_non_mutating(self) -> None:
        command = build_remote_tag_check_command()

        self.assertEqual(command, ["git", "ls-remote", "--tags", "--refs", "origin"])
        self.assertNotIn("fetch", command)
        self.assertNotIn("pull", command)
        self.assertNotIn("clean", command)

    def test_dirty_checkout_policy_stashes_untracked_and_cleans_only_allowlist(self) -> None:
        stash = build_stash_command("windows-supporter auto update")
        clean = build_allowed_clean_command()
        clean_probe = build_allowed_clean_probe_command()

        self.assertEqual(
            stash,
            [
                "git",
                "stash",
                "push",
                "--include-untracked",
                "-m",
                "windows-supporter auto update",
            ],
        )
        self.assertEqual(clean, ["git", "clean", "-fdX", "--", *DEFAULT_CLEAN_ALLOWLIST])
        self.assertEqual(clean_probe, ["git", "clean", "-ndX", "--", *DEFAULT_CLEAN_ALLOWLIST])
        self.assertEqual(DEFAULT_CLEAN_ALLOWLIST, ("build/", "dist/", "*.spec", "*.egg-info/"))
        self.assertNotIn(".venv/", DEFAULT_CLEAN_ALLOWLIST)
        self.assertNotIn(".omx/", DEFAULT_CLEAN_ALLOWLIST)
        self.assertNotIn("windows-supporter.exe", DEFAULT_CLEAN_ALLOWLIST)

    def test_git_state_parsers_separate_source_cleanup_and_divergence(self) -> None:
        self.assertEqual(
            parse_git_status_porcelain(" M src/main.py\n?? new-file.txt\n\n"),
            ("M src/main.py", "?? new-file.txt"),
        )
        self.assertEqual(
            parse_clean_probe_output("Would remove build/generated.tmp\nWould remove dist/app\n"),
            ("build/generated.tmp", "dist/app"),
        )
        self.assertEqual(parse_divergence_counts("3\t2"), (2, 3))
        self.assertEqual(parse_divergence_counts("not counts"), (0, 0))
        self.assertEqual(
            parse_left_right_log("<abc123 remote commit\n>def456 local commit\nplain line"),
            (("def456 local commit",), ("abc123 remote commit",)),
        )

    def test_divergence_commands_use_remote_left_and_local_right(self) -> None:
        self.assertEqual(
            build_divergence_count_command(local_ref="main", remote_ref="origin/main"),
            ["git", "rev-list", "--left-right", "--count", "origin/main...main"],
        )
        self.assertEqual(
            build_divergence_log_command(local_ref="main", remote_ref="origin/main"),
            [
                "git",
                "log",
                "--oneline",
                "--left-right",
                "--cherry-pick",
                "origin/main...main",
            ],
        )

    def test_force_clean_command_builders_are_safe_and_explicit(self) -> None:
        backup_name = build_backup_branch_name("20260614-120000", "abc123")

        self.assertEqual(build_switch_main_command(), ["git", "switch", "main"])
        self.assertEqual(build_reset_main_command(), ["git", "reset", "--hard", "origin/main"])
        self.assertEqual(
            build_fast_forward_main_command(),
            ["git", "merge", "--ff-only", "origin/main"],
        )
        self.assertEqual(build_short_head_command(), ["git", "rev-parse", "--short", "main"])
        self.assertEqual(
            backup_name,
            "backup/windows-supporter-auto-update/20260614-120000-abc123",
        )
        self.assertEqual(
            build_backup_branch_command(backup_name),
            ["git", "branch", backup_name, "main"],
        )

    def test_fetch_command_forces_authoritative_remote_tags(self) -> None:
        # Given / When
        command = build_fetch_origin_command()

        # Then
        self.assertEqual(command, ["git", "fetch", "--force", "--tags", "origin"])

    def test_working_tree_state_flags_cleanup_without_source_stash(self) -> None:
        cleanup_only = UpdateWorkingTreeState(cleanup_targets=("build/generated.tmp",))
        diverged = UpdateWorkingTreeState(local_only_count=1, remote_only_count=2)
        local_only = UpdateWorkingTreeState(local_only_count=1)

        self.assertFalse(cleanup_only.has_source_changes)
        self.assertTrue(cleanup_only.has_cleanup_targets)
        self.assertFalse(cleanup_only.needs_source_stash)
        self.assertTrue(cleanup_only.needs_pre_update_notice)
        self.assertTrue(diverged.is_diverged)
        self.assertTrue(diverged.needs_pre_update_notice)
        self.assertFalse(local_only.is_diverged)

    def test_handoff_command_uses_python_script_when_not_frozen(self) -> None:
        command = build_update_handoff_command(
            r"C:\state\update_handoff.json",
            executable=r"C:\Python\python.exe",
            argv=["main.py"],
            frozen=False,
            main_file=r"C:\repo\main.py",
        )

        self.assertEqual(
            command,
            [
                r"C:\Python\python.exe",
                os.path.abspath("main.py"),
                UPDATE_HANDOFF_ARG,
                r"C:\state\update_handoff.json",
            ],
        )

    def test_handoff_command_copies_frozen_executable_to_updater_name(self) -> None:
        copies = []

        def copy_function(source, target):
            copies.append((str(source), str(target)))
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text("copy", encoding="utf-8")
            return target

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "windows-supporter.exe"
            source.write_text("source", encoding="utf-8")
            target = Path(tmp) / "windows-supporter-updater.exe"

            command = build_update_handoff_command(
                Path(tmp) / "update_handoff.json",
                executable=source,
                frozen=True,
                handoff_executable_path=target,
                copy_function=copy_function,
            )

        self.assertEqual(copies, [(str(source), str(target))])
        self.assertEqual(command[0], str(target))
        self.assertIn(UPDATE_HANDOFF_ARG, command)
        self.assertNotEqual(command[0], str(source))

    def test_cleanup_update_handoff_executable_removes_helper_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "windows-supporter-updater.exe"
            helper.write_text("helper", encoding="utf-8")
            current = Path(tmp) / "windows-supporter.exe"
            current.write_text("current", encoding="utf-8")

            cleaned = cleanup_update_handoff_executable(
                executable_path=helper,
                current_executable=current,
            )

            self.assertTrue(cleaned)
            self.assertFalse(helper.exists())
            self.assertTrue(current.exists())

    def test_cleanup_update_handoff_executable_does_not_delete_current_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "windows-supporter-updater.exe"
            helper.write_text("helper", encoding="utf-8")

            cleaned = cleanup_update_handoff_executable(
                executable_path=helper,
                current_executable=helper,
            )

            self.assertFalse(cleaned)
            self.assertTrue(helper.exists())

    def test_start_update_handoff_cleanup_thread_retries_without_cmd_shell(self) -> None:
        class InlineThread:
            def __init__(self, target=None, daemon=False) -> None:
                self.target = target
                self.daemon = bool(daemon)
                self.started = False

            def start(self):
                self.started = True
                self.target()

        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "windows-supporter-updater.exe"
            helper.write_text("helper", encoding="utf-8")
            current = Path(tmp) / "windows-supporter.exe"
            current.write_text("current", encoding="utf-8")

            thread = start_update_handoff_cleanup_thread(
                executable_path=helper,
                current_executable=current,
                attempts=1,
                delay_seconds=0,
                thread_factory=InlineThread,
            )

            self.assertTrue(thread.started)
            self.assertTrue(thread.daemon)
            self.assertFalse(helper.exists())

    def test_handoff_ack_timeout_allows_slow_windows_startup(self) -> None:
        self.assertGreaterEqual(UPDATE_HANDOFF_ACK_TIMEOUT_SECONDS, 10.0)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "update_handoff.json"
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump({"status": "running"}, fp)

            self.assertTrue(wait_for_update_handoff_ack(state_path))

    def test_build_bat_supports_updater_owned_relaunch(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        self.assertIn("WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN", script)
        self.assertIn("SKIP_POST_BUILD_RUN", script)
        self.assertIn("Skipping post-build launch", script)

    def test_build_bat_artifact_only_mode_avoids_global_process_and_root_exe_mutation(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        self.assertIn("WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY", script)
        taskkill_guard = script.index('if "%ARTIFACT_ONLY%"=="0" (')
        taskkill_index = script.index('taskkill /f /t /im "%EXE_NAME%"')
        taskkill_guard_end = script.index("\n)", taskkill_index)
        staged_validation_index = script.index("tools\\verify_codex_usage_worker_smoke.py")
        artifact_exit = script.index(
            'if "%ARTIFACT_ONLY%"=="1" (', staged_validation_index
        )
        move_index = script.index('move /Y "dist\\%EXE_NAME%" "%ROOT_EXE%"')

        self.assertLess(taskkill_guard, taskkill_index)
        self.assertLess(taskkill_index, taskkill_guard_end)
        self.assertLess(artifact_exit, move_index)
        self.assertLess(staged_validation_index, artifact_exit)
        self.assertIn("dist\\%EXE_NAME%", script[artifact_exit:move_index])

    def test_build_bat_can_emit_step_log_for_updater_progress(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        self.assertIn("WINDOWS_SUPPORTER_EMIT_STEP_LOG", script)
        self.assertIn("WINDOWS_SUPPORTER_STEP_LOG=%STEP_LOG%", script)
        self.assertIn('if "%EMIT_STEP_LOG%"=="1"', script)

    def test_build_bat_terminates_running_process_tree(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read().lower()

        self.assertIn('taskkill /f /t /im "%exe_name%"', script)

    def test_build_bat_treats_taskkill_128_as_not_running(self) -> None:
        if os.name != "nt":
            self.skipTest("build.bat is a Windows command path")

        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            (temp_dir / "taskkill.cmd").write_text(
                "@echo off\r\nexit /b 128\r\n",
                encoding="utf-8",
            )
            (temp_dir / "powershell.cmd").write_text(
                "@echo off\r\nexit /b 1\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PATH"] = f"{temp_dir}{os.pathsep}{env['PATH']}"
            env["TEMP"] = str(temp_dir)
            env["TMP"] = str(temp_dir)

            result = subprocess.run(
                ["cmd", "/d", "/c", "build.bat"],
                cwd=Path.cwd(),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

        self.assertIn("[ Not running ]", result.stdout)
        self.assertNotIn("Failed to stop the running windows-supporter.exe process.", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_build_bat_wait_loops_do_not_depend_on_timeout_stdin(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        self.assertNotIn("timeout /t", script.lower())
        self.assertIn("call :sleep_one_second", script)
        self.assertIn(":sleep_one_second", script)
        self.assertIn("Start-Sleep -Seconds 1", script)

    def test_build_bat_validates_pyinstaller_archive_before_launch(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        validation_index = script.index("tools\\verify_pyinstaller_archive.py")
        move_index = script.index("Moving %EXE_NAME%")
        cleanup_index = script.index("Remove build byproducts")
        launch_index = script.index("Running %EXE_NAME%")

        self.assertLess(validation_index, move_index)
        self.assertLess(validation_index, cleanup_index)
        self.assertLess(validation_index, launch_index)
        self.assertIn("playwright\\driver\\node.exe", script)

    def test_build_bat_rolls_back_when_promoted_worker_boundary_is_blocked(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        staged_validation_index = script.index(
            '"tools\\verify_codex_usage_worker_smoke.py" "dist\\%EXE_NAME%"'
        )
        backup_index = script.index("call :backup_root_executable")
        move_index = script.index('move /Y "dist\\%EXE_NAME%" "%ROOT_EXE%"')
        promoted_validation_index = script.index(
            '"tools\\verify_codex_usage_worker_smoke.py" "%ROOT_EXE%"'
        )
        rollback_index = script.index(
            "call :restore_root_executable", promoted_validation_index
        )
        cleanup_index = script.index("Remove build byproducts")

        self.assertLess(staged_validation_index, backup_index)
        self.assertLess(backup_index, move_index)
        self.assertLess(move_index, promoted_validation_index)
        self.assertLess(promoted_validation_index, rollback_index)
        self.assertLess(rollback_index, cleanup_index)
        self.assertIn(":backup_root_executable", script)
        self.assertIn(":restore_root_executable", script)
        backup_helper_index = script.index("\n:backup_root_executable")
        restore_helper_index = script.index("\n:restore_root_executable")
        backup_helper = script[backup_helper_index:restore_helper_index]
        self.assertIn('mklink /H "%ROOT_EXE_BACKUP%" "%ROOT_EXE%"', backup_helper)
        self.assertIn('move /Y "%ROOT_EXE%" "%ROOT_EXE_BACKUP%"', backup_helper)
        self.assertNotIn('copy /Y "%ROOT_EXE%" "%ROOT_EXE_BACKUP%"', backup_helper)

    def test_build_bat_rollback_helpers_restore_the_previous_file(self) -> None:
        if os.name != "nt":
            self.skipTest("build.bat rollback helpers are a Windows command path")

        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()
        helper_start = script.index("\n:backup_root_executable") + 1
        helper_end = script.index("\n:remove_pyinstaller_byproducts", helper_start) + 1
        rollback_helpers = script[helper_start:helper_end]

        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            root_exe = temp_dir / "windows-supporter-rollback-test.exe"
            expected_exe = temp_dir / "expected.exe"
            candidate_exe = temp_dir / "candidate.exe"
            backup_exe = temp_dir / "windows-supporter-rollback-test.previous.exe"
            step_log = temp_dir / "rollback.log"
            transaction = temp_dir / "windows-supporter-rollback-test.promotion-pending"
            expected_exe.write_bytes(b"known-good")
            root_exe.write_bytes(b"known-good")
            original_file_index = root_exe.stat().st_ino
            candidate_exe.write_bytes(b"blocked-candidate")
            harness = temp_dir / "rollback-helper-test.bat"
            harness.write_text(
                "\r\n".join(
                    [
                        "@echo off",
                        "setlocal EnableExtensions DisableDelayedExpansion",
                        f'set "ROOT_EXE={root_exe}"',
                        f'set "ROOT_EXE_BACKUP={backup_exe}"',
                        'set "ROOT_BACKUP_CREATED=0"',
                        'set "EXE_NAME=windows-supporter-rollback-test.exe"',
                        f'set "STEP_LOG={step_log}"',
                        f'set "ROOT_PROMOTION_TRANSACTION={transaction}"',
                        "call :backup_root_executable",
                        "if errorlevel 1 exit /b 10",
                        f'move /Y "{candidate_exe}" "%ROOT_EXE%" > NUL',
                        "if errorlevel 1 exit /b 11",
                        "call :recover_interrupted_promotion",
                        "if errorlevel 1 exit /b 12",
                        f'fc /B "{expected_exe}" "%ROOT_EXE%" > NUL',
                        "if errorlevel 1 exit /b 13",
                        'if exist "%ROOT_EXE_BACKUP%" exit /b 14',
                        'if exist "%ROOT_PROMOTION_TRANSACTION%" exit /b 15',
                        "exit /b 0",
                        rollback_helpers,
                        ":clear_log",
                        'if exist "%STEP_LOG%" del /F /Q "%STEP_LOG%" > NUL 2>&1',
                        "exit /b 0",
                        ":wait_for_process_stop",
                        "exit /b 0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["cmd", "/d", "/c", str(harness)],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            restored_file_index = root_exe.stat().st_ino

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertEqual(restored_file_index, original_file_index)

    def test_build_bat_sleep_helper_runs_with_redirected_stdin(self) -> None:
        if os.name != "nt":
            self.skipTest("build.bat sleep helper is a Windows command path")

        with open("build.bat", "r", encoding="utf-8") as fp:
            lines = fp.read().splitlines()

        helper_index = next(
            index
            for index, line in enumerate(lines)
            if line.strip().lower() == ":sleep_one_second"
        )
        sleep_command = lines[helper_index + 1].strip()
        self.assertIn("Start-Sleep -Seconds 1", sleep_command)

        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "sleep-helper.bat"
            harness.write_text(
                f"@echo off\r\n{sleep_command}\r\nexit /b %ERRORLEVEL%\r\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["cmd", "/c", str(harness)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )

        output = f"{result.stdout}\n{result.stderr}"
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Input redirection is not supported", output)

    def test_update_progress_snapshot_exposes_korean_copy_and_progressbar(self) -> None:
        build_step = get_update_progress_step("build")
        snapshot = build_update_progress_snapshot("build", state="running")
        failed = build_update_progress_snapshot(
            "failed",
            state="failed",
            detail="build.bat 실패",
            failed_step="build.bat 실행",
            can_retry=True,
            can_manual_action=True,
        )

        self.assertEqual(build_step.label, "빌드 실행 중")
        self.assertEqual(snapshot["title"], "Windows Supporter 업데이트")
        self.assertEqual(snapshot["label"], "빌드 실행 중")
        self.assertEqual(snapshot["percent"], 74)
        self.assertTrue(snapshot["progressbar"]["visible"])
        self.assertEqual(snapshot["progressbar"]["mode"], "determinate")
        self.assertEqual(failed["title"], "Windows Supporter 업데이트 실패")
        self.assertEqual(failed["failed_step"], "build.bat 실행")
        self.assertTrue(failed["can_retry"])
        self.assertTrue(failed["can_manual_action"])
        self.assertEqual(failed["labels"]["log"], UPDATE_PROGRESS_LOG_BUTTON_TEXT)
        self.assertEqual(failed["labels"]["retry"], UPDATE_PROGRESS_RETRY_BUTTON_TEXT)
        self.assertEqual(failed["labels"]["manual_action"], UPDATE_PROGRESS_MANUAL_ACTION_TEXT)

    def test_update_handoff_progress_ui_uses_borderless_shell_and_collapses_empty_activity(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        try:
            progress.show(
                build_update_progress_snapshot(
                    "handoff_start",
                    state="running",
                    detail="업데이트 전용 프로세스가 시작되었습니다.",
                )
            )

            self.assertIsNotNone(progress._root)
            assert progress._root is not None
            self.assertTrue(progress._root.overrideredirect())
            self.assertEqual(str(progress._root.cget("bg")).upper(), "#FFFFFF")
            self.assertIsNotNone(progress._drag_region)
            self.assertTrue(progress._drag_region.bind("<ButtonPress-1>"))
            self.assertTrue(progress._drag_region.bind("<B1-Motion>"))
            self.assertTrue(progress._root.bind("<Alt-F4>"))
            self.assertIsNotNone(progress._activity_shell)
            self.assertEqual(progress._activity_shell.winfo_manager(), "")
            self.assertTrue(progress._root.protocol("WM_DELETE_WINDOW"))
            self.assertEqual(progress._handle_alt_f4(), "break")
            self.assertIsNotNone(progress._root)
            self.assertFalse(progress._closed)

            progress.set_snapshot(build_update_progress_snapshot("complete", state="complete"))
            self.assertEqual(progress._handle_alt_f4(), "break")
            self.assertIsNone(progress._root)
            self.assertTrue(progress._closed)
        finally:
            progress.close()

    def test_update_handoff_progress_ui_drag_moves_borderless_window(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        try:
            progress.show(build_update_progress_snapshot("handoff_start", state="running"))
            assert progress._root is not None
            progress._root.update_idletasks()
            start_x = int(progress._root.winfo_x())
            start_y = int(progress._root.winfo_y())

            progress._start_drag(types.SimpleNamespace(x_root=start_x + 20, y_root=start_y + 18))
            progress._drag_window(types.SimpleNamespace(x_root=start_x + 22, y_root=start_y + 20))
            progress._root.update_idletasks()
            self.assertEqual(int(progress._root.winfo_x()), start_x)
            self.assertEqual(int(progress._root.winfo_y()), start_y)

            progress._drag_window(types.SimpleNamespace(x_root=start_x + 92, y_root=start_y + 66))
            progress._root.update_idletasks()

            self.assertEqual(int(progress._root.winfo_x()), start_x + 72)
            self.assertEqual(int(progress._root.winfo_y()), start_y + 48)
            progress._end_drag()
            self.assertIsNone(progress._drag_offset)
            self.assertIsNone(progress._drag_press)

            moved_x = int(progress._root.winfo_x())
            moved_y = int(progress._root.winfo_y())
            pointer_x = moved_x + 20
            pointer_y = moved_y + 18
            progress._start_drag(types.SimpleNamespace(x_root=pointer_x, y_root=pointer_y))
            progress._drag_window(
                types.SimpleNamespace(
                    x_root=pointer_x + (-10 - moved_x),
                    y_root=pointer_y + (-20 - moved_y),
                )
            )
            progress._root.update_idletasks()
            self.assertEqual(int(progress._root.winfo_x()), -10)
            self.assertEqual(int(progress._root.winfo_y()), -20)
        finally:
            progress.close()

    def test_update_handoff_progress_ui_shows_only_real_activity_rows(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        snapshot = build_update_progress_snapshot("build", state="running")
        snapshot["activity"] = {
            "id": "uv_sync",
            "source": "build",
            "line": "uv 환경 동기화를 완료했습니다.",
        }
        try:
            progress.show(snapshot)

            self.assertIsNotNone(progress._activity_shell)
            self.assertEqual(progress._activity_shell.winfo_manager(), "pack")
            visible_labels = [
                label
                for label in progress._activity_labels
                if label.winfo_manager() and label.winfo_ismapped()
            ]
            self.assertEqual(len(progress._activity_labels), 3)
            self.assertEqual(len(visible_labels), 1)
            self.assertEqual(visible_labels[0].cget("text"), "uv 환경 동기화를 완료했습니다.")
            self.assertIsNotNone(progress._activity_timeline)
            self.assertGreater(len(progress._activity_timeline.find_all()), 0)
            assert progress._root is not None
            self.assertLessEqual(
                progress._activity_shell.winfo_rooty() + progress._activity_shell.winfo_height(),
                progress._root.winfo_rooty() + progress._root.winfo_height(),
            )
        finally:
            progress.close()

    def test_update_handoff_progress_ui_timeline_tracks_scaled_label_rows(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        try:
            snapshot = build_update_progress_snapshot("build", state="running")
            for index in range(3):
                snapshot["activity"] = {
                    "id": f"stage-{index}",
                    "source": "build",
                    "line": f"진행 단계 {index + 1}",
                }
                progress.show(snapshot) if index == 0 else progress.set_snapshot(snapshot)
            assert progress._root is not None
            assert progress._activity_timeline is not None
            progress._root.update_idletasks()
            progress._root.update()

            oval_ids = [
                item
                for item in progress._activity_timeline.find_all()
                if progress._activity_timeline.type(item) == "oval"
            ]
            self.assertEqual(len(oval_ids), 3)
            last_oval = progress._activity_timeline.coords(oval_ids[-1])
            actual_center_y = (float(last_oval[1]) + float(last_oval[3])) / 2
            last_label = progress._activity_labels[2]
            first_line_height = int(progress._activity_labels[0].winfo_height())
            expected_center_y = (
                int(last_label.winfo_rooty())
                - int(progress._activity_timeline.winfo_rooty())
                + (first_line_height / 2)
            )
            self.assertAlmostEqual(actual_center_y, expected_center_y, delta=3.0)
        finally:
            progress.close()

    def test_update_handoff_progress_ui_limits_and_sanitizes_visible_text(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        raw_detail = "C:\\Users\\someone\\build\x00\x1b[31m\u202e" + ("A" * 10000)
        try:
            progress.show(
                build_update_progress_snapshot(
                    "failed",
                    state="failed",
                    detail=raw_detail,
                    can_manual_action=True,
                )
            )

            visible_detail = str(progress._detail_label.cget("text"))
            self.assertLessEqual(len(visible_detail), 320)
            self.assertNotIn("\x00", visible_detail)
            self.assertNotIn("\x1b", visible_detail)
            self.assertNotIn("\u202e", visible_detail)
            self.assertTrue(progress._manual_button.winfo_ismapped())
            self.assertTrue(progress._close_button.winfo_ismapped())
        finally:
            progress.close()

    def test_update_handoff_progress_ui_focuses_close_when_complete(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi(log_path="update.log")
        try:
            progress.show(
                build_update_progress_snapshot(
                    "complete",
                    state="complete",
                    log_path="update.log",
                )
            )
            progress.pump()

            self.assertIs(progress._preferred_focus_button, progress._close_button)
        finally:
            progress.close()

    def test_update_handoff_progress_ui_draws_full_track_at_zero_percent(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        progress = UpdateHandoffProgressUi()
        try:
            progress.show(build_update_progress_snapshot("handoff_start", state="running"))
            progress.pump()
            items = progress._progress_canvas.find_all()
            self.assertGreaterEqual(len(items), 1)
            track = progress._progress_canvas.coords(items[0])
            self.assertGreater(track[2] - track[0], 500)
        finally:
            progress.close()

    def test_update_handoff_progress_ui_uses_requested_height_at_equivalent_150_percent(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk unavailable: {exc}")

        original_tk = tk.Tk

        def scaled_tk():
            root = original_tk()
            root.tk.call("tk", "scaling", 2.0)
            return root

        progress = UpdateHandoffProgressUi()
        try:
            with patch.object(tk, "Tk", side_effect=scaled_tk):
                progress.show(build_update_progress_snapshot("build_prepare", state="running"))
            progress.pump()
            actual_height = progress._root.winfo_height()
            requested_height = progress._root.winfo_reqheight()
            self.assertLessEqual(actual_height - requested_height, 12)
        finally:
            progress.close()

    def test_build_output_progress_is_distributed_from_early_visible_percentages(self) -> None:
        shutdown = build_update_build_output_progress_snapshot(
            "Shutting down the running windows-supporter.exe process...[ Success !! ]"
        )
        uv_sync = build_update_build_output_progress_snapshot(
            "Syncing uv environment...[ Success !! ]"
        )
        build = build_update_build_output_progress_snapshot(
            "Building main.py to windows-supporter.exe...[ Success !! ]"
        )

        self.assertIsNotNone(shutdown)
        self.assertIsNotNone(uv_sync)
        self.assertIsNotNone(build)
        assert shutdown is not None
        assert uv_sync is not None
        assert build is not None
        self.assertLessEqual(shutdown["percent"], 20)
        self.assertLessEqual(uv_sync["percent"], 35)
        self.assertLess(build["percent"], 80)
        self.assertLess(shutdown["percent"], uv_sync["percent"])
        self.assertLess(uv_sync["percent"], build["percent"])

    def test_build_output_progress_uses_korean_stage_copy_without_raw_build_line(self) -> None:
        raw_line = "Shutting down the running windows-supporter.exe process...[ Not running ]"
        snapshot = build_update_build_output_progress_snapshot(raw_line)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        activity = snapshot.get("activity", {})
        self.assertEqual(activity.get("id"), "shutdown")
        self.assertEqual(activity.get("source"), "build")
        self.assertIn("실행 중인 앱", snapshot["label"])
        self.assertIn("다음 단계", snapshot["detail"])
        self.assertNotIn("build.bat", snapshot["detail"])
        self.assertNotIn("Not running", snapshot["detail"])
        self.assertNotIn("Shutting down", str(activity.get("line") or ""))

    def test_failed_progress_can_preserve_last_meaningful_percent(self) -> None:
        snapshot = build_update_progress_snapshot(
            "failed",
            state="failed",
            detail="업데이트를 완료하지 못했습니다.",
            percent=72,
        )

        self.assertEqual(snapshot["percent"], 72)
        self.assertEqual(snapshot["progressbar"]["value"], 72)

    def test_build_output_progress_advances_for_real_substep_logs(self) -> None:
        class FakeProgressUi:
            def __init__(self) -> None:
                self.snapshots = []

            def set_snapshot(self, snapshot) -> None:
                self.snapshots.append(dict(snapshot))

        output = "\n".join(
            [
                "Building main.py to windows-supporter.exe...",
                "1432 INFO: PyInstaller: checking Analysis",
                "2179 INFO: Building PYZ (ZlibArchive)",
                "3120 INFO: Building PKG (CArchive) windows-supporter.pkg",
                "Moving windows-supporter.exe...",
            ]
        )
        progress_ui = FakeProgressUi()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "update_handoff.json"
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(build_update_handoff_payload(repo_root=tmp), fp)

            update_monitor_module.publish_build_output_progress(
                output,
                progress_ui=progress_ui,
                state_path=state_path,
                log_path=str(Path(tmp) / "update.log"),
                seen=set(),
            )

        details = [str(snapshot["detail"]) for snapshot in progress_ui.snapshots]
        activities = [
            str(snapshot.get("activity", {}).get("line") or "")
            for snapshot in progress_ui.snapshots
        ]
        percents = [int(snapshot["percent"]) for snapshot in progress_ui.snapshots]
        self.assertEqual(len(progress_ui.snapshots), 2)
        self.assertFalse(any("checking Analysis" in detail for detail in details))
        self.assertFalse(any("Building PYZ" in detail for detail in details))
        self.assertFalse(any("Building PKG" in detail for detail in details))
        self.assertFalse(any("checking Analysis" in activity for activity in activities))
        self.assertEqual(percents, sorted(percents))
        self.assertGreater(percents[-1], percents[0])

    def test_process_descendant_cleanup_script_does_not_shadow_powershell_pid(self) -> None:
        if os.name != "nt":
            self.skipTest("process descendant cleanup is a Windows command path")

        class FakeSubprocess:
            def __init__(self) -> None:
                self.argv = []

            def run(self, argv, **_kwargs):
                self.argv = list(argv)
                return types.SimpleNamespace(
                    returncode=0,
                    stdout='{"parent_pid":123,"exclude_pids":[456],"terminated_pids":[],"failed_pids":[]}',
                    stderr="",
                )

        fake_subprocess = FakeSubprocess()

        result = update_monitor_module.terminate_process_descendants(
            123,
            exclude_pids=(456,),
            subprocess_module=fake_subprocess,
        )

        script = fake_subprocess.argv[-1]
        self.assertEqual(result["exclude_pids"], [456])
        self.assertIn("function Add-Descendants([int]$treePid)", script)
        self.assertIn("$childPid -eq $PID", script)
        self.assertNotIn("function Add-Descendants([int]$pid)", script)

    def test_update_korean_ux_copy_distinguishes_cleanup_source_and_force_clean(self) -> None:
        self.assertIn("강제정리", UPDATE_FORCE_CLEAN_APPROVAL_TEXT)
        self.assertIn("stash", UPDATE_FORCE_CLEAN_APPROVAL_TEXT)
        self.assertIn("백업 브랜치", UPDATE_FORCE_CLEAN_APPROVAL_TEXT)
        self.assertNotIn("백업 브랜치/태그", UPDATE_FORCE_CLEAN_APPROVAL_TEXT)
        self.assertIn("reset/동기화", UPDATE_FORCE_CLEAN_APPROVAL_TEXT)
        self.assertIn("강제정리가 취소", UPDATE_FORCE_CLEAN_REJECTED_NOTICE)
        self.assertIn("stash", UPDATE_SOURCE_CHANGE_NOTICE)
        self.assertNotIn("커밋되지 않은 변경", UPDATE_CLEANUP_ONLY_NOTICE)
        self.assertIn("빌드 산출물", UPDATE_CLEANUP_ONLY_NOTICE)
        message = build_force_clean_approval_message(
            UpdateWorkingTreeState(local_only_count=2, remote_only_count=1)
        )
        self.assertIn("로컬 전용 커밋: 2개", message)
        self.assertIn("원격 전용 커밋: 1개", message)

    def test_git_checkout_root_requires_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_git_checkout_root(tmp))
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../.git/worktrees/example\n")
            self.assertTrue(is_git_checkout_root(tmp))

    def test_start_marks_update_unavailable_outside_git_checkout(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))

        with tempfile.TemporaryDirectory() as tmp:
            root = FakeRoot()
            snapshots = []
            updater = WindowsSupporterUpdater(
                root=root,
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                status_changed_callback=lambda: snapshots.append(updater.get_status_snapshot()),
            )

            updater.start()

        self.assertEqual(root.after_calls, [])
        self.assertEqual(snapshots[-1]["state"], "unavailable")

    def test_manual_check_reports_unavailable_outside_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            messages = []
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
            )
            updater._show_info = lambda title, message: messages.append((title, message))

            updater.check_now(manual=True)

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("Git checkout", snapshot["last_error"])
        self.assertEqual(len(messages), 1)

    def test_start_marks_update_unavailable_in_codex_temporary_worktree(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = os.path.join(tmp, ".codex", "worktrees", "9f9a", "windows-supporter")
            os.makedirs(repo_root)
            with open(os.path.join(repo_root, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../../../main/.git/worktrees/9f9a\n")
            root = FakeRoot()
            snapshots = []
            updater = WindowsSupporterUpdater(
                root=root,
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=repo_root,
                status_changed_callback=lambda: snapshots.append(updater.get_status_snapshot()),
            )

            updater.start()

        self.assertEqual(root.after_calls, [])
        self.assertEqual(snapshots[-1]["state"], "unavailable")
        self.assertIn("main worktree", snapshots[-1]["last_error"])

    def test_start_marks_update_unavailable_in_non_primary_linked_worktree(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))

        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "main", "windows-supporter")
            linked = os.path.join(tmp, "linked", "windows-supporter")
            os.makedirs(primary)
            os.makedirs(linked)
            with open(os.path.join(linked, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../.git/worktrees/feature\n")
            porcelain = "\n".join(
                [
                    f"worktree {primary.replace(os.sep, '/')}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                    f"worktree {linked.replace(os.sep, '/')}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/codex/feature",
                    "",
                ]
            )

            def worktree_runner(_argv, **_kwargs):
                return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

            root = FakeRoot()
            updater = WindowsSupporterUpdater(
                root=root,
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=linked,
                worktree_runner=worktree_runner,
            )

            updater.start()

        snapshot = updater.get_status_snapshot()
        self.assertEqual(root.after_calls, [])
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("main worktree", snapshot["last_error"])

    def test_manual_check_reports_unavailable_in_codex_temporary_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = os.path.join(tmp, ".codex", "worktrees", "9f9a", "windows-supporter")
            os.makedirs(repo_root)
            with open(os.path.join(repo_root, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../../../main/.git/worktrees/9f9a\n")
            messages = []
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=repo_root,
            )
            updater._show_info = lambda title, message: messages.append((title, message))

            updater.check_now(manual=True)

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("main worktree", snapshot["last_error"])
        self.assertEqual(len(messages), 1)

    def test_update_launch_rechecks_dirty_state_before_helper_stash(self) -> None:
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
        )
        warnings = []
        launches = []
        updater._ask_update = lambda _candidate: True
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(
            source_status=("M src/main.py",)
        )
        updater._show_warning = lambda title, message: warnings.append((title, message))
        updater._prepare_repository_for_update = lambda _working_tree: True
        updater.launch_update = lambda: launches.append(True) or True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(),
            error="",
            manual=True,
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("커밋되지 않은 변경", warnings[0][1])
        self.assertIn("stash", warnings[0][1])
        self.assertEqual(launches, [True])

    def test_cleanup_only_update_launch_does_not_show_uncommitted_warning(self) -> None:
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
        )
        warnings = []
        launches = []
        updater._ask_update = lambda _candidate: True
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(
            cleanup_targets=("build/generated.tmp",)
        )
        updater._show_warning = lambda title, message: warnings.append((title, message))
        updater._prepare_repository_for_update = lambda _working_tree: True
        updater.launch_update = lambda: launches.append(True) or True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(cleanup_targets=("build/generated.tmp",)),
            error="",
            manual=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(launches, [True])

    def test_force_clean_rejection_stops_before_destructive_commands(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                self.commands.append(list(argv))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
        )
        launches = []
        updater._ask_update = lambda _candidate: True
        updater._ask_force_clean = lambda _working_tree: False
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(
            source_status=("M src/main.py",),
            local_only_count=1,
            remote_only_count=1,
        )
        updater.launch_update = lambda: launches.append(True) or True
        updater._show_warning = lambda _title, _message: None

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(),
            error="",
            manual=True,
        )

        snapshot = updater.get_status_snapshot()
        destructive_verbs = {"stash", "branch", "switch", "reset", "clean"}
        self.assertFalse(
            any(command[1] in destructive_verbs for command in fake_subprocess.commands)
        )
        self.assertEqual(launches, [])
        self.assertEqual(snapshot["state"], "cancelled")
        self.assertIn("강제정리가 취소", snapshot["last_error"])
        self.assertEqual(snapshot["progress"]["failed_step"], "강제정리 승인")

    def test_force_clean_approval_sequences_stash_backup_sync_clean_before_handoff(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                command = list(argv)
                self.commands.append(command)
                if command == build_short_head_command("main"):
                    return types.SimpleNamespace(returncode=0, stdout="abc123", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
            timestamp_provider=lambda: "20260614-120000",
        )
        launches = []
        backup_branch = build_backup_branch_name("20260614-120000", "abc123")
        working_tree = UpdateWorkingTreeState(
            source_status=("M src/main.py",),
            cleanup_targets=("build/generated.tmp",),
            local_only_count=1,
            remote_only_count=2,
        )
        updater._ask_update = lambda _candidate: True
        updater._ask_force_clean = lambda _working_tree: True
        updater._inspect_working_tree_state = lambda: working_tree
        updater._show_warning = lambda _title, _message: None
        updater.launch_update = lambda: launches.append(dict(updater._preflight_result)) or True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(),
            error="",
            manual=True,
        )

        self.assertEqual(
            fake_subprocess.commands,
            [
                build_fetch_origin_command(),
                build_stash_command("windows-supporter auto update 20260614-120000"),
                build_short_head_command("main"),
                build_backup_branch_command(backup_branch, "main"),
                build_switch_main_command(),
                build_reset_main_command(),
                build_allowed_clean_command(),
            ],
        )
        self.assertEqual(len(launches), 1)
        self.assertTrue(launches[0]["force_clean_approved"])
        self.assertEqual(launches[0]["backup_branch"], backup_branch)
        self.assertEqual(launches[0]["cleaned_targets"], ["build/generated.tmp"])

    def test_post_fetch_remote_only_state_fast_forwards_before_handoff(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                self.commands.append(list(argv))
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
        )
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(remote_only_count=1)

        self.assertTrue(updater._prepare_repository_for_update(UpdateWorkingTreeState()))

        self.assertEqual(
            fake_subprocess.commands,
            [
                build_fetch_origin_command(),
                build_switch_main_command(),
                build_fast_forward_main_command(),
            ],
        )

    def test_git_gui_blocker_prompts_for_close_and_continue(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                self.commands.append(list(argv))
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
            git_gui_process_detector=lambda _repo_root: ("Fork.exe",),
        )
        prompts = []
        updater._ask_close_git_gui_processes = (
            lambda processes: prompts.append(tuple(processes)) or False
        )

        self.assertFalse(
            updater._prepare_repository_for_update(
                UpdateWorkingTreeState(remote_only_count=1)
            )
        )

        snapshot = updater.get_status_snapshot()
        self.assertEqual(prompts, [("Fork.exe",)])
        self.assertEqual(fake_subprocess.commands, [])
        self.assertEqual(snapshot["state"], "cancelled")
        self.assertIn("Fork.exe", snapshot["last_error"])
        self.assertEqual(snapshot["progress"]["failed_step"], "Git GUI 확인")
        self.assertEqual(snapshot["progress"]["state"], "cancelled")

    def test_git_gui_close_and_continue_waits_and_records_relaunch(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                self.commands.append(list(argv))
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
            git_gui_process_detector=lambda _repo_root: ("Fork.exe",),
        )
        prompts = []
        close_calls = []
        updater._ask_close_git_gui_processes = (
            lambda processes: prompts.append(tuple(processes)) or True
        )
        updater._close_git_gui_processes_for_update = lambda processes: close_calls.append(
            tuple(processes)
        ) or {
            "closed": ["Fork.exe"],
            "relaunch": [{"name": "Fork.exe", "command": ["Fork.exe"]}],
        }
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(
            remote_only_count=1
        )

        self.assertTrue(
            updater._prepare_repository_for_update(
                UpdateWorkingTreeState(remote_only_count=1)
            )
        )

        self.assertEqual(prompts, [("Fork.exe",)])
        self.assertEqual(close_calls, [("Fork.exe",)])
        self.assertEqual(
            fake_subprocess.commands,
            [build_fetch_origin_command(), build_switch_main_command(), build_fast_forward_main_command()],
        )
        self.assertEqual(updater._preflight_result["git_gui_processes"], ["Fork.exe"])
        self.assertTrue(updater._preflight_result["git_gui_close_approved"])
        self.assertEqual(
            updater._preflight_result["git_gui_relaunch"],
            [{"name": "Fork.exe", "command": ["Fork.exe"]}],
        )

    def test_close_running_git_gui_processes_parses_relaunch_metadata(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.calls = []

            def run(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=(
                        '{"closed":["Fork.exe"],"still_running":[],'
                        '"relaunch":[{"name":"Fork.exe","command":["C:/Apps/Fork/Fork.exe"]}]}'
                    ),
                    stderr="",
                )

        fake_subprocess = FakeSubprocess()
        with patch.object(update_monitor_module.os, "name", "nt"):
            result = close_running_git_gui_processes(
                ("Fork.exe",),
                subprocess_module=fake_subprocess,
            )

        self.assertEqual(fake_subprocess.calls[0][0][:4], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"])
        self.assertNotIn("Stop-Process", fake_subprocess.calls[0][0][-1])
        self.assertEqual(result["closed"], ["Fork.exe"])
        self.assertEqual(result["still_running"], [])
        self.assertEqual(
            result["relaunch"],
            [{"name": "Fork.exe", "command": ["C:/Apps/Fork/Fork.exe"]}],
        )

    def test_update_approval_publishes_preflight_progress_before_git_fetch(self) -> None:
        class FakeSubprocess:
            def __init__(self, snapshots) -> None:
                self.commands = []
                self.snapshots = snapshots

            def run(self, argv, **_kwargs):
                self.snapshots.append(updater.get_status_snapshot()["progress"])
                self.commands.append(list(argv))
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        snapshots = []
        fake_subprocess = FakeSubprocess(snapshots)
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
            status_changed_callback=lambda: snapshots.append(
                updater.get_status_snapshot()["progress"]
            ),
        )
        updater._ask_update = lambda _candidate: True
        updater._inspect_working_tree_state = lambda: UpdateWorkingTreeState(
            remote_only_count=1
        )
        updater.launch_update = lambda: True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(),
            error="",
            manual=True,
        )

        step_keys = [snapshot["step_key"] for snapshot in snapshots]
        first_fetch_index = step_keys.index("fetch")
        self.assertIn("accepted", step_keys[:first_fetch_index])
        self.assertIn("preflight", step_keys[:first_fetch_index])
        self.assertLess(get_update_progress_step("build").percent, 80)
        self.assertLess(get_update_progress_step("fetch").percent, get_update_progress_step("build").percent)

    def test_update_approval_fails_closed_when_git_state_inspection_fails(self) -> None:
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
        )
        launches = []
        updater._ask_update = lambda _candidate: True
        updater._inspect_working_tree_state = lambda: (_ for _ in ()).throw(
            RuntimeError("git status failed")
        )
        updater.launch_update = lambda: launches.append(True) or True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            working_tree=UpdateWorkingTreeState(),
            error="",
            manual=True,
        )

        snapshot = updater.get_status_snapshot()
        self.assertEqual(launches, [])
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("Git 상태를 확인할 수 없습니다", snapshot["last_error"])
        self.assertEqual(snapshot["progress"]["failed_step"], "Git 상태 확인")

    def test_ignored_build_outputs_count_as_update_cleanup_targets(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.commands = []

            def run(self, argv, **_kwargs):
                self.commands.append(list(argv))
                output = "Would remove build/generated.tmp" if "-ndX" in argv else ""
                return types.SimpleNamespace(returncode=0, stdout=output, stderr="")

        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=FakeSubprocess(),
        )

        self.assertTrue(updater._has_update_cleanup_targets())

    def test_checking_state_notifies_dashboard_immediately(self) -> None:
        class DeferredThread:
            def __init__(self, target=None, daemon=False) -> None:
                self.target = target

            def start(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            snapshots = []
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                thread_factory=DeferredThread,
                status_changed_callback=lambda: snapshots.append(updater.get_status_snapshot()),
                worktree_runner=_primary_worktree_runner(tmp),
            )

            updater.check_now(manual=True)

        self.assertEqual(snapshots[0]["state"], "checking")
        self.assertEqual(snapshots[0]["progress"]["label"], "업데이트 확인 중")
        self.assertTrue(snapshots[0]["progress"]["progressbar"]["visible"])

    def test_git_output_is_noninteractive_and_bounded(self) -> None:
        class FakeStartupInfo:
            def __init__(self) -> None:
                self.dwFlags = 0
                self.wShowWindow = None

        class FakeSubprocess:
            CREATE_NO_WINDOW = 0x08000000
            STARTF_USESHOWWINDOW = 1
            SW_HIDE = 0
            STARTUPINFO = FakeStartupInfo

            def __init__(self) -> None:
                self.kwargs = None

            def run(self, _argv, **kwargs):
                self.kwargs = dict(kwargs)
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        fake_subprocess = FakeSubprocess()
        updater = WindowsSupporterUpdater(
            root=object(),
            event_queue=types.SimpleNamespace(put=lambda callback: callback()),
            repo_root=".",
            subprocess_module=fake_subprocess,
        )

        self.assertEqual(updater._git_output(["git", "status"]), "ok")
        self.assertEqual(fake_subprocess.kwargs["timeout"], GIT_COMMAND_TIMEOUT_SECONDS)
        self.assertEqual(fake_subprocess.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(fake_subprocess.kwargs["creationflags"], 0x08000000)
        self.assertIsInstance(fake_subprocess.kwargs["startupinfo"], FakeStartupInfo)
        self.assertEqual(fake_subprocess.kwargs["startupinfo"].dwFlags, 1)
        self.assertEqual(fake_subprocess.kwargs["startupinfo"].wShowWindow, 0)

    def test_git_timeout_resets_state_and_allows_manual_retry(self) -> None:
        class InlineThread:
            def __init__(self, target=None, daemon=False) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        class ImmediateQueue:
            def put(self, callback) -> None:
                callback()

        class TimeoutSubprocess:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, argv, **kwargs):
                self.calls += 1
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            fake_subprocess = TimeoutSubprocess()
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=ImmediateQueue(),
                repo_root=tmp,
                subprocess_module=fake_subprocess,
                thread_factory=InlineThread,
                worktree_runner=_primary_worktree_runner(tmp),
            )
            updater._show_info = lambda _title, _message: None

            updater.check_now(manual=True)
            self.assertEqual(updater.get_status_snapshot()["state"], "error")
            self.assertIn("timed out", updater.get_status_snapshot()["last_error"])
            first_call_count = fake_subprocess.calls

            updater.check_now(manual=True)

        self.assertGreater(fake_subprocess.calls, first_call_count)

    def test_run_update_handoff_builds_with_skip_env_and_relaunches(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.calls = []

            def run(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                return types.SimpleNamespace(returncode=0, stdout="built", stderr="")

        class FakeProgressUi:
            def __init__(self, *, log_path="") -> None:
                self.log_path = str(log_path)
                self.snapshots = []
                self.pump_calls = 0
                self.close_calls = 0
                self.retry_calls = 0

            def show(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def set_snapshot(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def pump(self):
                self.pump_calls += 1

            def close(self):
                self.close_calls += 1

            def wait_for_retry_or_close(self):
                self.retry_calls += 1
                return False

        launches = []
        fake_subprocess = FakeSubprocess()
        progress_instances = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "windows-supporter.exe").write_text("exe", encoding="utf-8")
            state_path = Path(tmp) / "update_handoff.json"
            write_payload = build_update_handoff_payload(
                repo_root=repo,
                target_tag="v0.5.7",
                log_path=Path(tmp) / "update.log",
            )
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(write_payload, fp)

            rc = run_update_handoff(
                state_path,
                subprocess_module=fake_subprocess,
                launch=lambda command, **kwargs: launches.append((command, dict(kwargs)))
                or StablePopen(),
                progress_ui_factory=lambda **kwargs: progress_instances.append(
                    FakeProgressUi(**kwargs)
                )
                or progress_instances[-1],
            )
            state = read_update_handoff_state(state_path)

        self.assertEqual(rc, 0)
        self.assertEqual(fake_subprocess.calls[0][0], ["cmd", "/c", "build.bat"])
        self.assertEqual(fake_subprocess.calls[0][1]["cwd"], str(repo))
        self.assertEqual(
            fake_subprocess.calls[0][1]["env"]["WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN"],
            "1",
        )
        self.assertEqual(launches[0][0], [str(repo / "windows-supporter.exe")])
        self.assertEqual(launches[0][1]["cwd"], str(repo))
        self.assertEqual(
            launches[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"],
            "1",
        )
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["progress"]["label"], "업데이트 완료")
        self.assertEqual(progress_instances[0].snapshots[0]["step_key"], "handoff_start")
        self.assertLessEqual(progress_instances[0].snapshots[0]["percent"], 5)
        self.assertEqual(
            [snapshot["label"] for snapshot in progress_instances[0].snapshots],
            [
                "업데이트 프로세스 시작",
                "기존 앱 정리 중",
                "빌드 준비 중",
                "Windows Supporter 재실행 중",
                "업데이트 완료",
            ],
        )
        self.assertEqual(progress_instances[0].close_calls, 1)

    def test_run_update_handoff_relaunches_approved_git_gui_apps(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.calls = []

            def run(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                return types.SimpleNamespace(returncode=0, stdout="built", stderr="")

        launches = []
        fake_subprocess = FakeSubprocess()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "windows-supporter.exe").write_text("exe", encoding="utf-8")
            state_path = Path(tmp) / "update_handoff.json"
            payload = build_update_handoff_payload(
                repo_root=repo,
                target_tag="v0.5.7",
                log_path=Path(tmp) / "update.log",
                preflight={
                    "git_gui_relaunch": [
                        {"name": "Fork.exe", "command": ["C:/Apps/Fork/Fork.exe"]}
                    ]
                },
            )
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(payload, fp)

            rc = run_update_handoff(
                state_path,
                subprocess_module=fake_subprocess,
                launch=lambda command, **kwargs: launches.append((command, dict(kwargs)))
                or StablePopen(),
                progress_ui_factory=lambda **_kwargs: None,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(launches[0][0], [str(repo / "windows-supporter.exe")])
        self.assertEqual(launches[0][1]["cwd"], str(repo))
        self.assertEqual(
            launches[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"],
            "1",
        )
        self.assertEqual(launches[1], (["C:/Apps/Fork/Fork.exe"], {"cwd": str(repo)}))

    def test_run_no_window_with_progress_pumps_ui_while_waiting_for_build_output(self) -> None:
        class SlowStdout:
            def __init__(self, owner) -> None:
                self.owner = owner
                self.line_requested = threading.Event()
                self.release_line = threading.Event()
                self.lines = ["Building main.py to windows-supporter.exe...[ Success !! ]\n"]

            def readline(self):
                self.line_requested.set()
                self.release_line.wait(timeout=2)
                if self.lines:
                    return self.lines.pop(0)
                self.owner.returncode = 0
                return ""

        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.stdout = SlowStdout(self)

            def poll(self):
                return self.returncode

            def wait(self, timeout=0):
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = 1

        class FakeSubprocess:
            PIPE = subprocess.PIPE
            STDOUT = subprocess.STDOUT

            def __init__(self) -> None:
                self.process = FakeProcess()

            def Popen(self, argv, **kwargs):
                return self.process

        class FakeProgressUi:
            def __init__(self, stdout: SlowStdout) -> None:
                self.stdout = stdout
                self.pumped_before_first_line = False
                self.pump_calls = 0

            def pump(self):
                self.pump_calls += 1
                if self.stdout.line_requested.is_set() and not self.stdout.release_line.is_set():
                    self.pumped_before_first_line = True

        fake_subprocess = FakeSubprocess()
        progress_ui = FakeProgressUi(fake_subprocess.process.stdout)
        holder = {}

        def run_command() -> None:
            holder["result"] = run_no_window_with_progress(
                ["cmd", "/c", "build.bat"],
                subprocess_module=fake_subprocess,
                progress_ui=progress_ui,
                capture_output=True,
                text=True,
                pump_interval_seconds=0.01,
            )

        worker = threading.Thread(target=run_command)
        worker.start()
        self.assertTrue(fake_subprocess.process.stdout.line_requested.wait(timeout=1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not progress_ui.pumped_before_first_line:
            time.sleep(0.01)
        fake_subprocess.process.stdout.release_line.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(progress_ui.pumped_before_first_line)
        self.assertIn("Building main.py", holder["result"].stdout)

    def test_run_update_handoff_streams_build_progress_lines_to_ui(self) -> None:
        class FakeStdout:
            def __init__(self, owner) -> None:
                self.owner = owner
                self.lines = [
                    "Shutting down the running windows-supporter.exe process...[ Success !! ]\n",
                    "Syncing uv environment...[ Success !! ]\n",
                    "Generating version metadata...[ Success !! ]\n",
                    "Building main.py to windows-supporter.exe...[ Success !! ]\n",
                    "Moving windows-supporter.exe...[ Success !! ]\n",
                    "Skipping post-build launch because WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1.\n",
                ]

            def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                time.sleep(0.05)
                self.owner.returncode = 0
                return ""

            def read(self):
                return ""

        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.stdout = FakeStdout(self)

            def poll(self):
                return self.returncode

            def wait(self, timeout=0):
                self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = 1

        class FakeSubprocess:
            def __init__(self) -> None:
                self.calls = []
                self.process = FakeProcess()
                self.PIPE = subprocess.PIPE
                self.STDOUT = subprocess.STDOUT

            def Popen(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                return self.process

        class FakeProgressUi:
            def __init__(self, *, log_path="", process=None) -> None:
                self.snapshots = []
                self.streamed_before_exit = False
                self.process = process

            def show(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def set_snapshot(self, snapshot):
                if (
                    self.process is not None
                    and self.process.returncode is None
                    and snapshot.get("label") == "실행 파일 빌드 중"
                ):
                    self.streamed_before_exit = True
                self.snapshots.append(dict(snapshot))

            def pump(self):
                return None

            def close(self):
                return None

            def wait_for_retry_or_close(self):
                return False

        fake_subprocess = FakeSubprocess()
        progress_instances = []
        launches = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "windows-supporter.exe").write_text("exe", encoding="utf-8")
            state_path = Path(tmp) / "update_handoff.json"
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(build_update_handoff_payload(repo_root=repo), fp)

            rc = run_update_handoff(
                state_path,
                subprocess_module=fake_subprocess,
                launch=lambda command, **kwargs: launches.append((command, kwargs)) or StablePopen(),
                progress_ui_factory=lambda **kwargs: progress_instances.append(
                    FakeProgressUi(process=fake_subprocess.process, **kwargs)
                )
                or progress_instances[-1],
            )

        labels = [snapshot["label"] for snapshot in progress_instances[0].snapshots]
        details = [snapshot["detail"] for snapshot in progress_instances[0].snapshots]
        self.assertEqual(rc, 0)
        self.assertIn("실행 중인 앱 확인 중", labels)
        self.assertIn("빌드 환경 동기화 중", labels)
        self.assertIn("버전 정보 생성 중", labels)
        self.assertIn("실행 파일 빌드 중", labels)
        self.assertIn("실행 파일 배치 중", labels)
        self.assertIn("Windows Supporter 재실행 중", labels)
        self.assertTrue(progress_instances[0].streamed_before_exit)
        self.assertFalse(any("build.bat 단계" in detail for detail in details))
        self.assertFalse(any("[ Success !! ]" in detail for detail in details))

    def test_run_update_handoff_tails_emitted_build_step_log(self) -> None:
        class FakeStdout:
            def __init__(self, owner, step_log: Path) -> None:
                self.owner = owner
                self.step_log = step_log
                self.index = 0

            def readline(self):
                if self.index == 0:
                    self.index += 1
                    return f"WINDOWS_SUPPORTER_STEP_LOG={self.step_log}\n"
                if self.index == 1:
                    self.index += 1
                    return "Generating version metadata...[ Success !! ]\n"
                if self.index == 2:
                    self.index += 1
                    self.step_log.write_text(
                        "\n".join(
                            [
                                "1432 INFO: PyInstaller: checking Analysis",
                                "2179 INFO: Building PYZ (ZlibArchive)",
                                "3120 INFO: Building PKG (CArchive) windows-supporter.pkg",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    time.sleep(0.2)
                    return "Building main.py to windows-supporter.exe...[ Success !! ]\n"
                if self.index == 3:
                    self.index += 1
                    return "Skipping post-build launch because WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1.\n"
                time.sleep(0.05)
                self.owner.returncode = 0
                return ""

            def read(self):
                return ""

        class FakeProcess:
            def __init__(self, step_log: Path) -> None:
                self.returncode = None
                self.stdout = FakeStdout(self, step_log)

            def poll(self):
                return self.returncode

            def wait(self, timeout=0):
                self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = 1

        class FakeSubprocess:
            def __init__(self, step_log: Path) -> None:
                self.calls = []
                self.process = FakeProcess(step_log)
                self.PIPE = subprocess.PIPE
                self.STDOUT = subprocess.STDOUT

            def Popen(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                return self.process

        class FakeProgressUi:
            def __init__(self, *, log_path="", process=None) -> None:
                self.snapshots = []
                self.tailed_before_exit = False
                self.process = process

            def show(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def set_snapshot(self, snapshot):
                if (
                    self.process is not None
                    and self.process.returncode is None
                    and "checking Analysis" in str(snapshot.get("detail") or "")
                ):
                    self.tailed_before_exit = True
                self.snapshots.append(dict(snapshot))

            def pump(self):
                return None

            def close(self):
                return None

            def wait_for_retry_or_close(self):
                return False

        progress_instances = []
        launches = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "windows-supporter.exe").write_text("exe", encoding="utf-8")
            step_log = Path(tmp) / "windows-supporter-build.log"
            fake_subprocess = FakeSubprocess(step_log)
            state_path = Path(tmp) / "update_handoff.json"
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(
                    build_update_handoff_payload(
                        repo_root=repo,
                        log_path=Path(tmp) / "update.log",
                    ),
                    fp,
                )

            rc = run_update_handoff(
                state_path,
                subprocess_module=fake_subprocess,
                launch=lambda command, **kwargs: launches.append((command, kwargs)) or StablePopen(),
                progress_ui_factory=lambda **kwargs: progress_instances.append(
                    FakeProgressUi(process=fake_subprocess.process, **kwargs)
                )
                or progress_instances[-1],
            )
            raw_log = (Path(tmp) / "update.log").read_text(encoding="utf-8")

        details = [str(snapshot["detail"]) for snapshot in progress_instances[0].snapshots]
        call_env = fake_subprocess.calls[0][1]["env"]
        self.assertEqual(rc, 0)
        self.assertEqual(call_env["WINDOWS_SUPPORTER_EMIT_STEP_LOG"], "1")
        self.assertFalse(progress_instances[0].tailed_before_exit)
        self.assertFalse(any("checking Analysis" in detail for detail in details))
        self.assertFalse(any("Building PYZ" in detail for detail in details))
        self.assertFalse(any("Building PKG" in detail for detail in details))
        self.assertIn("checking Analysis", raw_log)
        self.assertIn("Building PYZ", raw_log)
        self.assertIn("Building PKG", raw_log)

    def test_approving_update_shows_handoff_ui_before_build_completes(self) -> None:
        snapshots_during_ack = []
        launches = []
        quit_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            state_path = Path(tmp) / "update_handoff.json"
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                quit_callback=lambda: quit_calls.append(True),
                handoff_path_provider=lambda: state_path,
                handoff_command_builder=lambda path: ["python", "main.py", UPDATE_HANDOFF_ARG, str(path)],
                handoff_ack_waiter=lambda _path: snapshots_during_ack.append(
                    updater.get_status_snapshot()
                )
                or True,
                worktree_runner=_primary_worktree_runner(tmp),
            )
            updater._latest_tag = "v0.5.7"

            self.assertTrue(updater.launch_update())

        self.assertEqual(launches[0][0][-2:], [UPDATE_HANDOFF_ARG, str(state_path)])
        self.assertEqual(snapshots_during_ack[0]["state"], "updating")
        self.assertEqual(snapshots_during_ack[0]["progress"]["step_key"], "handoff")
        self.assertEqual(snapshots_during_ack[0]["progress"]["percent"], 68)
        self.assertEqual(quit_calls, [True])

    def test_auto_update_settings_persist_and_gate_scheduling(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.after_calls = []
                self.after_cancel_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, after_id):
                self.after_cancel_calls.append(after_id)

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            settings_path = Path(tmp) / "update_settings.json"
            root = FakeRoot()
            updater = WindowsSupporterUpdater(
                root=root,
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                settings_path_provider=lambda: settings_path,
                worktree_runner=_primary_worktree_runner(tmp),
            )

            settings = updater.get_settings_snapshot()
            self.assertTrue(settings["auto_check_enabled"])
            self.assertEqual(settings["check_interval_minutes"], 10)
            self.assertEqual(settings["settings_path"], str(settings_path))

            ok, error = updater.update_settings(
                {"auto_check_enabled": True, "check_interval_minutes": 1}
            )
            self.assertFalse(ok)
            self.assertIn("3분 이상", str(error))
            self.assertEqual(updater.get_settings_snapshot()["check_interval_minutes"], 10)
            self.assertFalse(settings_path.exists())

            updater.start()
            self.assertEqual(root.after_calls[0][0], updater.INITIAL_CHECK_DELAY_MS)

            ok, error = updater.update_settings(
                {"auto_check_enabled": False, "check_interval_minutes": 3}
            )
            self.assertTrue(ok, error)
            self.assertFalse(updater.get_settings_snapshot()["auto_check_enabled"])
            self.assertEqual(updater.get_settings_snapshot()["check_interval_minutes"], 3)
            self.assertEqual(root.after_cancel_calls, ["after-1"])
            root.after_calls[0][1]()
            self.assertEqual(len(root.after_calls), 1)

            again = WindowsSupporterUpdater(
                root=root,
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                settings_path_provider=lambda: settings_path,
                worktree_runner=_primary_worktree_runner(tmp),
            )
            self.assertFalse(again.get_settings_snapshot()["auto_check_enabled"])
            again.start()

        self.assertEqual(len(root.after_calls), 1)

    def test_run_update_handoff_retries_after_failure_when_ui_requests_retry(self) -> None:
        class FakeSubprocess:
            def __init__(self) -> None:
                self.calls = []

            def run(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                if len(self.calls) == 1:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="failed")
                return types.SimpleNamespace(returncode=0, stdout="built", stderr="")

        class RetryingProgressUi:
            def __init__(self, *, log_path="") -> None:
                self.snapshots = []
                self.retry_waits = 0

            def show(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def set_snapshot(self, snapshot):
                self.snapshots.append(dict(snapshot))

            def pump(self):
                return None

            def close(self):
                return None

            def wait_for_retry_or_close(self):
                self.retry_waits += 1
                return True

        fake_subprocess = FakeSubprocess()
        progress_instances = []
        launches = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "windows-supporter.exe").write_text("exe", encoding="utf-8")
            state_path = Path(tmp) / "update_handoff.json"
            with open(state_path, "w", encoding="utf-8") as fp:
                import json

                json.dump(build_update_handoff_payload(repo_root=repo), fp)

            rc = run_update_handoff(
                state_path,
                subprocess_module=fake_subprocess,
                launch=lambda command, **kwargs: launches.append((command, kwargs)) or StablePopen(),
                progress_ui_factory=lambda **kwargs: progress_instances.append(
                    RetryingProgressUi(**kwargs)
                )
                or progress_instances[-1],
                max_attempts=2,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(fake_subprocess.calls), 2)
        self.assertEqual(progress_instances[0].retry_waits, 1)
        self.assertIn("업데이트 실패", [item["label"] for item in progress_instances[0].snapshots])
        self.assertEqual(launches[0][1]["cwd"], str(repo))

    def test_update_handoff_argv_exits_with_handoff_result_code(self) -> None:
        with patch.object(update_monitor_module, "run_update_handoff", return_value=7):
            with self.assertRaises(SystemExit) as caught:
                update_monitor_module.run_update_handoff_from_argv(
                    ["windows-supporter.exe", UPDATE_HANDOFF_ARG, "state.json"]
                )

        self.assertEqual(caught.exception.code, 7)

    def test_launch_update_writes_handoff_state_and_quits_after_ack(self) -> None:
        launches = []
        acked_paths = []
        quit_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            porcelain = "\n".join(
                [
                    f"worktree {tmp.replace(os.sep, '/')}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                ]
            )
            state_path = Path(tmp) / "update_handoff.json"
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                quit_callback=lambda: quit_calls.append(True),
                handoff_path_provider=lambda: state_path,
                handoff_command_builder=lambda path: ["python", "main.py", UPDATE_HANDOFF_ARG, str(path)],
                handoff_ack_waiter=lambda path: acked_paths.append(str(path)) or True,
                worktree_runner=lambda _argv, **_kwargs: types.SimpleNamespace(
                    returncode=0,
                    stdout=porcelain,
                    stderr="",
                ),
            )
            updater._latest_tag = "v0.5.7"
            updater._working_tree_state = UpdateWorkingTreeState(
                cleanup_targets=("build/generated.tmp",)
            )
            updater._preflight_result = {"force_clean_approved": False, "backup_branch": ""}

            self.assertTrue(updater.launch_update())
            state = read_update_handoff_state(state_path)

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "updating")
        self.assertEqual(snapshot["progress"]["label"], "업데이트 실행 준비 중")
        self.assertEqual(snapshot["progress"]["percent"], 68)
        self.assertEqual(
            launches,
            [
                (
                    [
                        "python",
                        "main.py",
                        UPDATE_HANDOFF_ARG,
                        str(state_path),
                    ],
                    {"cwd": str(Path(tmp).resolve())},
                )
            ],
        )
        self.assertEqual(acked_paths, [str(state_path)])
        self.assertEqual(quit_calls, [True])
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["repo_root"], str(Path(tmp).resolve()))
        self.assertEqual(state["target_tag"], "v0.5.7")
        self.assertEqual(
            state["recovery_executable_path"],
            str(get_update_handoff_executable_path(state_path.parent)),
        )
        self.assertEqual(state["working_tree"]["cleanup_targets"], ["build/generated.tmp"])
        self.assertEqual(state["preflight"]["force_clean_approved"], False)

    def test_launch_update_cleans_current_process_descendants_before_exit(self) -> None:
        events = []

        class HandoffProcess:
            pid = 4242

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            state_path = Path(tmp) / "update_handoff.json"
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda _command, **_kwargs: HandoffProcess(),
                quit_callback=lambda: events.append(("quit",)),
                handoff_path_provider=lambda: state_path,
                handoff_command_builder=lambda path: ["python", "main.py", UPDATE_HANDOFF_ARG, str(path)],
                handoff_ack_waiter=lambda _path: True,
                worktree_runner=_primary_worktree_runner(tmp),
            )
            updater._latest_tag = "v0.5.7"

            with patch.object(
                update_monitor_module,
                "terminate_process_descendants",
                side_effect=lambda pid, **kwargs: events.append(
                    (
                        "cleanup",
                        pid,
                        tuple(kwargs.get("exclude_pids") or ()),
                    )
                )
                or {"terminated_pids": []},
            ):
                self.assertTrue(updater.launch_update())

        self.assertEqual(events[0], ("cleanup", os.getpid(), (4242,)))
        self.assertEqual(events[1], ("quit",))

    def test_launch_update_quits_current_process_before_waiting_for_handoff_ack(self) -> None:
        launches = []
        quit_calls = []
        quit_calls_observed_by_ack = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            state_path = Path(tmp) / "update_handoff.json"
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs)))
                or object(),
                quit_callback=lambda: quit_calls.append(True),
                handoff_path_provider=lambda: state_path,
                handoff_command_builder=lambda path: [
                    "python",
                    "main.py",
                    UPDATE_HANDOFF_ARG,
                    str(path),
                ],
                handoff_ack_waiter=lambda _path: quit_calls_observed_by_ack.append(
                    list(quit_calls)
                )
                or True,
                worktree_runner=_primary_worktree_runner(tmp),
            )
            updater._latest_tag = "v0.5.7"

            self.assertTrue(updater.launch_update())

        self.assertEqual(len(launches), 1)
        self.assertEqual(quit_calls, [True])
        self.assertEqual(quit_calls_observed_by_ack, [[True]])

    def test_launch_update_reports_ack_failure_after_requesting_quit_without_exit_callback(self) -> None:
        launches = []
        quit_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            state_path = Path(tmp) / "update_handoff.json"
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                quit_callback=lambda: quit_calls.append(True),
                handoff_path_provider=lambda: state_path,
                handoff_command_builder=lambda path: ["python", "main.py", UPDATE_HANDOFF_ARG, str(path)],
                handoff_ack_waiter=lambda _path: False,
                worktree_runner=_primary_worktree_runner(tmp),
            )

            self.assertFalse(updater.launch_update())

        snapshot = updater.get_status_snapshot()
        self.assertEqual(
            launches,
            [
                (
                    ["python", "main.py", UPDATE_HANDOFF_ARG, str(state_path)],
                    {"cwd": str(Path(tmp).resolve())},
                )
            ],
        )
        self.assertEqual(quit_calls, [True])
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("acknowledge", snapshot["last_error"])
        self.assertEqual(snapshot["progress"]["failed_step"], "업데이트 프로세스 확인")

    def test_launch_update_fails_closed_in_codex_temporary_worktree(self) -> None:
        launches = []
        handoff_writes = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = os.path.join(tmp, ".codex", "worktrees", "9f9a", "windows-supporter")
            os.makedirs(repo_root)
            with open(os.path.join(repo_root, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../../../main/.git/worktrees/9f9a\n")
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=repo_root,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                handoff_path_provider=lambda: handoff_writes.append(True)
                or os.path.join(tmp, "update_handoff.json"),
            )

            self.assertFalse(updater.launch_update())

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("main worktree", snapshot["last_error"])
        self.assertEqual(handoff_writes, [])
        self.assertEqual(launches, [])

    def test_launch_update_fails_closed_in_non_primary_linked_worktree(self) -> None:
        launches = []
        handoff_writes = []
        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "main", "windows-supporter")
            linked = os.path.join(tmp, "linked", "windows-supporter")
            os.makedirs(primary)
            os.makedirs(linked)
            with open(os.path.join(linked, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../.git/worktrees/feature\n")
            porcelain = "\n".join(
                [
                    f"worktree {primary.replace(os.sep, '/')}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                    f"worktree {linked.replace(os.sep, '/')}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/codex/feature",
                    "",
                ]
            )
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=linked,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                handoff_path_provider=lambda: handoff_writes.append(True)
                or os.path.join(tmp, "update_handoff.json"),
                worktree_runner=lambda _argv, **_kwargs: types.SimpleNamespace(
                    returncode=0,
                    stdout=porcelain,
                    stderr="",
                ),
            )

            self.assertFalse(updater.launch_update())

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("main worktree", snapshot["last_error"])
        self.assertEqual(handoff_writes, [])
        self.assertEqual(launches, [])

    def test_launch_update_reports_handoff_prepare_failure(self) -> None:
        launches = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command, **kwargs: launches.append((command, dict(kwargs))) or object(),
                handoff_path_provider=lambda: (_ for _ in ()).throw(RuntimeError("denied")),
                worktree_runner=_primary_worktree_runner(tmp),
            )

            self.assertFalse(updater.launch_update())

            snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("failed to prepare update handoff", snapshot["last_error"])
        self.assertEqual(launches, [])


if __name__ == "__main__":
    unittest.main()
