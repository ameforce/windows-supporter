from __future__ import annotations

import codecs
import os
import subprocess
import tempfile
import types
import unittest

from src.utils.update_monitor import (
    DEFAULT_CLEAN_ALLOWLIST,
    GIT_COMMAND_TIMEOUT_SECONDS,
    UpdateCandidate,
    UpdatePromptSession,
    WindowsSupporterUpdater,
    build_allowed_clean_command,
    build_allowed_clean_probe_command,
    build_detached_helper_command,
    build_remote_tag_check_command,
    build_stash_command,
    classify_switch_main_error,
    is_git_checkout_root,
    parse_remote_tag_refs,
    parse_semver_tag,
    render_update_helper_script,
    resolve_current_tag,
    select_update_candidate,
    write_detached_helper_script,
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

    def test_switch_main_error_classifies_linked_worktree_ownership(self) -> None:
        stderr = "fatal: 'main' is already checked out at 'C:/workspace/windows-supporter'"

        self.assertEqual(
            classify_switch_main_error(stderr),
            "main_checked_out_in_other_worktree",
        )
        self.assertEqual(classify_switch_main_error("fatal: something else"), "unknown")

    def test_detached_helper_command_uses_temp_helper_and_repo_argument(self) -> None:
        command = build_detached_helper_command(
            r"C:\repo\windows-supporter",
            helper_path=r"C:\Users\me\AppData\Local\windows-supporter\update_windows_supporter.ps1",
        )

        self.assertEqual(
            command,
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                r"C:\Users\me\AppData\Local\windows-supporter\update_windows_supporter.ps1",
                "-RepoRoot",
                r"C:\repo\windows-supporter",
            ],
        )
        self.assertNotIn("-m", command)

    def test_detached_helper_command_preserves_paths_with_spaces(self) -> None:
        command = build_detached_helper_command(
            r"C:\repo with spaces\windows-supporter",
            helper_path=r"C:\Users\me\AppData\Local\windows supporter\update helper.ps1",
        )

        self.assertEqual(
            command,
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                r"C:\Users\me\AppData\Local\windows supporter\update helper.ps1",
                "-RepoRoot",
                r"C:\repo with spaces\windows-supporter",
            ],
        )

    def test_detached_helper_command_executes_paths_with_cmd_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ws upd & test ") as tmp:
            helper_dir = os.path.join(tmp, "helper & dir")
            repo_dir = os.path.join(tmp, "repo & dir")
            os.makedirs(helper_dir)
            os.makedirs(repo_dir)
            helper_path = os.path.join(helper_dir, "update helper & script.ps1")
            result_path = os.path.join(tmp, "result.txt")
            escaped_result_path = result_path.replace("'", "''")
            with open(helper_path, "w", encoding="utf-8") as fp:
                fp.write(
                    "param([string]$RepoRoot)\n"
                    f"Set-Content -LiteralPath '{escaped_result_path}' -Value $RepoRoot\n"
                )

            result = subprocess.run(
                build_detached_helper_command(repo_dir, helper_path=helper_path),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with open(result_path, "r", encoding="utf-8") as fp:
                self.assertEqual(fp.read().strip(), repo_dir)

    def test_write_detached_helper_script_uses_utf8_bom_for_windows_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper_path = write_detached_helper_script(os.path.join(tmp, "helper.ps1"))

            with open(helper_path, "rb") as fp:
                contents = fp.read()

            self.assertTrue(contents.startswith(codecs.BOM_UTF8))

    def test_rendered_update_helper_accepts_repo_root_and_runs_safe_flow(self) -> None:
        script = render_update_helper_script()

        self.assertIn("param(", script)
        self.assertIn("[string]$RepoRoot", script)
        self.assertIn("git stash push --include-untracked", script)
        self.assertIn('git clean -fdX -- build/ dist/ "*.spec" "*.egg-info/"', script)
        self.assertIn("git fetch --tags origin", script)
        self.assertIn("git merge --ff-only origin/main", script)
        self.assertIn("cmd /c build.bat", script)
        self.assertIn("WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1", script)

    def test_build_bat_supports_updater_owned_relaunch(self) -> None:
        with open("build.bat", "r", encoding="utf-8") as fp:
            script = fp.read()

        self.assertIn("WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN", script)
        self.assertIn("SKIP_POST_BUILD_RUN", script)
        self.assertIn("Skipping post-build launch", script)

    def test_rendered_update_helper_exposes_visible_progress_window_contract(self) -> None:
        script = render_update_helper_script()

        self.assertIn("Add-Type -AssemblyName System.Windows.Forms", script)
        self.assertIn("Add-Type -AssemblyName System.Drawing", script)
        self.assertIn("System.Windows.Forms.Form", script)
        self.assertIn("Windows Supporter 업데이트", script)
        self.assertIn("취소는 지원하지 않습니다", script)
        self.assertIn("Open-UpdateLog", script)
        self.assertIn("Update-ProgressStage", script)
        self.assertIn("Checking Git checkout", script)
        self.assertIn("Running build.bat", script)
        self.assertIn("Start-Process", script)
        self.assertIn("-PassThru", script)
        self.assertIn("WaitForExit(250)", script)
        self.assertIn("[System.Windows.Forms.Application]::DoEvents()", script)

    def test_rendered_update_helper_exposes_failure_actions_and_retry_contract(self) -> None:
        script = render_update_helper_script()

        self.assertIn("Show-UpdateFailure", script)
        self.assertIn("업데이트 실패", script)
        self.assertIn("로그 열기", script)
        self.assertIn("재시도", script)
        self.assertIn("닫기", script)
        self.assertIn("Restart-UpdateHelper", script)
        self.assertIn("-ExecutionPolicy", script)
        self.assertIn("-RepoRoot", script)

    def test_rendered_update_helper_relaunches_built_executable_from_repo_root(self) -> None:
        script = render_update_helper_script()

        self.assertIn("Start-UpdatedWindowsSupporter", script)
        self.assertIn('Join-Path -Path $RepoRoot -ChildPath "windows-supporter.exe"', script)
        self.assertIn("Test-Path -LiteralPath $exePath", script)
        self.assertIn("Start-Process", script)
        self.assertIn("-WorkingDirectory $RepoRoot", script)
        self.assertIn("Relaunched Windows Supporter exited immediately", script)
        self.assertIn("$process.WaitForExit(250)", script)
        self.assertIn("Update completed", script)

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
        updater._is_worktree_dirty = lambda: True
        updater._show_warning = lambda title, message: warnings.append((title, message))
        updater.launch_update = lambda: launches.append(True) or True

        updater._handle_check_result(
            UpdateCandidate(tag="v0.5.7", version=(0, 5, 7)),
            dirty=False,
            error="",
            manual=True,
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("자동 stash", warnings[0][1])
        self.assertEqual(launches, [True])

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

    def test_git_output_is_noninteractive_and_bounded(self) -> None:
        class FakeSubprocess:
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

    def test_launch_update_writes_detached_helper_and_passes_repo_root(self) -> None:
        launches = []
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
            helper_path = write_detached_helper_script(os.path.join(tmp, "helper.ps1"))
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command: launches.append(command) or object(),
                helper_writer=lambda: helper_path,
                worktree_runner=lambda _argv, **_kwargs: types.SimpleNamespace(
                    returncode=0,
                    stdout=porcelain,
                    stderr="",
                ),
            )

            self.assertTrue(updater.launch_update())

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "updating")
        self.assertEqual(
            launches,
            [
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(helper_path),
                    "-RepoRoot",
                    str(updater._repo_root),
                ]
            ],
        )

    def test_launch_update_fails_closed_in_codex_temporary_worktree(self) -> None:
        launches = []
        helper_writes = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = os.path.join(tmp, ".codex", "worktrees", "9f9a", "windows-supporter")
            os.makedirs(repo_root)
            with open(os.path.join(repo_root, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: ../../../main/.git/worktrees/9f9a\n")
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=repo_root,
                popen=lambda command: launches.append(command) or object(),
                helper_writer=lambda: helper_writes.append(True) or os.path.join(tmp, "helper.ps1"),
            )

            self.assertFalse(updater.launch_update())

        snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("main worktree", snapshot["last_error"])
        self.assertEqual(helper_writes, [])
        self.assertEqual(launches, [])

    def test_launch_update_fails_closed_in_non_primary_linked_worktree(self) -> None:
        launches = []
        helper_writes = []
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
                popen=lambda command: launches.append(command) or object(),
                helper_writer=lambda: helper_writes.append(True) or os.path.join(tmp, "helper.ps1"),
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
        self.assertEqual(helper_writes, [])
        self.assertEqual(launches, [])

    def test_launch_update_reports_helper_prepare_failure(self) -> None:
        launches = []
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as fp:
                fp.write("gitdir: .git\n")
            updater = WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=tmp,
                popen=lambda command: launches.append(command) or object(),
                helper_writer=lambda: (_ for _ in ()).throw(RuntimeError("denied")),
                worktree_runner=_primary_worktree_runner(tmp),
            )

            self.assertFalse(updater.launch_update())

            snapshot = updater.get_status_snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("failed to prepare update helper", snapshot["last_error"])
        self.assertEqual(launches, [])


if __name__ == "__main__":
    unittest.main()
