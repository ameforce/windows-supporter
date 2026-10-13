import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import build_workspace


class BuildWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.owner = SimpleNamespace(
            path=Path("fresh scratch space"),
            release=Mock(return_value=True),
            retain=Mock(),
        )
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def run_owned(self, runner=None, **kwargs):
        if runner is None:
            runner = Mock(return_value=SimpleNamespace(returncode=0))
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return build_workspace.run_workspace(
                self.owner, ["payload.exe", "argument with spaces"],
                kind="build", runner=runner, **kwargs,
            )

    def test_success_releases_once_after_synchronous_child_returns(self):
        events = []
        self.owner.release.side_effect = lambda: events.append("release") or True

        def child(command, **kwargs):
            self.owner.release.assert_not_called()
            events.append("child completed export and consumers")
            return SimpleNamespace(returncode=0)

        self.assertEqual(self.run_owned(child), 0)
        self.assertEqual(events, ["child completed export and consumers", "release"])
        self.owner.release.assert_called_once_with()
        self.owner.retain.assert_not_called()
        self.assertIn(str(self.owner.path), self.stdout.getvalue())
        self.assertIn("cleanup unconfirmed", self.stderr.getvalue())

    def test_child_failure_preserves_status_and_retains(self):
        self.assertEqual(self.run_owned(Mock(return_value=SimpleNamespace(returncode=37))), 37)
        self.owner.retain.assert_called_once_with()
        self.owner.release.assert_not_called()

    def test_negative_child_status_is_preserved(self):
        self.assertEqual(self.run_owned(Mock(return_value=SimpleNamespace(returncode=-9))), -9)
        self.owner.release.assert_not_called()

    def test_launch_failure_is_retained_and_propagated(self):
        with self.assertRaisesRegex(OSError, "launch failure"):
            self.run_owned(Mock(side_effect=OSError("launch failure")))
        self.owner.retain.assert_called_once_with()
        self.owner.release.assert_not_called()

    def test_interrupt_does_not_infer_process_completion(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_owned(Mock(side_effect=KeyboardInterrupt))
        self.owner.retain.assert_called_once_with()
        self.owner.release.assert_not_called()

    def test_unexpected_runner_exception_is_preserved(self):
        with self.assertRaisesRegex(RuntimeError, "uncertain child"):
            self.run_owned(Mock(side_effect=RuntimeError("uncertain child")))
        self.owner.retain.assert_called_once_with()
        self.owner.release.assert_not_called()

    def test_retention_exception_does_not_mask_original_exception(self):
        self.owner.retain.side_effect = RuntimeError("handle failure")
        with self.assertRaisesRegex(OSError, "original"):
            self.run_owned(Mock(side_effect=OSError("original")))
        self.assertIn("no cleanup requested", self.stderr.getvalue())

    def test_keep_retains_even_when_child_succeeds(self):
        self.assertEqual(self.run_owned(keep=True), 0)
        self.owner.retain.assert_called_once_with()
        self.owner.release.assert_not_called()

    def test_handoff_false_keeps_success_without_retry(self):
        self.owner.release.return_value = False
        self.assertEqual(self.run_owned(), 0)
        self.owner.release.assert_called_once_with()
        self.owner.retain.assert_called_once_with()
        self.assertIn("handoff incomplete", self.stderr.getvalue())

    def test_handoff_exceptions_keep_success_without_retry(self):
        for failure in (OSError("pipe"), KeyboardInterrupt(), SystemExit(4)):
            with self.subTest(failure=type(failure).__name__):
                self.owner.release.reset_mock()
                self.owner.retain.reset_mock()
                self.owner.release.side_effect = failure
                self.assertEqual(self.run_owned(), 0)
                self.owner.release.assert_called_once_with()
                self.owner.retain.assert_called_once_with()

    def test_parent_scope_and_marker_do_not_bypass_fresh_owner(self):
        inherited = {
            "WINDOWS_SUPPORTER_BUILD_WORKSPACE": "blocked existing scope",
            "WINDOWS_SUPPORTER_BUILD_WORKSPACE_OWNER": "stale marker",
            "OTHER": "preserved",
        }
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(self.run_owned(runner, environ=inherited), 0)
        command = runner.call_args.args[0]
        env = runner.call_args.kwargs["env"]
        self.assertEqual(command, ["payload.exe", "argument with spaces"])
        self.assertEqual(env["WINDOWS_SUPPORTER_BUILD_WORKSPACE"], str(self.owner.path))
        self.assertEqual(inherited["WINDOWS_SUPPORTER_BUILD_WORKSPACE"], "blocked existing scope")
        self.assertEqual(env["OTHER"], "preserved")
        self.assertIs(runner.call_args.kwargs["shell"], False)
        self.assertIs(runner.call_args.kwargs["check"], False)

    def test_unmanaged_fallback_retains_for_success_and_failure(self):
        for status in (0, 12):
            with self.subTest(status=status):
                self.owner.retain.reset_mock()
                self.assertEqual(self.run_owned(
                    Mock(return_value=SimpleNamespace(returncode=status)), managed=False,
                ), status)
                self.owner.retain.assert_called_once_with()
                self.owner.release.assert_not_called()
        self.assertIn("provider absent", self.stderr.getvalue())

    def test_absent_provider_allocates_new_temp_without_adopting_env(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)), \
                    patch.object(build_workspace.tempfile, "mkdtemp", return_value="new temp") as allocate, \
                    patch.dict(os.environ, {"WINDOWS_SUPPORTER_INSTALLER_WORKSPACE": "blocked"}):
                owner, managed = build_workspace.allocate_workspace("installer")
            self.assertFalse(managed)
            self.assertEqual(owner.path, Path("new temp"))
            allocate.assert_called_once_with(prefix="windows-supporter-installer-")

    def test_explicit_import_ignores_public_module_collision(self):
        with tempfile.TemporaryDirectory() as home:
            package = Path(home) / ".codex/tools/codex-disposable-workspace/disposable_workspace"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("from .producer import allocate\n", encoding="utf-8")
            (package / "producer.py").write_text(
                "def allocate():\n    return 'known package owner'\n", encoding="utf-8",
            )
            collision = SimpleNamespace(allocate=Mock(side_effect=AssertionError("wrong import")))
            before = set(sys.modules)
            try:
                with patch.object(Path, "home", return_value=Path(home)), \
                        patch.dict(sys.modules, {"disposable_workspace": collision}):
                    owner, managed = build_workspace.allocate_workspace("build")
                self.assertEqual(owner, "known package owner")
                self.assertTrue(managed)
                collision.allocate.assert_not_called()
            finally:
                for name in set(sys.modules) - before:
                    if name.startswith("_windows_supporter_disposable_"):
                        del sys.modules[name]

    def test_present_broken_provider_never_falls_back_or_launches(self):
        for source in (None, "raise ImportError('broken import')\n",
                       "def allocate():\n    raise OSError('failed allocation')\n"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as home:
                package = Path(home) / ".codex/tools/codex-disposable-workspace/disposable_workspace"
                package.mkdir(parents=True)
                if source is not None:
                    (package / "__init__.py").write_text(source, encoding="utf-8")
                with patch.object(Path, "home", return_value=Path(home)), \
                        patch.object(build_workspace.tempfile, "mkdtemp") as fallback, \
                        patch.object(build_workspace.subprocess, "run") as runner, \
                        contextlib.redirect_stderr(self.stderr):
                    self.assertEqual(build_workspace.main(["--kind", "build", "--", "payload"]), 1)
                fallback.assert_not_called()
                runner.assert_not_called()

    def test_main_reports_launch_failure_and_interrupt(self):
        for failure, status in ((OSError("launch"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(status=status):
                self.owner.retain.reset_mock()
                with patch.object(build_workspace, "allocate_workspace", return_value=(self.owner, True)), \
                        patch.object(build_workspace.subprocess, "run", side_effect=failure), \
                        contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
                    self.assertEqual(build_workspace.main(["--kind", "build", "--", "payload"]), status)
                self.owner.retain.assert_called_once_with()
                self.owner.release.assert_not_called()

    def test_cli_requires_boundary_and_nonempty_command(self):
        for argv in (["--kind", "build"], ["--kind", "build", "--"],
                     ["--kind", "other", "--", "payload"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(self.stderr):
                with self.assertRaises(SystemExit) as error:
                    build_workspace.parse_args(argv)
                self.assertEqual(error.exception.code, 2)

    def test_cli_preserves_child_flags_and_spaces(self):
        args = build_workspace.parse_args([
            "--kind", "installer", "--keep", "--", "child path", "--kind", "build", "a b",
        ])
        self.assertEqual(args.kind, "installer")
        self.assertTrue(args.keep)
        self.assertEqual(args.command, ["child path", "--kind", "build", "a b"])

    def test_actual_python_cli_preserves_windows_arguments_and_child_env(self):
        scratch = "new scratch 한글 space"
        arguments = ["space argument", 'quote"argument', "amp&percent%", "한글", "trailing\\"]
        bootstrap = (
            "from tools import build_workspace as b; from pathlib import Path; "
            "from types import SimpleNamespace; "
            f"b.allocate_workspace=lambda kind:(SimpleNamespace(path=Path({scratch!r}),retain=lambda:None),True); "
            "raise SystemExit(b.main())"
        )
        child = (
            "import json,os,sys; print(json.dumps({'args':sys.argv[1:],"
            "'scratch':os.environ['WINDOWS_SUPPORTER_INSTALLER_WORKSPACE']},ensure_ascii=False)); "
            "raise SystemExit(23)"
        )
        env = dict(os.environ, PYTHONUTF8="1", WINDOWS_SUPPORTER_INSTALLER_WORKSPACE="blocked old")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", bootstrap, "--kind", "installer", "--keep", "--",
             sys.executable, "-X", "utf8", "-c", child, *arguments],
            cwd=Path(__file__).resolve().parents[2], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20,
        )
        stdout = result.stdout.decode("utf-8", errors="strict")
        stderr = result.stderr.decode("utf-8", errors="strict")
        self.assertEqual(result.returncode, 23, stderr)
        self.assertEqual(json.loads(stdout.splitlines()[-1]), {"args": arguments, "scratch": scratch})
        self.assertIn("Scratch retained", stderr)


if __name__ == "__main__":
    unittest.main()
