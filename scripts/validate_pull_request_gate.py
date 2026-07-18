from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ATTESTATION_START = "<!-- windows-supporter-pr-attestation:v2"
ATTESTATION_END = "-->"
GITHUB_API_VERSION = "2026-03-10"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._+/@:-]{3,100}$")
REQUIRED_ATTESTATION_KEYS = (
    "policy_version",
    "repository_id",
    "repository_full_name",
    "pull_request_number",
    "base_ref",
    "base_sha",
    "head_ref",
    "head_sha",
    "reviewer_source",
    "finding_low",
    "finding_medium",
    "finding_high",
    "finding_critical",
    "ui_evidence",
    "generated_at",
    "expires_at",
    "review_evidence_digest",
)
DIGEST_INPUT_KEYS = tuple(key for key in REQUIRED_ATTESTATION_KEYS if key != "review_evidence_digest")


class PolicyError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise PolicyError(f"JSON object required: {path}")
    return value


def review_evidence_digest(values: Mapping[str, str]) -> str:
    payload = {key: str(values[key]) for key in DIGEST_INPUT_KEYS}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_attestation(body: str) -> dict[str, str]:
    start = body.find(ATTESTATION_START)
    if start < 0:
        raise PolicyError("PR body에 windows-supporter evidence가 없습니다.")
    end = body.find(ATTESTATION_END, start)
    if end < 0:
        raise PolicyError("PR evidence 종료 표식이 없습니다.")
    if body.find(ATTESTATION_START, start + len(ATTESTATION_START)) >= 0:
        raise PolicyError("PR evidence는 정확히 하나만 허용합니다.")

    values: dict[str, str] = {}
    block = body[start + len(ATTESTATION_START) : end]
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise PolicyError(f"잘못된 evidence 행: {line}")
        normalized_key = key.strip()
        if normalized_key in values:
            raise PolicyError(f"중복된 evidence 키: {normalized_key}")
        values[normalized_key] = value.strip()

    missing = [key for key in REQUIRED_ATTESTATION_KEYS if key not in values]
    extra = sorted(set(values) - set(REQUIRED_ATTESTATION_KEYS))
    if missing:
        raise PolicyError(f"누락된 evidence 키: {', '.join(missing)}")
    if extra:
        raise PolicyError(f"허용되지 않은 evidence 키: {', '.join(extra)}")
    return values


