from __future__ import annotations

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.github_release_update import (
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_PROGRESS_MIN_INTERVAL_SECONDS,
    GITHUB_LATEST_RELEASE_URL,
    GITHUB_RELEASES_URL,
    GITHUB_TAGS_URL,
    GitHubReleaseClient,
    GitHubReleaseUpdateError,
    ReleaseCandidate,
)
from src.utils.update_monitor import (
    UPDATE_RELEASE_MODE,
    UPDATE_SKIP_AUTO_UPDATE_TAG_ENV,
    UpdateCandidate,
    read_update_handoff_state,
    run_release_update_handoff,
    build_update_handoff_payload,
)
import src.utils.update_monitor as update_monitor_module


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        final_url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._position = 0
        self._final_url = final_url
        self.headers = dict(headers or {})
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(int(size))
        if self._position >= len(self._body):
            return b""
        if size is None or size < 0:
            size = len(self._body) - self._position
        start = self._position
        self._position += int(size)
        return self._body[start : self._position]

    def close(self) -> None:
        self.closed = True

    def geturl(self) -> str:
        return self._final_url


class FakeOpener:
    def __init__(self, responses: dict[str, bytes], *, include_content_length: bool = True) -> None:
        self.responses = dict(responses)
        self.include_content_length = bool(include_content_length)
        self.calls: list[str] = []
        self.last_response: FakeResponse | None = None

    def __call__(self, request, *, timeout: float):
        del timeout
        url = str(request.full_url)
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        headers = (
            {"Content-Length": str(len(self.responses[url]))}
            if self.include_content_length
            else {}
        )
        self.last_response = FakeResponse(self.responses[url], headers=headers)
        return self.last_response


def release_payload(
    *,
    installer_url: str,
    installer_name: str,
    digest: str | None,
    sidecar_url: str | None = None,
    tag: str = "v0.22.0",
) -> dict:
    asset = {
        "name": installer_name,
        "browser_download_url": installer_url,
    }
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    assets = [asset]
    if sidecar_url is not None:
        assets.append(
            {
                "name": f"{installer_name}.sha256",
                "browser_download_url": sidecar_url,
            }
        )
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/ameforce/windows-supporter/releases/tag/{tag}",
        "body": "installer update",
        "assets": assets,
    }


