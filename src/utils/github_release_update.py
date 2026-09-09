from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


GITHUB_REPOSITORY = "ameforce/windows-supporter"
GITHUB_LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
GITHUB_RELEASE_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
SHA256_RE = re.compile(r"(?i)(?:sha256:)?(?P<digest>[0-9a-f]{64})")
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 20


class GitHubReleaseUpdateError(RuntimeError):
    """Raised when a GitHub Release cannot be trusted as an update source."""


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    tag: str
    version: tuple[int, int, int]
    installer_name: str
    installer_url: str
    installer_sha256: str
    release_url: str = ""
    release_body: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "version": list(self.version),
            "installer_name": self.installer_name,
            "installer_url": self.installer_url,
            "installer_sha256": self.installer_sha256,
            "release_url": self.release_url,
            "release_body": self.release_body,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReleaseCandidate":
        if not isinstance(payload, Mapping):
            raise GitHubReleaseUpdateError("release candidate payload is not an object")
        tag = normalize_release_tag(payload.get("tag"))
        version = parse_release_version(tag)
        if version is None:
            raise GitHubReleaseUpdateError("release candidate tag is not semantic")
        installer_name = str(payload.get("installer_name") or "").strip()
        installer_url = str(payload.get("installer_url") or "").strip()
        digest = normalize_sha256(payload.get("installer_sha256"))
        if (
            not installer_name
            or installer_name != _asset_name_for_version(version)
            or not installer_url
            or digest is None
        ):
            raise GitHubReleaseUpdateError("release candidate is missing installer metadata")
        _validate_download_url(installer_url)
        return cls(
            tag=tag,
            version=version,
            installer_name=installer_name,
            installer_url=installer_url,
            installer_sha256=digest,
            release_url=str(payload.get("release_url") or "").strip(),
            release_body=str(payload.get("release_body") or ""),
        )


def normalize_release_tag(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.lower().startswith("v"):
        text = f"v{text}"
    return text


def parse_release_version(tag: Any) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(tag or "").strip())
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in range(1, 4))


def normalize_sha256(value: Any) -> str | None:
    match = SHA256_RE.search(str(value or "").strip())
    return match.group("digest").upper() if match else None


def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in GITHUB_RELEASE_HOSTS:
        raise GitHubReleaseUpdateError("release asset URL is not an allowed GitHub HTTPS URL")


def _asset_name_for_version(version: tuple[int, int, int]) -> str:
    return f"WindowsSupporter-v{version[0]}.{version[1]}.{version[2]}-Setup.exe"


def _find_asset(assets: list[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    for asset in assets:
        if str(asset.get("name") or "").strip() == name:
            return asset
    return None


class GitHubReleaseClient:
    def __init__(
        self,
        *,
        api_url: str = GITHUB_LATEST_RELEASE_URL,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        user_agent: str = "Windows-Supporter-Updater",
    ) -> None:
        self._api_url = str(api_url or GITHUB_LATEST_RELEASE_URL).strip()
        self._opener = opener
        self._timeout = max(1.0, float(timeout))
        self._user_agent = str(user_agent or "Windows-Supporter-Updater")

    def _open(self, url: str) -> Any:
        _validate_download_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": self._user_agent,
            },
        )
        try:
            response = self._opener(request, timeout=self._timeout)
            final_url = ""
            get_url = getattr(response, "geturl", None)
            if callable(get_url):
                final_url = str(get_url() or "").strip()
            if final_url:
                _validate_download_url(final_url)
            return response
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise GitHubReleaseUpdateError(f"GitHub Release 요청 실패: {exc}") from exc

    def _read_json(self, url: str) -> dict[str, Any]:
        response = self._open(url)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReleaseUpdateError("GitHub Release 응답 JSON을 해석할 수 없습니다.") from exc
        if not isinstance(payload, dict):
            raise GitHubReleaseUpdateError("GitHub Release 응답 형식이 올바르지 않습니다.")
        return payload

    def _read_text(self, url: str) -> str:
        response = self._open(url)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise GitHubReleaseUpdateError("installer hash 파일 인코딩을 해석할 수 없습니다.") from exc

    def fetch_latest(self, current_version: tuple[int, int, int]) -> ReleaseCandidate | None:
        current = tuple(int(part) for part in current_version)
        payload = self._read_json(self._api_url)
        if bool(payload.get("draft")) or bool(payload.get("prerelease")):
            return None

        tag = normalize_release_tag(payload.get("tag_name"))
        version = parse_release_version(tag)
        if version is None or version <= current:
            return None
        assets_value = payload.get("assets")
        assets = [item for item in assets_value if isinstance(item, Mapping)] if isinstance(assets_value, list) else []
        installer_name = _asset_name_for_version(version)
        installer = _find_asset(assets, installer_name)
        if installer is None:
            raise GitHubReleaseUpdateError(
                f"GitHub Release {tag}에 {installer_name} asset이 없습니다."
            )

        installer_url = str(installer.get("browser_download_url") or "").strip()
        _validate_download_url(installer_url)
        digest = normalize_sha256(installer.get("digest") or installer.get("sha256"))
        if digest is None:
            sidecar_names = (
                f"{installer_name}.sha256",
                f"{installer_name}.SHA256",
            )
            sidecar = next(
                (item for item in assets if str(item.get("name") or "") in sidecar_names),
                None,
            )
            if sidecar is None:
                raise GitHubReleaseUpdateError(
                    f"GitHub Release {tag} installer의 SHA-256 asset이 없습니다."
                )
            sidecar_url = str(sidecar.get("browser_download_url") or "").strip()
            digest = normalize_sha256(self._read_text(sidecar_url))
        if digest is None:
            raise GitHubReleaseUpdateError(f"GitHub Release {tag} installer SHA-256이 올바르지 않습니다.")

        return ReleaseCandidate(
            tag=tag,
            version=version,
            installer_name=installer_name,
            installer_url=installer_url,
            installer_sha256=digest,
            release_url=str(payload.get("html_url") or "").strip(),
            release_body=str(payload.get("body") or ""),
        )

    def download_installer(
        self,
        candidate: ReleaseCandidate,
        destination_dir: str | os.PathLike[str],
    ) -> Path:
        _validate_download_url(candidate.installer_url)
        destination = Path(destination_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        final_path = destination / candidate.installer_name
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{candidate.installer_name}.",
            suffix=".download",
            dir=str(destination),
        )
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        total = 0
        try:
            response = self._open(candidate.installer_url)
            try:
                with os.fdopen(fd, "wb") as output:
                    fd = -1
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_INSTALLER_BYTES:
                            raise GitHubReleaseUpdateError("installer 크기가 허용 한도를 초과했습니다.")
                        digest.update(chunk)
                        output.write(chunk)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            actual = digest.hexdigest().upper()
            if actual != candidate.installer_sha256.upper():
                raise GitHubReleaseUpdateError(
                    f"installer SHA-256 불일치: expected={candidate.installer_sha256} actual={actual}"
                )
            os.replace(temp_path, final_path)
            return final_path
        except GitHubReleaseUpdateError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise GitHubReleaseUpdateError(f"installer 다운로드 실패: {exc}") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
