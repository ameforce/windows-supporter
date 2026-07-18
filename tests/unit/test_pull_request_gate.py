from __future__ import annotations

import datetime as dt
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_pull_request_gate.py"
SPEC = importlib.util.spec_from_file_location("validate_pull_request_gate", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
REPOSITORY_ID = 1202717044
REPOSITORY = "ameforce/windows-supporter"
NOW = dt.datetime(2026, 7, 18, 3, 0, tzinfo=dt.timezone.utc)


def config() -> dict[str, object]:
    return {
        "policy_version": "1.1.0",
        "state": "active",
        "lane": "hotfix",
        "active_base": "hotfix/v0.8.1",
        "repository_id": REPOSITORY_ID,
        "repository_full_name": REPOSITORY,
        "attestation_max_age_hours": 24,
        "merge_freshness_safety_margin_seconds": 300,
        "maintainer_policy_label": "pr-gate-policy-change",
        "policy_head_prefix": "policy/",
        "allowed_head_prefixes": ["chore/", "fix/", "feat/", "task/", "refact/", "policy/"],
        "protected_paths": [".github/pr-gate/", ".github/workflows/", "AGENTS.md"],
        "ui_path_patterns": ["main.py", "src/apps/*_ui.py"],
    }


def attestation(*, recompute_digest: bool = True, **overrides: str) -> str:
    values = {
        "policy_version": "1.1.0",
        "repository_id": str(REPOSITORY_ID),
        "repository_full_name": REPOSITORY,
        "pull_request_number": "17",
        "base_ref": "hotfix/v0.8.1",
        "base_sha": BASE_SHA,
        "head_ref": "chore/pr-gate-v0.8.1",
        "head_sha": HEAD_SHA,
        "reviewer_source": "codex+chatgpt-gpt-5.6-sol-pro",
        "finding_low": "0",
        "finding_medium": "0",
        "finding_high": "0",
        "finding_critical": "0",
        "ui_evidence": "not-applicable",
        "generated_at": "2026-07-18T02:30:00Z",
        "expires_at": "2026-07-18T03:30:00Z",
    }
    values.update(overrides)
    if recompute_digest or "review_evidence_digest" not in values:
        values["review_evidence_digest"] = gate.review_evidence_digest(values)
    lines = [gate.ATTESTATION_START]
    lines.extend(f"{key}: {values[key]}" for key in gate.REQUIRED_ATTESTATION_KEYS)
    lines.append(gate.ATTESTATION_END)
    return "\n".join(lines)


def event(
    *,
    body: str | None = None,
    labels: list[str] | None = None,
    head_ref: str = "chore/pr-gate-v0.8.1",
) -> dict[str, object]:
    resolved_body = body or attestation(head_ref=head_ref)
    return {
        "number": 17,
        "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
        "pull_request": {
            "number": 17,
            "draft": False,
            "body": resolved_body,
            "labels": [{"name": label} for label in (labels or [])],
            "base": {"ref": "hotfix/v0.8.1", "sha": BASE_SHA},
            "head": {
                "ref": head_ref,
                "sha": HEAD_SHA,
                "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            },
        },
    }


def live_pull_request(*, body: str | None = None) -> dict[str, object]:
    pull_request = event(body=body)["pull_request"]
    assert isinstance(pull_request, dict)
    value = dict(pull_request)
    value.update(
        {
            "state": "open",
            "merged_at": None,
            "changed_files": 1,
        }
    )
    base = dict(value["base"])
    base["repo"] = {"id": REPOSITORY_ID, "full_name": REPOSITORY}
    value["base"] = base
    return value


class PullRequestGateUnitTest(unittest.TestCase):
    def test_accepts_canonical_zero_finding_evidence(self) -> None:
        summary = gate.validate_event(event(), config(), ["docs/release.md"], now=NOW)

        self.assertEqual(summary["head_sha"], HEAD_SHA)
        self.assertFalse(summary["ui_changed"])
        self.assertTrue(summary["review_evidence_digest"].startswith("sha256:"))

    def test_rejects_evidence_for_previous_head(self) -> None:
        body = attestation(head_sha="3" * 40)

        with self.assertRaisesRegex(gate.PolicyError, "head_sha"):
            gate.validate_event(event(body=body), config(), ["docs/release.md"], now=NOW)

    def test_rejects_tampered_canonical_digest(self) -> None:
        original = attestation()
        tampered = original.replace(
            "reviewer_source: codex+chatgpt-gpt-5.6-sol-pro",
            "reviewer_source: codex+chatgpt-gpt-5.6-sol",
        )

        with self.assertRaisesRegex(gate.PolicyError, "review_evidence_digest"):
            gate.validate_event(event(body=tampered), config(), ["docs/release.md"], now=NOW)

    def test_rejects_nonzero_low_finding(self) -> None:
        with self.assertRaisesRegex(gate.PolicyError, "finding_low"):
            gate.validate_event(
                event(body=attestation(finding_low="1")),
                config(),
                ["docs/release.md"],
                now=NOW,
            )

    def test_requires_policy_branch_and_label_for_gate_policy_changes(self) -> None:
        with self.assertRaisesRegex(gate.PolicyError, "policy/"):
            gate.validate_event(
                event(labels=["pr-gate-policy-change"]),
                config(),
                [".github/workflows/pr-policy-gate.yml"],
                now=NOW,
            )

        policy_event = event(head_ref="policy/close-v0.8.1", labels=["pr-gate-policy-change"])
        summary = gate.validate_event(
            policy_event,
            config(),
            [".github/workflows/pr-policy-gate.yml"],
            now=NOW,
        )
        self.assertEqual(summary["protected_files"], [".github/workflows/pr-policy-gate.yml"])

    def test_requires_sha_bound_ui_evidence_for_ui_files(self) -> None:
        with self.assertRaisesRegex(gate.PolicyError, "ui_evidence"):
            gate.validate_event(event(), config(), ["src/apps/main_ui.py"], now=NOW)

        evidence = "sha256:" + "a" * 64
        summary = gate.validate_event(
            event(body=attestation(ui_evidence=evidence)),
            config(),
            ["src/apps/main_ui.py"],
            now=NOW,
        )
        self.assertTrue(summary["ui_changed"])

    def test_rejects_expired_evidence(self) -> None:
        with self.assertRaisesRegex(gate.PolicyError, "만료"):
            gate.validate_event(
                event(
                    body=attestation(
                        generated_at="2026-07-17T01:00:00Z",
                        expires_at="2026-07-18T01:00:00Z",
                    )
                ),
                config(),
                ["docs/release.md"],
                now=NOW,
            )

    def test_rejects_inactive_release_lane(self) -> None:
        inactive = config()
        inactive["state"] = "inactive"

        with self.assertRaisesRegex(gate.PolicyError, "active release lane"):
            gate.validate_event(event(), inactive, ["docs/release.md"], now=NOW)

    def test_changed_files_include_rename_source_path(self) -> None:
        with mock.patch.object(
            gate,
            "_github_json",
            return_value=[
                {
                    "filename": "docs/moved.yml",
                    "previous_filename": ".github/workflows/pr-policy-gate.yml",
                }
            ],
        ):
            paths = gate.fetch_changed_files(event(), token="token", api_url="https://api.github.test")

        self.assertEqual(paths, ["docs/moved.yml", ".github/workflows/pr-policy-gate.yml"])

    def test_changed_files_fail_closed_at_github_3000_file_limit(self) -> None:
        large_event = event()
        large_event["_changed_files"] = [f"docs/{index}.md" for index in range(3000)]

        with self.assertRaisesRegex(gate.PolicyError, "3000"):
            gate.fetch_changed_files(large_event, token=None, api_url="https://api.github.test")

    def test_detects_pull_request_snapshot_change(self) -> None:
        current = event()["pull_request"]
        assert isinstance(current, dict)
        current = dict(current)
        current["body"] = str(current["body"]) + "\nchanged"

        with self.assertRaisesRegex(gate.PolicyError, "변경"):
            gate.assert_current_pull_request(event(), current)

    def test_live_controller_revalidates_current_evidence(self) -> None:
        current = live_pull_request()
        with mock.patch.object(
            gate,
            "_run_gh_json",
            side_effect=[current, [{"filename": "docs/release.md"}], current],
        ):
            result = gate.validate_live_pull_request(
                repository=REPOSITORY,
                pr_number=17,
                expected_head_sha=HEAD_SHA,
                config=config(),
                now=NOW,
            )

        self.assertEqual(result["summary"]["head_sha"], HEAD_SHA)
        self.assertEqual(result["summary"]["expires_at"], "2026-07-18T03:30:00Z")

    def test_live_controller_rejects_expired_evidence_at_merge_time(self) -> None:
        expired = live_pull_request(
            body=attestation(
                generated_at="2026-07-17T01:00:00Z",
                expires_at="2026-07-18T01:00:00Z",
            )
        )
        with mock.patch.object(
            gate,
            "_run_gh_json",
            side_effect=[expired, [{"filename": "docs/release.md"}], expired],
        ):
            with self.assertRaisesRegex(gate.PolicyError, "만료"):
                gate.validate_live_pull_request(
                    repository=REPOSITORY,
                    pr_number=17,
                    expected_head_sha=HEAD_SHA,
                    config=config(),
                    now=NOW,
                )

    def test_live_changed_files_rejects_metadata_count_mismatch(self) -> None:
        with mock.patch.object(
            gate,
            "_run_gh_json",
            return_value=[{"filename": "docs/release.md"}],
        ):
            with self.assertRaisesRegex(gate.PolicyError, "count"):
                gate.fetch_live_changed_files(REPOSITORY, 17, 2)

    def test_server_time_response_requires_date_header(self) -> None:
        completed = mock.Mock(returncode=0, stdout='HTTP/2.0 200 OK\n\n{"ok":true}', stderr="")
        with mock.patch.object(gate.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(gate.PolicyError, "Date"):
                gate._run_gh_json_with_server_time(["api", "repos/example/repo"])

    def test_server_time_response_uses_github_date_header(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout=(
                "HTTP/2.0 200 OK\n"
                "Date: Sat, 18 Jul 2026 03:00:00 GMT\n\n"
                '{"ok":true}'
            ),
            stderr="",
        )
        with mock.patch.object(gate.subprocess, "run", return_value=completed):
            value, server_time = gate._run_gh_json_with_server_time(["api", "repos/example/repo"])

        self.assertEqual(value, {"ok": True})
        self.assertEqual(server_time, NOW)

    def test_merge_safety_margin_rejects_near_expiry(self) -> None:
        with self.assertRaisesRegex(gate.PolicyError, "safety margin"):
            gate.validate_event(
                event(),
                config(),
                ["docs/release.md"],
                now=dt.datetime(2026, 7, 18, 3, 26, tzinfo=dt.timezone.utc),
                minimum_remaining_seconds=300,
            )

    def test_controller_source_must_match_trusted_base_bytes(self) -> None:
        def trusted_bytes(_repository: str, _revision: str, path: str) -> bytes:
            return (REPO_ROOT / path).read_bytes()

        with mock.patch.object(gate, "_remote_file_bytes", side_effect=trusted_bytes):
            gate.assert_trusted_controller_source(
                repository=REPOSITORY,
                base_sha=BASE_SHA,
                config_path=REPO_ROOT / ".github/pr-gate/active-release.json",
            )

        with mock.patch.object(gate, "_remote_file_bytes", return_value=b"tampered"):
            with self.assertRaisesRegex(gate.PolicyError, "trusted base SHA"):
                gate.assert_trusted_controller_source(
                    repository=REPOSITORY,
                    base_sha=BASE_SHA,
                    config_path=REPO_ROOT / ".github/pr-gate/active-release.json",
                )

    def test_controller_source_accepts_git_equivalent_lf_checkout(self) -> None:
        def trusted_lf_bytes(_repository: str, _revision: str, path: str) -> bytes:
            return (REPO_ROOT / path).read_bytes().replace(b"\r\n", b"\n")

        with mock.patch.object(gate, "_remote_file_bytes", side_effect=trusted_lf_bytes):
            gate.assert_trusted_controller_source(
                repository=REPOSITORY,
                base_sha=BASE_SHA,
                config_path=REPO_ROOT / ".github/pr-gate/active-release.json",
            )

    def test_merge_controller_rejects_post_validation_metadata_change(self) -> None:
        current = live_pull_request()
        changed = dict(current)
        changed["body"] = str(changed["body"]) + "\nchanged"
        args = mock.Mock(
            repository=REPOSITORY,
            pr_number=17,
            expected_head_sha=HEAD_SHA,
            config=REPO_ROOT / ".github/pr-gate/active-release.json",
            allow_bootstrap_local_source=False,
        )
        with mock.patch.object(
            gate,
            "validate_live_pull_request",
            return_value={
                "pull_request": current,
                "summary": {"head_sha": HEAD_SHA},
                "changed_files": ["docs/release.md"],
            },
        ), mock.patch.object(
            gate,
            "fetch_live_pull_request_with_server_time",
            return_value=(changed, NOW),
        ), mock.patch.object(
            gate, "_run_gh_json"
        ) as merge_call:
            with self.assertRaisesRegex(gate.PolicyError, "merge 요청 전"):
                gate.merge_live_pull_request(args)

        merge_call.assert_not_called()

    def test_merge_controller_uses_expected_head_and_merge_commit_method(self) -> None:
        current = live_pull_request()
        args = mock.Mock(
            repository=REPOSITORY,
            pr_number=17,
            expected_head_sha=HEAD_SHA,
            config=REPO_ROOT / ".github/pr-gate/active-release.json",
            allow_bootstrap_local_source=False,
        )
        with mock.patch.object(
            gate,
            "validate_live_pull_request",
            return_value={
                "pull_request": current,
                "summary": {"head_sha": HEAD_SHA},
                "changed_files": ["docs/release.md"],
            },
        ), mock.patch.object(
            gate,
            "fetch_live_pull_request_with_server_time",
            return_value=(current, NOW),
        ), mock.patch.object(
            gate,
            "assert_trusted_controller_source",
        ), mock.patch.object(
            gate,
            "_run_gh_json",
            return_value={"merged": True, "sha": "4" * 40},
        ) as merge_call:
            result = gate.merge_live_pull_request(args)

        command = merge_call.call_args.args[0]
        self.assertIn(f"sha={HEAD_SHA}", command)
        self.assertIn("merge_method=merge", command)
        self.assertEqual(result["merge_commit_sha"], "4" * 40)

    def test_merge_controller_adopts_success_after_response_loss(self) -> None:
        current = live_pull_request()
        adopted = dict(current)
        adopted["merged_at"] = "2026-07-18T03:00:01Z"
        adopted["merge_commit_sha"] = "5" * 40
        args = mock.Mock(
            repository=REPOSITORY,
            pr_number=17,
            expected_head_sha=HEAD_SHA,
            config=REPO_ROOT / ".github/pr-gate/active-release.json",
            allow_bootstrap_local_source=False,
        )
        with mock.patch.object(
            gate,
            "validate_live_pull_request",
            return_value={
                "pull_request": current,
                "summary": {"head_sha": HEAD_SHA},
                "changed_files": ["docs/release.md"],
            },
        ), mock.patch.object(
            gate,
            "fetch_live_pull_request_with_server_time",
            return_value=(current, NOW),
        ), mock.patch.object(
            gate,
            "assert_trusted_controller_source",
        ), mock.patch.object(
            gate,
            "_run_gh_json",
            side_effect=gate.PolicyError("transport lost"),
        ), mock.patch.object(gate, "fetch_live_pull_request", return_value=adopted):
            result = gate.merge_live_pull_request(args)

        self.assertEqual(result["merge_commit_sha"], "5" * 40)

    def test_checked_in_workflows_and_ruleset_keep_fixed_gate_contract(self) -> None:
        policy_workflow = (REPO_ROOT / ".github/workflows/pr-policy-gate.yml").read_text(encoding="utf-8")
        quality_workflow = (REPO_ROOT / ".github/workflows/pr-quality-gate.yml").read_text(encoding="utf-8")
        configure_script = (REPO_ROOT / "scripts/configure_github_pr_gate.ps1").read_text(encoding="utf-8")
        ruleset = json.loads((REPO_ROOT / ".github/pr-gate/ruleset.json").read_text(encoding="utf-8"))

        self.assertIn("pull_request:", policy_workflow)
        self.assertNotIn("pull_request_target:", policy_workflow)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", policy_workflow)
        self.assertNotIn("paths:", policy_workflow)
        self.assertNotIn("paths:", quality_workflow)
        self.assertIn("name: pr-policy-gate", policy_workflow)
        self.assertIn("name: pr-quality-gate", quality_workflow)
        self.assertIn("- converted_to_draft", policy_workflow)
        self.assertIn("- edited", quality_workflow)
        self.assertIn(
            "group: pr-policy-${{ github.repository_id }}-${{ github.event.pull_request.number }}-${{ github.event.pull_request.base.sha }}-${{ github.event.pull_request.head.sha }}",
            policy_workflow,
        )
        self.assertIn(
            "group: pr-quality-${{ github.repository_id }}-${{ github.event.pull_request.number }}-${{ github.event.pull_request.base.sha }}-${{ github.event.pull_request.head.sha }}",
            quality_workflow,
        )
        concurrency_lines = [
            line.strip()
            for line in (policy_workflow + quality_workflow).splitlines()
            if line.strip().startswith("group:")
        ]
        self.assertTrue(all("base.sha" in line and "head.sha" in line for line in concurrency_lines))
        self.assertNotIn("continue-on-error:", policy_workflow + quality_workflow)
        self.assertIn('"DeleteLane"', configure_script)
        self.assertIn("Assert-LaneIntegrated", configure_script)
        self.assertIn('$exactRef = "refs/heads/$LaneRef"', configure_script)
        self.assertIn("--force-with-lease=refs/heads/${LaneRef}:$laneSha", configure_script)
        self.assertIn("Assert-FreezeEffective", configure_script)
        self.assertIn('type = "creation"', configure_script)
        self.assertIn('type = "update"', configure_script)
        self.assertIn("-cnotmatch", configure_script)
        self.assertIn("repository numeric identity", configure_script)
        self.assertNotIn("[Security.Cryptography.SHA256]::HashData", configure_script)
        self.assertIn("[AllowEmptyCollection()]$Value", configure_script)
        self.assertIn("$allBefore = @(Get-RepositoryRulesets)", configure_script)
        validator_source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertGreaterEqual(validator_source.count('encoding="utf-8"'), 3)
        self.assertIn("restored canonical protection", configure_script)
        self.assertIn("actions/upload-artifact@", quality_workflow)
        self.assertIn("name: Prepare pinned uv environment", quality_workflow)
        self.assertIn("timeout-minutes: 5", quality_workflow)
        self.assertIn("timeout-minutes: 30", quality_workflow)
        self.assertIn('PYTHONUNBUFFERED: "1"', quality_workflow)
        self.assertIn('-p "test_*.py" -v -f', quality_workflow)
        self.assertIn('tzutil /s "Korea Standard Time"', quality_workflow)
        self.assertIn("fetch-depth: 0", quality_workflow)
        self.assertNotIn("actions/checkout@v", policy_workflow + quality_workflow)

        pull_request_rule = next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")
        self.assertEqual(pull_request_rule["parameters"]["required_reviewers"], [])
        status_rule = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
        contexts = [item["context"] for item in status_rule["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, ["pr-policy-gate", "pr-quality-gate"])
        self.assertTrue(status_rule["parameters"]["do_not_enforce_on_create"])
        self.assertFalse(any(rule["type"] == "required_linear_history" for rule in ruleset["rules"]))
        self.assertIn({"type": "non_fast_forward"}, ruleset["rules"])
        self.assertIn({"type": "deletion"}, ruleset["rules"])


if __name__ == "__main__":
    unittest.main()