class GitHubReleaseUpdateUnitTest(unittest.TestCase):
    def test_installer_builder_binds_asset_name_to_tag_and_embedded_runtime_version(self) -> None:
        script = Path("tools/build_installer.ps1").read_text(encoding="utf-8")

        self.assertIn(
            'Invoke-GitText @("describe", "--tags", "--exact-match", "HEAD")',
            script,
        )
        self.assertIn("$sourceVersionInfo = (Get-Item -LiteralPath $sourceExe).VersionInfo", script)
        self.assertIn("Source executable $field", script)
        self.assertIn("Source executable Comments", script)
        self.assertIn("BootstrapCompilerPath", script)
        self.assertIn("installer_bootstrap.c", script)
        self.assertIn('"/FWindowsSupporter-v$Version-Core"', script)
        self.assertIn("[IO.FileMode]::CreateNew", script)
        self.assertIn('INSTALLER_FORMAT=legacy-compatible-bootstrap', script)
        installer_definition = Path("installer/windows-supporter.iss").read_text(encoding="utf-8")
        self.assertIn("CloseApplications=force", installer_definition)
        self.assertIn("Flags: ignoreversion", installer_definition)
        self.assertNotIn("restartreplace", installer_definition)
        bootstrap = Path("installer/installer_bootstrap.c").read_text(encoding="utf-8")
        self.assertIn("PAYLOAD_MAGIC", bootstrap)
        self.assertIn("CommandLineToArgvW", bootstrap)
        self.assertIn("normalize_legacy_value", bootstrap)

    def test_github_release_asset_redirect_host_is_allowed(self) -> None:
        final_url = "https://release-assets.githubusercontent.com/github-production-release-asset/test"

        def opener(_request, *, timeout: float):
            del timeout
            return FakeResponse(b"", final_url=final_url)

        response = GitHubReleaseClient(opener=opener)._open(
            "https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe"
        )

        response.close()

    def test_latest_release_uses_github_digest_and_downloads_atomically(self) -> None:
        installer_name = "WindowsSupporter-v0.22.0-Setup.exe"
        installer_url = f"https://objects.githubusercontent.com/{installer_name}"
        installer_bytes = b"fake installer bytes"
        digest = hashlib.sha256(installer_bytes).hexdigest()
        opener = FakeOpener(
            {
                GITHUB_LATEST_RELEASE_URL: json.dumps(
                    release_payload(
                        installer_url=installer_url,
                        installer_name=installer_name,
                        digest=digest,
                    )
                ).encode("utf-8"),
                installer_url: installer_bytes,
            }
        )

        client = GitHubReleaseClient(opener=opener)
        candidate = client.fetch_latest((0, 21, 1))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.tag, "v0.22.0")
        self.assertEqual(candidate.installer_sha256, digest.upper())
        with tempfile.TemporaryDirectory() as tmp:
            downloaded = client.download_installer(candidate, tmp)
            self.assertEqual(downloaded.read_bytes(), installer_bytes)
            self.assertFalse(list(Path(tmp).glob("*.download")))

    def test_download_uses_large_reads_and_throttles_progress_publication(self) -> None:
        installer_name = "WindowsSupporter-v0.22.0-Setup.exe"
        installer_url = f"https://objects.githubusercontent.com/{installer_name}"
        installer_bytes = b"x" * (DOWNLOAD_CHUNK_SIZE * 2 + 123)
        digest = hashlib.sha256(installer_bytes).hexdigest()
        candidate = types.SimpleNamespace(
            installer_name=installer_name,
            installer_url=installer_url,
            installer_sha256=digest,
        )
        opener = FakeOpener({installer_url: installer_bytes})
        progress: list[tuple[int, int | None]] = []
        timestamps = iter((0.0, 0.05, DOWNLOAD_PROGRESS_MIN_INTERVAL_SECONDS + 0.05, 0.3))

        with tempfile.TemporaryDirectory() as tmp:
            downloaded = GitHubReleaseClient(opener=opener).download_installer(
                candidate,
                tmp,
                progress_callback=lambda downloaded_bytes, total_bytes: progress.append(
                    (downloaded_bytes, total_bytes)
                ),
                progress_monotonic=lambda: next(timestamps),
            )
            self.assertEqual(downloaded.read_bytes(), installer_bytes)

        self.assertIsNotNone(opener.last_response)
        assert opener.last_response is not None
        self.assertEqual(
            opener.last_response.read_sizes[:3],
            [DOWNLOAD_CHUNK_SIZE, DOWNLOAD_CHUNK_SIZE, DOWNLOAD_CHUNK_SIZE],
        )
        self.assertEqual(progress[0], (0, len(installer_bytes)))
        self.assertEqual(progress[-1], (len(installer_bytes), len(installer_bytes)))
        self.assertEqual(len(progress), 3)
        self.assertLess(len(progress), len(opener.last_response.read_sizes))

    def test_download_infers_total_on_final_callback_when_header_is_missing(self) -> None:
        installer_name = "WindowsSupporter-v0.22.0-Setup.exe"
        installer_url = f"https://objects.githubusercontent.com/{installer_name}"
        installer_bytes = b"x" * (DOWNLOAD_CHUNK_SIZE + 17)
        digest = hashlib.sha256(installer_bytes).hexdigest()
        candidate = types.SimpleNamespace(
            installer_name=installer_name,
            installer_url=installer_url,
            installer_sha256=digest,
        )
        opener = FakeOpener(
            {installer_url: installer_bytes},
            include_content_length=False,
        )
        progress: list[tuple[int, int | None]] = []

        with tempfile.TemporaryDirectory() as tmp:
            GitHubReleaseClient(opener=opener).download_installer(
                candidate,
                tmp,
                progress_callback=lambda downloaded_bytes, total_bytes: progress.append(
                    (downloaded_bytes, total_bytes)
                ),
            )

        self.assertEqual(progress[0], (0, None))
        self.assertEqual(progress[-1], (len(installer_bytes), len(installer_bytes)))

    def test_latest_release_falls_back_to_sha256_sidecar(self) -> None:
        installer_name = "WindowsSupporter-v0.22.0-Setup.exe"
        installer_url = f"https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/{installer_name}"
        sidecar_url = f"https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/{installer_name}.sha256"
        digest = "a" * 64
        opener = FakeOpener(
            {
                GITHUB_LATEST_RELEASE_URL: json.dumps(
                    release_payload(
                        installer_url=installer_url,
                        installer_name=installer_name,
                        digest=None,
                        sidecar_url=sidecar_url,
                    )
                ).encode("utf-8"),
                sidecar_url: f"{digest}  {installer_name}\n".encode("ascii"),
            }
        )

        candidate = GitHubReleaseClient(opener=opener).fetch_latest((0, 21, 1))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.installer_sha256, digest.upper())
        self.assertEqual(opener.calls[-1], sidecar_url)

    def test_semver_newer_release_is_found_when_latest_endpoint_is_stale(self) -> None:
        installer_name = "WindowsSupporter-v0.22.6-Setup.exe"
        installer_url = f"https://objects.githubusercontent.com/{installer_name}"
        digest = "a" * 64
        release = release_payload(
            installer_url=installer_url,
            installer_name=installer_name,
            digest=digest,
            tag="v0.22.6",
        )
        opener = FakeOpener(
            {
                GITHUB_LATEST_RELEASE_URL: json.dumps(
                    {"tag_name": "v0.22.5", "draft": False, "prerelease": False}
                ).encode("utf-8"),
                GITHUB_RELEASES_URL: json.dumps([release]).encode("utf-8"),
            }
        )

        candidate = GitHubReleaseClient(opener=opener).fetch_latest((0, 22, 5))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.tag, "v0.22.6")
        self.assertNotIn(GITHUB_TAGS_URL, opener.calls)

    def test_newer_public_tag_without_release_is_not_reported_as_current(self) -> None:
        opener = FakeOpener(
            {
                GITHUB_LATEST_RELEASE_URL: json.dumps(
                    {"tag_name": "v0.22.5", "draft": False, "prerelease": False}
                ).encode("utf-8"),
                GITHUB_RELEASES_URL: b"[]",
                GITHUB_TAGS_URL: json.dumps([{"name": "v0.22.6"}]).encode("utf-8"),
            }
        )

        with self.assertRaisesRegex(GitHubReleaseUpdateError, "v0.22.6.*최신으로 표시하지 않습니다"):
            GitHubReleaseClient(opener=opener).fetch_latest((0, 22, 5))

    def test_no_newer_public_tag_still_reports_current(self) -> None:
        opener = FakeOpener(
            {
                GITHUB_LATEST_RELEASE_URL: json.dumps(
                    {"tag_name": "v0.22.5", "draft": False, "prerelease": False}
                ).encode("utf-8"),
                GITHUB_RELEASES_URL: b"[]",
                GITHUB_TAGS_URL: json.dumps([{"name": "v0.22.5"}]).encode("utf-8"),
            }
        )

        self.assertIsNone(GitHubReleaseClient(opener=opener).fetch_latest((0, 22, 5)))

    def test_download_rejects_hash_mismatch_and_leaves_no_installer(self) -> None:
        installer_name = "WindowsSupporter-v0.22.0-Setup.exe"
        installer_url = f"https://objects.githubusercontent.com/{installer_name}"
        candidate = types.SimpleNamespace(
            installer_name=installer_name,
            installer_url=installer_url,
            installer_sha256="b" * 64,
        )
        client = GitHubReleaseClient(
            opener=FakeOpener({installer_url: b"not-the-expected-installer"})
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GitHubReleaseUpdateError):
                client.download_installer(candidate, tmp)
            self.assertFalse((Path(tmp) / installer_name).exists())
            self.assertFalse(list(Path(tmp).glob("*.download")))

    def test_release_candidate_rejects_untrusted_asset_host(self) -> None:
        payload = release_payload(
            installer_url="https://example.invalid/WindowsSupporter-v0.22.0-Setup.exe",
            installer_name="WindowsSupporter-v0.22.0-Setup.exe",
            digest="a" * 64,
        )
        opener = FakeOpener(
            {GITHUB_LATEST_RELEASE_URL: json.dumps(payload).encode("utf-8")}
        )

        with self.assertRaises(GitHubReleaseUpdateError):
            GitHubReleaseClient(opener=opener).fetch_latest((0, 21, 1))


