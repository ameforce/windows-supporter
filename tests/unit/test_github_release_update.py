from __future__ import annotations

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.github_release_update import (
    GITHUB_LATEST_RELEASE_URL,
    GitHubReleaseClient,
    GitHubReleaseUpdateError,
    ReleaseCandidate,
)
from src.utils.update_monitor import (
    UPDATE_RELEASE_MODE,
    UpdateCandidate,
    read_update_handoff_state,
    run_release_update_handoff,
    build_update_handoff_payload,
)
import src.utils.update_monitor as update_monitor_module


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._position = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._position >= len(self._body):
            return b""
        if size is None or size < 0:
            size = len(self._body) - self._position
        start = self._position
        self._position += int(size)
        return self._body[start : self._position]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def __call__(self, request, *, timeout: float):
        del timeout
        url = str(request.full_url)
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        return FakeResponse(self.responses[url])


def release_payload(
    *,
    installer_url: str,
    installer_name: str,
    digest: str | None,
    sidecar_url: str | None = None,
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
        "tag_name": "v0.22.0",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/ameforce/windows-supporter/releases/tag/v0.22.0",
        "body": "installer update",
        "assets": assets,
    }


class GitHubReleaseUpdateUnitTest(unittest.TestCase):
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

            def download_installer(self, candidate, destination_dir):
                self.calls.append((candidate, Path(destination_dir)))
                destination = Path(destination_dir)
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / candidate.installer_name
                path.write_bytes(b"verified installer")
                return path

        class FakeProcess:
            pid = 4123

            def wait(self, *, timeout: float) -> int:
                self.timeout = timeout
                return 0

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
            installer_calls = []
            target_calls = []
            progress_instances = []

            def installer_launcher(argv, **kwargs):
                installer_calls.append((list(argv), dict(kwargs)))
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
                    process_exists=lambda _pid: False,
                    progress_ui_factory=lambda **kwargs: progress_instances.append(
                        FakeProgressUi(**kwargs)
                    )
                    or progress_instances[-1],
                )

            state = read_update_handoff_state(state_path)

        self.assertEqual(rc, 0)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(installer_calls), 1)
        self.assertIn("/VERYSILENT", installer_calls[0][0])
        self.assertIn('/DIR="' + str(root) + '"', installer_calls[0][0])
        self.assertEqual(target_calls[0][0], [str(executable)])
        self.assertEqual(progress_instances[0].snapshots[-1]["step_key"], "complete")
        self.assertTrue(progress_instances[0].closed)