def parse_utc_timestamp(value: str, field_name: str) -> dt.datetime:
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError(f"{field_name}은 ISO-8601 UTC 형식이어야 합니다.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != dt.timedelta(0):
        raise PolicyError(f"{field_name}은 UTC 시각이어야 합니다.")
    return timestamp


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        normalized_pattern = str(pattern).replace("\\", "/")
        if normalized_pattern.endswith("/") and normalized.startswith(normalized_pattern):
            return True
        if fnmatch.fnmatchcase(normalized, normalized_pattern):
            return True
    return False


def _github_json(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "windows-supporter-pr-policy-gate",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _run_gh_json(arguments: Sequence[str]) -> Any:
    result = subprocess.run(
        ["gh", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise PolicyError(result.stderr.strip() or f"gh {' '.join(arguments)} failed")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PolicyError("gh 응답이 JSON이 아닙니다.") from exc


def _run_gh_json_with_server_time(arguments: Sequence[str]) -> tuple[Any, dt.datetime]:
    result = subprocess.run(
        ["gh", *arguments, "--include"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise PolicyError(result.stderr.strip() or f"gh {' '.join(arguments)} failed")
    parts = re.split(r"\r?\n\r?\n", result.stdout.strip())
    if len(parts) < 2:
        raise PolicyError("GitHub 응답에 HTTP header가 없습니다.")
    body = parts[-1]
    headers = parts[-2]
    date_value = None
    for line in headers.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "date":
            date_value = value.strip()
            break
    if not date_value:
        raise PolicyError("GitHub 응답에 Date header가 없습니다.")
    try:
        server_time = email.utils.parsedate_to_datetime(date_value)
        value = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyError("GitHub Date 또는 JSON 응답을 해석할 수 없습니다.") from exc
    if server_time.tzinfo is None:
        raise PolicyError("GitHub Date header에 timezone이 없습니다.")
    return value, server_time.astimezone(dt.timezone.utc)


def _event_repository_and_number(event: Mapping[str, Any]) -> tuple[str, int]:
    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if not isinstance(pull_request, Mapping) or not isinstance(repository, Mapping):
        raise PolicyError("pull_request/repository event 객체가 필요합니다.")
    number = pull_request.get("number") or event.get("number")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not isinstance(number, int):
        raise PolicyError("repository full_name과 PR number가 필요합니다.")
    return full_name, number


def fetch_current_pull_request(
    event: Mapping[str, Any],
    *,
    token: str | None,
    api_url: str,
) -> dict[str, Any]:
    if not token:
        raise PolicyError("현재 PR 조회에 GITHUB_TOKEN이 필요합니다.")
    full_name, number = _event_repository_and_number(event)
    value = _github_json(f"{api_url.rstrip('/')}/repos/{full_name}/pulls/{number}", token)
    if not isinstance(value, dict):
        raise PolicyError("GitHub PR 응답이 객체가 아닙니다.")
    return value


def pull_request_fingerprint(pull_request: Mapping[str, Any]) -> str:
    base = pull_request.get("base")
    head = pull_request.get("head")
    if not isinstance(base, Mapping) or not isinstance(head, Mapping):
        raise PolicyError("PR base/head 정보가 없습니다.")
    labels = sorted(
        str(item.get("name"))
        for item in pull_request.get("labels", [])
        if isinstance(item, Mapping) and item.get("name")
    )
    snapshot = {
        "number": pull_request.get("number"),
        "draft": bool(pull_request.get("draft")),
        "body": str(pull_request.get("body") or ""),
        "labels": labels,
        "base_ref": base.get("ref"),
        "base_sha": base.get("sha"),
        "base_repo_id": base.get("repo", {}).get("id") if isinstance(base.get("repo"), Mapping) else None,
        "base_repo_full_name": (
            base.get("repo", {}).get("full_name") if isinstance(base.get("repo"), Mapping) else None
        ),
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "head_repo_id": head.get("repo", {}).get("id") if isinstance(head.get("repo"), Mapping) else None,
        "head_repo_full_name": (
            head.get("repo", {}).get("full_name") if isinstance(head.get("repo"), Mapping) else None
        ),
    }
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def assert_current_pull_request(event: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    event_pull_request = event.get("pull_request")
    if not isinstance(event_pull_request, Mapping):
        raise PolicyError("pull_request event가 필요합니다.")
    if pull_request_fingerprint(event_pull_request) != pull_request_fingerprint(current):
        raise PolicyError("PR이 policy run 도중 변경되었습니다. 최신 상태에서 다시 실행해야 합니다.")


def _append_changed_path(paths: list[str], seen: set[str], value: Any) -> None:
    if isinstance(value, str) and value not in seen:
        paths.append(value)
        seen.add(value)


def fetch_changed_files(
    event: Mapping[str, Any],
    *,
    token: str | None,
    api_url: str,
) -> list[str]:
    injected = event.get("_changed_files")
    if isinstance(injected, list) and all(isinstance(item, str) for item in injected):
        if len(injected) >= 3000:
            raise PolicyError("GitHub API 한도인 3000개 이상 변경 파일은 검증을 중단합니다.")
        return list(dict.fromkeys(injected))

    if not token:
        raise PolicyError("변경 파일 조회에 GITHUB_TOKEN이 필요합니다.")
    full_name, number = _event_repository_and_number(event)
    event_pull_request = event.get("pull_request")
    expected_count = (
        event_pull_request.get("changed_files")
        if isinstance(event_pull_request, Mapping)
        else None
    )
    if isinstance(expected_count, int) and expected_count >= 3000:
        raise PolicyError("GitHub API 한도인 3000개 이상 변경 파일은 검증을 중단합니다.")
    files: list[str] = []
    seen: set[str] = set()
    item_count = 0
    current_names: set[str] = set()
    for page in range(1, 31):
        url = f"{api_url.rstrip('/')}/repos/{full_name}/pulls/{number}/files?per_page=100&page={page}"
        page_items = _github_json(url, token)
        if not isinstance(page_items, list):
            raise PolicyError("GitHub changed-files 응답이 배열이 아닙니다.")
        for item in page_items:
            if not isinstance(item, Mapping):
                raise PolicyError("GitHub changed-files 항목이 객체가 아닙니다.")
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename or filename in current_names:
                raise PolicyError("GitHub changed-files filename이 비어 있거나 중복되었습니다.")
            current_names.add(filename)
            item_count += 1
            _append_changed_path(files, seen, filename)
            _append_changed_path(files, seen, item.get("previous_filename"))
        if len(page_items) < 100:
            if isinstance(expected_count, int) and item_count != expected_count:
                raise PolicyError(
                    f"GitHub changed-files count가 PR metadata와 다릅니다: {item_count} != {expected_count}"
                )
            return files
    raise PolicyError("GitHub API 한도인 3000개 이상 변경 파일은 검증을 중단합니다.")


def fetch_live_pull_request(repository: str, pr_number: int) -> dict[str, Any]:
    value = _run_gh_json(
        [
            "api",
            f"repos/{repository}/pulls/{pr_number}",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        ]
    )
    if not isinstance(value, dict):
        raise PolicyError("GitHub PR 응답이 객체가 아닙니다.")
    return value


def fetch_live_pull_request_with_server_time(
    repository: str,
    pr_number: int,
) -> tuple[dict[str, Any], dt.datetime]:
    value, server_time = _run_gh_json_with_server_time(
        [
            "api",
            f"repos/{repository}/pulls/{pr_number}",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        ]
    )
    if not isinstance(value, dict):
        raise PolicyError("GitHub PR 응답이 객체가 아닙니다.")
    return value, server_time


def fetch_live_changed_files(repository: str, pr_number: int, changed_count: Any) -> list[str]:
    if not isinstance(changed_count, int) or changed_count < 0:
        raise PolicyError("GitHub changed_files count가 유효하지 않습니다.")
    if changed_count >= 3000:
        raise PolicyError("GitHub API 한도인 3000개 이상 변경 파일은 검증을 중단합니다.")

    files: list[str] = []
    seen: set[str] = set()
    item_count = 0
    current_names: set[str] = set()
    for page in range(1, 31):
        value = _run_gh_json(
            [
                "api",
                f"repos/{repository}/pulls/{pr_number}/files?per_page=100&page={page}",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            ]
        )
        if not isinstance(value, list):
            raise PolicyError("GitHub changed-files 응답이 배열이 아닙니다.")
        for item in value:
            if not isinstance(item, Mapping):
                raise PolicyError("GitHub changed-files 항목이 객체가 아닙니다.")
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename or filename in current_names:
                raise PolicyError("GitHub changed-files filename이 비어 있거나 중복되었습니다.")
            current_names.add(filename)
            item_count += 1
            _append_changed_path(files, seen, filename)
            _append_changed_path(files, seen, item.get("previous_filename"))
        if len(value) < 100:
            if item_count != changed_count:
                raise PolicyError(
                    f"GitHub changed-files count가 PR metadata와 다릅니다: {item_count} != {changed_count}"
                )
            return files
    raise PolicyError("GitHub API 한도인 3000개 이상 변경 파일은 검증을 중단합니다.")


def build_live_event(pull_request: Mapping[str, Any]) -> dict[str, Any]:
    base = pull_request.get("base")
    if not isinstance(base, Mapping) or not isinstance(base.get("repo"), Mapping):
        raise PolicyError("현재 PR의 base repository 정보가 없습니다.")
    repository = base["repo"]
    return {
        "number": pull_request.get("number"),
        "repository": {
            "id": repository.get("id"),
            "full_name": repository.get("full_name"),
        },
        "pull_request": dict(pull_request),
    }


def _remote_file_bytes(repository: str, revision: str, path: str) -> bytes:
    value = _run_gh_json(
        [
            "api",
            f"repos/{repository}/contents/{path}?ref={revision}",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        ]
    )
    if not isinstance(value, Mapping) or value.get("encoding") != "base64":
        raise PolicyError(f"trusted base의 {path} 내용을 읽을 수 없습니다.")
    content = value.get("content")
    if not isinstance(content, str):
        raise PolicyError(f"trusted base의 {path} content가 없습니다.")
    try:
        return base64.b64decode(content, validate=False)
    except (ValueError, TypeError) as exc:
        raise PolicyError(f"trusted base의 {path} content가 base64가 아닙니다.") from exc


def _canonical_trusted_text_bytes(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n")


def assert_trusted_controller_source(
    *,
    repository: str,
    base_sha: str,
    config_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    expected_config = (repository_root / ".github/pr-gate/active-release.json").resolve()
    if config_path.resolve() != expected_config:
        raise PolicyError("merge controller config는 repository canonical path여야 합니다.")
    trusted_files = {
        "scripts/validate_pull_request_gate.py": Path(__file__).resolve(),
        ".github/pr-gate/active-release.json": expected_config,
    }
    for remote_path, local_path in trusted_files.items():
        local_bytes = _canonical_trusted_text_bytes(local_path.read_bytes())
        remote_bytes = _canonical_trusted_text_bytes(
            _remote_file_bytes(repository, base_sha, remote_path)
        )
        if local_bytes != remote_bytes:
            raise PolicyError(f"local {remote_path}가 trusted base SHA와 일치하지 않습니다.")


def validate_live_pull_request(
    *,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    config: Mapping[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    current_before = fetch_live_pull_request(repository, pr_number)
    if str(current_before.get("state", "")).lower() != "open" or current_before.get("merged_at"):
        raise PolicyError("merge controller는 open 상태의 미병합 PR만 처리합니다.")

    head = current_before.get("head")
    if not isinstance(head, Mapping) or str(head.get("sha", "")).lower() != expected_head_sha:
        raise PolicyError("현재 PR head SHA가 merge controller의 expected head SHA와 다릅니다.")

    changed_files = fetch_live_changed_files(
        repository,
        pr_number,
        current_before.get("changed_files"),
    )
    if now is None:
        current_after, validation_time = fetch_live_pull_request_with_server_time(
            repository,
            pr_number,
        )
    else:
        current_after = fetch_live_pull_request(repository, pr_number)
        validation_time = now
    if pull_request_fingerprint(current_before) != pull_request_fingerprint(current_after):
        raise PolicyError("PR metadata가 merge 직전 changed-files 조회 중 변경되었습니다.")

    summary = validate_event(
        build_live_event(current_after),
        config,
        changed_files,
        now=validation_time,
    )
    return {
        "pull_request": current_after,
        "summary": summary,
        "changed_files": changed_files,
    }


def merge_live_pull_request(args: argparse.Namespace) -> dict[str, Any]:
    config = load_json(args.config)
    validation = validate_live_pull_request(
        repository=args.repository,
        pr_number=args.pr_number,
        expected_head_sha=args.expected_head_sha,
        config=config,
    )
    validated_pull_request = validation["pull_request"]
    final_pull_request, github_server_time = fetch_live_pull_request_with_server_time(
        args.repository,
        args.pr_number,
    )
    if pull_request_fingerprint(validated_pull_request) != pull_request_fingerprint(final_pull_request):
        raise PolicyError("PR metadata가 policy 검증 후 merge 요청 전에 변경되었습니다.")

    final_summary = validate_event(
        build_live_event(final_pull_request),
        config,
        validation["changed_files"],
        now=github_server_time,
        minimum_remaining_seconds=int(config.get("merge_freshness_safety_margin_seconds", 300)),
    )
    final_base = final_pull_request.get("base")
    if not isinstance(final_base, Mapping):
        raise PolicyError("final PR base 정보가 없습니다.")
    if args.allow_bootstrap_local_source:
        if (
            config.get("bootstrap_local_source_allowed") is not True
            or str(final_base.get("sha", "")).lower() != str(config.get("source_main_sha", "")).lower()
        ):
            raise PolicyError("bootstrap local controller 예외 조건이 일치하지 않습니다.")
    else:
        assert_trusted_controller_source(
            repository=args.repository,
            base_sha=str(final_base.get("sha", "")).lower(),
            config_path=args.config,
        )

    try:
        merge_result = _run_gh_json(
            [
                "api",
                f"repos/{args.repository}/pulls/{args.pr_number}/merge",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                "--method",
                "PUT",
                "-f",
                f"sha={args.expected_head_sha}",
                "-f",
                "merge_method=merge",
            ]
        )
    except PolicyError:
        adopted = fetch_live_pull_request(args.repository, args.pr_number)
        adopted_head = adopted.get("head")
        if (
            not adopted.get("merged_at")
            or not isinstance(adopted_head, Mapping)
            or str(adopted_head.get("sha", "")).lower() != args.expected_head_sha
        ):
            raise
        merge_result = {"merged": True, "sha": adopted.get("merge_commit_sha")}
    if not isinstance(merge_result, Mapping) or merge_result.get("merged") is not True:
        message = merge_result.get("message") if isinstance(merge_result, Mapping) else None
        raise PolicyError(str(message or "GitHub이 PR merge를 거부했습니다."))
    merge_sha = str(merge_result.get("sha", "")).lower()
    if not SHA_PATTERN.fullmatch(merge_sha):
        raise PolicyError("GitHub merge 응답에 유효한 merge commit SHA가 없습니다.")

    return {
        **final_summary,
        "pull_request_number": args.pr_number,
        "merge_commit_sha": merge_sha,
        "merged": True,
    }


def validate_event(
    event: Mapping[str, Any],
    config: Mapping[str, Any],
    changed_files: Sequence[str],
    *,
    now: dt.datetime | None = None,
    minimum_remaining_seconds: int = 0,
) -> dict[str, Any]:
    if config.get("state") != "active":
        raise PolicyError("active release lane이 아닙니다.")
    if config.get("lane") not in {"hotfix", "release"}:
        raise PolicyError("lane은 hotfix 또는 release여야 합니다.")

    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if not isinstance(pull_request, Mapping) or not isinstance(repository, Mapping):
        raise PolicyError("pull_request event가 필요합니다.")
    if bool(pull_request.get("draft")):
        raise PolicyError("draft PR은 merge gate를 통과할 수 없습니다.")

    base = pull_request.get("base")
    head = pull_request.get("head")
    if not isinstance(base, Mapping) or not isinstance(head, Mapping):
        raise PolicyError("PR base/head 정보가 없습니다.")
    base_ref = str(base.get("ref", ""))
    base_sha = str(base.get("sha", "")).lower()
    head_ref = str(head.get("ref", ""))
    head_sha = str(head.get("sha", "")).lower()
    if base_ref != config.get("active_base"):
        raise PolicyError(f"PR base가 active release lane과 다릅니다: {base_ref}")
    if not SHA_PATTERN.fullmatch(base_sha) or not SHA_PATTERN.fullmatch(head_sha):
        raise PolicyError("base/head SHA는 40자리 소문자 Git SHA여야 합니다.")
    if not any(head_ref.startswith(prefix) for prefix in config.get("allowed_head_prefixes", [])):
        raise PolicyError(f"허용되지 않은 task branch 이름입니다: {head_ref}")

    repository_id = str(repository.get("id", ""))
    repository_full_name = str(repository.get("full_name", ""))
    if repository_id != str(config.get("repository_id", "")):
        raise PolicyError("repository ID가 정책과 일치하지 않습니다.")
    if repository_full_name != str(config.get("repository_full_name", "")):
        raise PolicyError("repository full_name이 정책과 일치하지 않습니다.")
    head_repo = head.get("repo")
    if (
        not isinstance(head_repo, Mapping)
        or head_repo.get("full_name") != repository_full_name
        or str(head_repo.get("id", "")) != repository_id
    ):
        raise PolicyError("fork PR은 이 release lane에서 허용하지 않습니다.")

    pr_number = pull_request.get("number") or event.get("number")
    if not isinstance(pr_number, int):
        raise PolicyError("PR number가 없습니다.")
    attestation = parse_attestation(str(pull_request.get("body") or ""))
    expected = {
        "policy_version": str(config.get("policy_version", "")),
        "repository_id": repository_id,
        "repository_full_name": repository_full_name,
        "pull_request_number": str(pr_number),
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "finding_low": "0",
        "finding_medium": "0",
        "finding_high": "0",
        "finding_critical": "0",
    }
    for key, expected_value in expected.items():
        if attestation[key] != expected_value:
            raise PolicyError(f"evidence {key}가 현재 PR과 일치하지 않습니다.")
    if not SOURCE_PATTERN.fullmatch(attestation["reviewer_source"]):
        raise PolicyError("reviewer_source 형식이 잘못되었습니다.")
    if attestation["review_evidence_digest"] != review_evidence_digest(attestation):
        raise PolicyError("review_evidence_digest가 canonical evidence와 일치하지 않습니다.")

    current_time = now or dt.datetime.now(dt.timezone.utc)
    generated_at = parse_utc_timestamp(attestation["generated_at"], "generated_at")
    expires_at = parse_utc_timestamp(attestation["expires_at"], "expires_at")
    max_age = dt.timedelta(hours=int(config.get("attestation_max_age_hours", 24)))
    if generated_at > current_time + dt.timedelta(minutes=5):
        raise PolicyError("generated_at이 현재 시각보다 미래입니다.")
    if expires_at <= generated_at or expires_at - generated_at > max_age:
        raise PolicyError("evidence 유효 기간이 정책 범위를 벗어났습니다.")
    if current_time >= expires_at:
        raise PolicyError("evidence가 만료되었습니다.")
    if minimum_remaining_seconds < 0:
        raise PolicyError("minimum_remaining_seconds는 음수일 수 없습니다.")
    remaining = expires_at - current_time
    if remaining < dt.timedelta(seconds=minimum_remaining_seconds):
        raise PolicyError(
            f"evidence 잔여 시간이 merge safety margin {minimum_remaining_seconds}초보다 짧습니다."
        )

    labels = {
        str(item.get("name"))
        for item in pull_request.get("labels", [])
        if isinstance(item, Mapping) and item.get("name")
    }
    protected = [
        path for path in changed_files if path_matches(path, config.get("protected_paths", []))
    ]
    if protected:
        policy_label = str(config.get("maintainer_policy_label", ""))
        policy_prefix = str(config.get("policy_head_prefix", "policy/"))
        if not head_ref.startswith(policy_prefix) or policy_label not in labels:
            raise PolicyError(
                f"PR gate 보호 파일 변경에는 {policy_prefix} branch와 {policy_label} label이 모두 필요합니다: "
                + ", ".join(protected)
            )

    ui_changed = any(path_matches(path, config.get("ui_path_patterns", [])) for path in changed_files)
    ui_evidence = attestation["ui_evidence"]
    if ui_changed and not HASH_PATTERN.fullmatch(ui_evidence):
        raise PolicyError("UI 변경에는 sha256:<64 hex> 형식의 ui_evidence가 필요합니다.")
    if not ui_changed and ui_evidence != "not-applicable":
        raise PolicyError("UI 변경이 없으면 ui_evidence는 not-applicable이어야 합니다.")

    return {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "changed_files": len(changed_files),
        "protected_files": protected,
        "ui_changed": ui_changed,
        "reviewer_source": attestation["reviewer_source"],
        "review_evidence_digest": attestation["review_evidence_digest"],
        "generated_at": attestation["generated_at"],
        "expires_at": attestation["expires_at"],
    }


def render_attestation(args: argparse.Namespace) -> str:
    generated_at = (
        parse_utc_timestamp(args.generated_at, "generated_at")
        if args.generated_at
        else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    )
    expires_at = (
        parse_utc_timestamp(args.expires_at, "expires_at")
        if args.expires_at
        else generated_at + dt.timedelta(hours=args.max_age_hours)
    )
    values = {
        "policy_version": args.policy_version,
        "repository_id": str(args.repository_id),
        "repository_full_name": args.repository,
        "pull_request_number": str(args.pr_number),
        "base_ref": args.base_ref,
        "base_sha": args.base_sha,
        "head_ref": args.head_ref,
        "head_sha": args.head_sha,
        "reviewer_source": args.reviewer_source,
        "finding_low": "0",
        "finding_medium": "0",
        "finding_high": "0",
        "finding_critical": "0",
        "ui_evidence": args.ui_evidence,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    values["review_evidence_digest"] = review_evidence_digest(values)
    return "\n".join(
        [ATTESTATION_START]
        + [f"{key}: {values[key]}" for key in REQUIRED_ATTESTATION_KEYS]
        + [ATTESTATION_END]
    )


def verify_merged(args: argparse.Namespace) -> dict[str, Any]:
    command = [
        "gh",
        "pr",
        "view",
        str(args.pr_number),
        "--repo",
        args.repository,
        "--json",
        "state,mergedAt,baseRefName,headRefOid,mergeCommit",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise PolicyError(result.stderr.strip() or "gh pr view 실패")
    payload = json.loads(result.stdout)
    merge_commit = payload.get("mergeCommit") or {}
    if payload.get("state") != "MERGED" or not payload.get("mergedAt"):
        raise PolicyError("PR이 merged 상태가 아닙니다.")
    if payload.get("baseRefName") != args.expected_base:
        raise PolicyError("merged PR base가 기대한 release lane과 다릅니다.")
    if payload.get("headRefOid") != args.expected_head_sha:
        raise PolicyError("merged PR head SHA가 검토한 SHA와 다릅니다.")
    if not SHA_PATTERN.fullmatch(str(merge_commit.get("oid", ""))):
        raise PolicyError("merge commit SHA가 없습니다.")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="windows-supporter PR operational gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-event")
    validate_parser.add_argument("--event", type=Path, required=True)
    validate_parser.add_argument("--config", type=Path, required=True)
    validate_parser.add_argument("--changed-files", type=Path)

    render_parser = subparsers.add_parser("render-attestation")
    render_parser.add_argument("--repository-id", type=int, required=True)
    render_parser.add_argument("--repository", required=True)
    render_parser.add_argument("--pr-number", type=int, required=True)
    render_parser.add_argument("--base-ref", required=True)
    render_parser.add_argument("--base-sha", required=True)
    render_parser.add_argument("--head-ref", required=True)
    render_parser.add_argument("--head-sha", required=True)
    render_parser.add_argument("--policy-version", default="1.1.0")
    render_parser.add_argument("--reviewer-source", required=True)
    render_parser.add_argument("--ui-evidence", default="not-applicable")
    render_parser.add_argument("--generated-at")
    render_parser.add_argument("--expires-at")
    render_parser.add_argument("--max-age-hours", type=int, default=24)

    merged_parser = subparsers.add_parser("verify-merged")
    merged_parser.add_argument("--repository", required=True)
    merged_parser.add_argument("--pr-number", type=int, required=True)
    merged_parser.add_argument("--expected-base", required=True)
    merged_parser.add_argument("--expected-head-sha", required=True)

    live_parser = subparsers.add_parser("validate-live")
    live_parser.add_argument("--repository", required=True)
    live_parser.add_argument("--pr-number", type=int, required=True)
    live_parser.add_argument("--expected-head-sha", required=True)
    live_parser.add_argument("--config", type=Path, required=True)

    merge_parser = subparsers.add_parser("merge-live")
    merge_parser.add_argument("--repository", required=True)
    merge_parser.add_argument("--pr-number", type=int, required=True)
    merge_parser.add_argument("--expected-head-sha", required=True)
    merge_parser.add_argument("--config", type=Path, required=True)
    merge_parser.add_argument("--allow-bootstrap-local-source", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-event":
            event = load_json(args.event)
            config = load_json(args.config)
            if args.changed_files:
                changed_files_value = json.loads(args.changed_files.read_text(encoding="utf-8"))
                if not isinstance(changed_files_value, list):
                    raise PolicyError("--changed-files JSON은 배열이어야 합니다.")
                changed_files = [str(item) for item in changed_files_value]
                effective_event = event
            else:
                token = os.environ.get("GITHUB_TOKEN")
                api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
                current_before = fetch_current_pull_request(event, token=token, api_url=api_url)
                assert_current_pull_request(event, current_before)
                changed_files = fetch_changed_files(event, token=token, api_url=api_url)
                current_after = fetch_current_pull_request(event, token=token, api_url=api_url)
                if pull_request_fingerprint(current_before) != pull_request_fingerprint(current_after):
                    raise PolicyError("PR metadata가 changed-files 조회 중 변경되었습니다.")
                assert_current_pull_request(event, current_after)
                effective_event = dict(event)
                effective_event["pull_request"] = current_after
            summary = validate_event(effective_event, config, changed_files)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        elif args.command == "render-attestation":
            if not SHA_PATTERN.fullmatch(args.base_sha) or not SHA_PATTERN.fullmatch(args.head_sha):
                raise PolicyError("base/head SHA는 40자리 소문자 Git SHA여야 합니다.")
            if not SOURCE_PATTERN.fullmatch(args.reviewer_source):
                raise PolicyError("reviewer_source 형식이 잘못되었습니다.")
            if args.max_age_hours < 1 or args.max_age_hours > 24:
                raise PolicyError("max-age-hours는 1~24 범위여야 합니다.")
            print(render_attestation(args))
        elif args.command == "verify-merged":
            print(json.dumps(verify_merged(args), ensure_ascii=False, sort_keys=True))
        elif args.command == "validate-live":
            if not SHA_PATTERN.fullmatch(args.expected_head_sha):
                raise PolicyError("expected head SHA는 40자리 소문자 Git SHA여야 합니다.")
            config = load_json(args.config)
            result = validate_live_pull_request(
                repository=args.repository,
                pr_number=args.pr_number,
                expected_head_sha=args.expected_head_sha,
                config=config,
            )
            print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
        elif args.command == "merge-live":
            if not SHA_PATTERN.fullmatch(args.expected_head_sha):
                raise PolicyError("expected head SHA는 40자리 소문자 Git SHA여야 합니다.")
            print(json.dumps(merge_live_pull_request(args), ensure_ascii=False, sort_keys=True))
        else:
            raise PolicyError(f"지원하지 않는 명령: {args.command}")
    except (PolicyError, OSError, json.JSONDecodeError) as exc:
        print(f"PR gate failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