class ReleaseUpdateHandoffUnitTest(unittest.TestCase):
    def test_updater_uses_release_source_without_git_checkout_metadata(self) -> None:
        class FakeReleaseClient:
            def fetch_latest(self, current_version):
                self.current_version = current_version
                return ReleaseCandidate(
                    tag="v0.22.0",
                    version=(0, 22, 0),
                    installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                    installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                    installer_sha256="a" * 64,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            root.mkdir()
            (root / "windows-supporter.exe").write_bytes(b"installed")
            release_client = FakeReleaseClient()
            updater = update_monitor_module.WindowsSupporterUpdater(
                root=object(),
                event_queue=types.SimpleNamespace(put=lambda callback: callback()),
                repo_root=root,
                app_version_provider=lambda: types.SimpleNamespace(source_tag="v0.21.1"),
                release_client=release_client,
                settings_path_provider=lambda: Path(tmp) / "settings.json",
            )

            candidate, working_tree, error = updater._collect_update_candidate()

        self.assertEqual(error, "")
        self.assertEqual(candidate.tag, "v0.22.0")
        self.assertEqual(candidate.installer_name, "WindowsSupporter-v0.22.0-Setup.exe")
        self.assertEqual(working_tree, update_monitor_module.UpdateWorkingTreeState())
        self.assertEqual(release_client.current_version, (0, 21, 1))

    def test_release_handoff_downloads_verifies_installs_and_relaunches(self) -> None:
        class FakeReleaseClient:
            def __init__(self, destination: Path) -> None:
                self.destination = destination
                self.calls = []

            def download_installer(self, candidate, destination_dir, *, progress_callback=None):
                self.calls.append((candidate, Path(destination_dir)))
                destination = Path(destination_dir)
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / candidate.installer_name
                installer_bytes = b"verified installer"
                path.write_bytes(installer_bytes)
                if progress_callback is not None:
                    progress_callback(0, len(installer_bytes))
                    progress_callback(len(installer_bytes) // 2, len(installer_bytes))
                    progress_callback(len(installer_bytes), len(installer_bytes))
                return path

        class FakeProcess:
            pid = 4123

            def wait(self, *, timeout: float) -> int:
                self.timeout = timeout
                return 0

        class FakeRuntimeProcessController:
            def __init__(self) -> None:
                self.find_calls = []
                self.terminate_calls = []
                self._remaining = [9001, 9002]

            def find_exact(self, executable):
                self.find_calls.append(Path(executable))
                current = list(self._remaining)
                if current:
                    self._remaining = []
                return current

            def terminate_tree(self, pids, timeout_seconds):
                self.terminate_calls.append((list(pids), timeout_seconds))
                return list(pids)

        class FakeProgressUi:
            def __init__(self, **_kwargs) -> None:
                self.snapshots = []
                self.closed = False

            def show(self, snapshot) -> None:
                self.snapshots.append(dict(snapshot))

            def set_snapshot(self, snapshot) -> None:
                self.snapshots.append(dict(snapshot))

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed folder"
            root.mkdir()
            executable = root / "windows-supporter.exe"
            executable.write_bytes(b"old executable")
            candidate = UpdateCandidate(
                tag="v0.22.0",
                version=(0, 22, 0),
                installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                installer_sha256="a" * 64,
            )
            state_path = Path(tmp) / "update_handoff.json"
            payload = build_update_handoff_payload(
                repo_root=root,
                target_tag=candidate.tag,
                mode=UPDATE_RELEASE_MODE,
                candidate=candidate,
                source_pid=0,
                install_dir=root,
                log_path=Path(tmp) / "update.log",
            )
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeReleaseClient(Path(tmp))
            runtime_controller = FakeRuntimeProcessController()
            installer_calls = []
            target_calls = []
            progress_instances = []

            def installer_launcher(argv, **kwargs):
                installer_calls.append((list(argv), dict(kwargs)))
                executable.write_bytes(b"new v0.22.0 executable")
                return FakeProcess()

            def target_launcher(argv, **kwargs):
                target_calls.append((list(argv), dict(kwargs)))
                return FakeProcess()

            with patch.object(
                update_monitor_module,
                "get_update_state_dir",
                return_value=Path(tmp) / "state",
            ):
                rc = run_release_update_handoff(
                    state_path,
                    release_client=client,
                    installer_launcher=installer_launcher,
                    target_launcher=target_launcher,
                    runtime_process_controller=runtime_controller,
                    runtime_readiness_waiter=lambda *_args, **_kwargs: {
                        "pid": 4123,
                        "heartbeat_samples": 3,
                    },
                    artifact_metadata_reader=lambda _path: {
                        "file_version": "0.22.0.0",
                        "product_version": "0.22.0.0",
                        "comments": "v0.22.0 (release-commit)",
                    },
                    process_exists=lambda _pid: False,
                    progress_ui_factory=lambda **kwargs: progress_instances.append(
                        FakeProgressUi(**kwargs)
                    )
                    or progress_instances[-1],
                )

            state = read_update_handoff_state(state_path)

        self.assertEqual(rc, 0)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["runtime_shutdown"]["status"], "verified")
        self.assertEqual(runtime_controller.terminate_calls[0][0], [9001, 9002])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(installer_calls), 1)
        self.assertIn("/VERYSILENT", installer_calls[0][0])
        self.assertIn("/FORCECLOSEAPPLICATIONS", installer_calls[0][0])
        self.assertIn("/NORESTARTAPPLICATIONS", installer_calls[0][0])
        self.assertIn("/DIR=" + str(root), installer_calls[0][0])
        self.assertNotIn('/DIR="' + str(root) + '"', installer_calls[0][0])
        self.assertTrue(any(item.startswith("/LOG=") for item in installer_calls[0][0]))
        self.assertEqual(
            state["installer_log_path"],
            str(Path(tmp) / "state" / "release-installer-0.22.0.log"),
        )
        self.assertEqual(target_calls[0][0], [str(executable)])
        self.assertEqual(progress_instances[0].snapshots[0]["step_key"], "handoff_start")
        self.assertEqual(progress_instances[0].snapshots[0]["percent"], 0)
        all_percents = [int(snapshot["percent"]) for snapshot in progress_instances[0].snapshots]
        self.assertEqual(all_percents, sorted(all_percents))
        download_snapshots = [
            snapshot
            for snapshot in progress_instances[0].snapshots
            if snapshot.get("step_key") == "release_download"
        ]
        self.assertGreaterEqual(len(download_snapshots), 4)
        download_percents = [int(snapshot["percent"]) for snapshot in download_snapshots]
        self.assertEqual(download_percents[0], 18)
        self.assertEqual(download_percents[-1], 62)
        self.assertEqual(download_percents, sorted(download_percents))
        self.assertTrue(any("50%" in str(snapshot["detail"]) for snapshot in download_snapshots))
        stage_keys = [str(snapshot["step_key"]) for snapshot in progress_instances[0].snapshots]
        self.assertIn("release_source_exit", stage_keys)
        self.assertIn("release_backup", stage_keys)
        self.assertIn("release_verify", stage_keys)
        self.assertIn("release_install_prepare", stage_keys)
        self.assertIn("release_validate", stage_keys)
        self.assertIn("release_relaunch", stage_keys)
        self.assertEqual(progress_instances[0].snapshots[-1]["step_key"], "complete")
        self.assertTrue(progress_instances[0].closed)

    def test_release_handoff_rejects_installer_that_leaves_old_runtime(self) -> None:
        class FakeReleaseClient:
            def download_installer(self, candidate, destination_dir, *, progress_callback=None):
                del progress_callback
                destination = Path(destination_dir)
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / candidate.installer_name
                path.write_bytes(b"verified installer")
                return path

        class FakeProcess:
            def wait(self, *, timeout: float) -> int:
                del timeout
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            root.mkdir()
            executable = root / "windows-supporter.exe"
            executable.write_bytes(b"old executable")
            candidate = UpdateCandidate(
                tag="v0.22.0",
                version=(0, 22, 0),
                installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                installer_sha256="a" * 64,
            )
            state_path = Path(tmp) / "update_handoff.json"
            state_path.write_text(
                json.dumps(
                    build_update_handoff_payload(
                        repo_root=root,
                        target_tag=candidate.tag,
                        mode=UPDATE_RELEASE_MODE,
                        candidate=candidate,
                        source_pid=0,
                        install_dir=root,
                    )
                ),
                encoding="utf-8",
            )

            with patch.object(
                update_monitor_module,
                "get_update_state_dir",
                return_value=Path(tmp) / "state",
            ):
                rc = run_release_update_handoff(
                    state_path,
                    release_client=FakeReleaseClient(),
                    installer_launcher=lambda *_args, **_kwargs: FakeProcess(),
                    target_launcher=lambda *_args, **_kwargs: FakeProcess(),
                    artifact_metadata_reader=lambda _path: {
                        "file_version": "0.22.0.0",
                        "product_version": "0.22.0.0",
                        "comments": "v0.22.0 (release-commit)",
                    },
                    process_exists=lambda _pid: False,
                    progress_ui_factory=lambda **_kwargs: None,
                )
            state = read_update_handoff_state(state_path)
            restored_bytes = executable.read_bytes()

        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("교체하지 않았습니다", state["error"])
        self.assertEqual(restored_bytes, b"old executable")

    def test_release_handoff_does_not_launch_installer_when_exact_runtime_survives(self) -> None:
        class FakeReleaseClient:
            def download_installer(self, *_args, **_kwargs):
                raise AssertionError("installer download must not start")

        class StuckRuntimeController:
            def find_exact(self, _executable):
                return [777]

            def terminate_tree(self, _pids, _timeout_seconds):
                return [777]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            root.mkdir()
            executable = root / "windows-supporter.exe"
            executable.write_bytes(b"old executable")
            candidate = UpdateCandidate(
                tag="v0.22.0",
                version=(0, 22, 0),
                installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                installer_sha256="a" * 64,
            )
            state_path = Path(tmp) / "update_handoff.json"
            state_path.write_text(
                json.dumps(
                    build_update_handoff_payload(
                        repo_root=root,
                        target_tag=candidate.tag,
                        mode=UPDATE_RELEASE_MODE,
                        candidate=candidate,
                        source_pid=0,
                        install_dir=root,
                    )
                ),
                encoding="utf-8",
            )
            installer_calls = []

            with patch.object(
                update_monitor_module,
                "get_update_state_dir",
                return_value=Path(tmp) / "state",
            ):
                rc = run_release_update_handoff(
                    state_path,
                    release_client=FakeReleaseClient(),
                    runtime_process_controller=StuckRuntimeController(),
                    installer_launcher=lambda *args, **kwargs: installer_calls.append(
                        (args, kwargs)
                    ),
                    progress_ui_factory=lambda **_kwargs: None,
                )
            state = read_update_handoff_state(state_path)

        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("installer를 실행하지 않았습니다", state["error"])
        self.assertEqual(installer_calls, [])

    def test_release_handoff_does_not_install_while_source_process_is_alive(self) -> None:
        class FakeReleaseClient:
            def download_installer(self, candidate, destination_dir, *, progress_callback=None):
                del progress_callback
                raise AssertionError("installer download must not start")

        class FakeProcess:
            def wait(self, *, timeout: float) -> int:
                del timeout
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            root.mkdir()
            executable = root / "windows-supporter.exe"
            executable.write_bytes(b"old executable")
            candidate = UpdateCandidate(
                tag="v0.22.0",
                version=(0, 22, 0),
                installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                installer_sha256="a" * 64,
            )
            state_path = Path(tmp) / "update_handoff.json"
            state_path.write_text(
                json.dumps(
                    build_update_handoff_payload(
                        repo_root=root,
                        target_tag=candidate.tag,
                        mode=UPDATE_RELEASE_MODE,
                        candidate=candidate,
                        source_pid=4123,
                        install_dir=root,
                    )
                ),
                encoding="utf-8",
            )
            installer_calls = []
            target_calls = []
            clock_values = iter((0.0, 26.0))

            with patch.object(
                update_monitor_module,
                "get_update_state_dir",
                return_value=Path(tmp) / "state",
            ):
                rc = run_release_update_handoff(
                    state_path,
                    release_client=FakeReleaseClient(),
                    installer_launcher=lambda *args, **kwargs: installer_calls.append(
                        (args, kwargs)
                    )
                    or FakeProcess(),
                    target_launcher=lambda *args, **kwargs: target_calls.append(
                        (args, kwargs)
                    )
                    or FakeProcess(),
                    process_exists=lambda pid: int(pid) == 4123,
                    sleep=lambda _seconds: None,
                    monotonic=lambda: next(clock_values),
                    progress_ui_factory=lambda **_kwargs: None,
                )
            state = read_update_handoff_state(state_path)
            restored_bytes = executable.read_bytes()

        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("프로세스가 종료되지 않아", state["error"])
        self.assertEqual(installer_calls, [])
        self.assertEqual(target_calls, [])
        self.assertEqual(restored_bytes, b"old executable")

    def test_release_handoff_rejects_installed_runtime_with_wrong_version(self) -> None:
        class FakeReleaseClient:
            def download_installer(self, candidate, destination_dir, *, progress_callback=None):
                del progress_callback
                destination = Path(destination_dir)
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / candidate.installer_name
                path.write_bytes(b"verified installer")
                return path

        class FakeProcess:
            def wait(self, *, timeout: float) -> int:
                del timeout
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            root.mkdir()
            executable = root / "windows-supporter.exe"
            executable.write_bytes(b"old executable")
            candidate = UpdateCandidate(
                tag="v0.22.0",
                version=(0, 22, 0),
                installer_name="WindowsSupporter-v0.22.0-Setup.exe",
                installer_url="https://github.com/ameforce/windows-supporter/releases/download/v0.22.0/WindowsSupporter-v0.22.0-Setup.exe",
                installer_sha256="a" * 64,
            )
            state_path = Path(tmp) / "update_handoff.json"
            state_path.write_text(
                json.dumps(
                    build_update_handoff_payload(
                        repo_root=root,
                        target_tag=candidate.tag,
                        mode=UPDATE_RELEASE_MODE,
                        candidate=candidate,
                        source_pid=0,
                        install_dir=root,
                    )
                ),
                encoding="utf-8",
            )

            def installer_launcher(*_args, **_kwargs):
                executable.write_bytes(b"wrong v0.21.1 executable")
                return FakeProcess()

            recovery_target_calls = []
            with patch.object(
                update_monitor_module,
                "get_update_state_dir",
                return_value=Path(tmp) / "state",
            ):
                rc = run_release_update_handoff(
                    state_path,
                    release_client=FakeReleaseClient(),
                    installer_launcher=installer_launcher,
                    target_launcher=lambda *args, **kwargs: recovery_target_calls.append(
                        (args, kwargs)
                    )
                    or FakeProcess(),
                    artifact_metadata_reader=lambda _path: {
                        "file_version": "0.21.1.0",
                        "product_version": "0.21.1.0",
                        "comments": "v0.21.1 (old-commit)",
                    },
                    process_exists=lambda _pid: False,
                    progress_ui_factory=lambda **_kwargs: None,
                )
            state = read_update_handoff_state(state_path)
            restored_bytes = executable.read_bytes()

        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("버전이 요청한 Release와 다릅니다", state["error"])
        self.assertEqual(restored_bytes, b"old executable")
        self.assertEqual(
            recovery_target_calls[0][1]["env"][UPDATE_SKIP_AUTO_UPDATE_TAG_ENV],
            candidate.tag,
        )
