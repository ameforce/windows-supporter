from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PullRequestReviewContractTest(unittest.TestCase):
    def test_actions_review_imitation_does_not_return(self) -> None:
        removed_paths = (
            ".github/pr-gate/active-release.json",
            ".github/pr-gate/ruleset.json",
            ".github/workflows/pr-policy-gate.yml",
            ".github/workflows/pr-quality-gate.yml",
            "scripts/configure_github_pr_gate.ps1",
            "scripts/validate_pull_request_gate.py",
        )

        for relative_path in removed_paths:
            with self.subTest(path=relative_path):
                self.assertFalse((REPO_ROOT / relative_path).exists())

    def test_server_protection_has_no_fake_review_status_checks(self) -> None:
        import json

        ruleset = json.loads(
            (REPO_ROOT / ".github/pr-protection/ruleset.json").read_text(encoding="utf-8")
        )
        rule_types = [rule["type"] for rule in ruleset["rules"]]
        self.assertEqual(rule_types, ["pull_request", "non_fast_forward", "deletion"])
        self.assertNotIn("required_status_checks", rule_types)

        pull_request_rule = ruleset["rules"][0]["parameters"]
        self.assertTrue(pull_request_rule["dismiss_stale_reviews_on_push"])
        self.assertTrue(pull_request_rule["required_review_thread_resolution"])
        self.assertEqual(pull_request_rule["allowed_merge_methods"], ["merge"])

    def test_pull_request_validation_is_exact_head_non_review_ci(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/pull-request-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: Pull request validation", workflow)
        self.assertIn("name: pull-request-validation", workflow)
        self.assertIn("github.event.pull_request.draft == false", workflow)
        self.assertIn("github.event.label.name == 'reviews-complete'", workflow)
        self.assertIn("- labeled", workflow)
        self.assertNotIn("- synchronize", workflow)
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("Verify immutable merge candidate identity", workflow)
        self.assertIn("git rev-list --parents -n 1 HEAD", workflow)
        self.assertIn("$parts.Count -ne 3", workflow)
        self.assertIn("$baseSha -ne $eventBaseSha", workflow)
        self.assertIn("$headSha -ne $eventHeadSha", workflow)
        self.assertIn('"base_sha=$baseSha"', workflow)
        self.assertIn('"head_sha=$headSha"', workflow)
        self.assertIn('"merge_candidate_sha=$mergeSha"', workflow)
        self.assertIn("It does not perform or prove PR review.", workflow)
        self.assertNotIn("pr-quality-gate", workflow)

    def test_pull_request_template_is_non_authoritative(self) -> None:
        template = (REPO_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("review `commit_id`", template)
        self.assertIn("작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다", template)
        self.assertNotIn("reviewer_source", template)
        self.assertNotIn("review_evidence_digest", template)

    def test_agents_contract_requires_two_exact_head_reviews(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "@codex review",
            "chatgpt-codex-connector",
            "review object의 `commit_id`",
            "top-level zero-finding 댓글",
            "`Reviewed commit` prefix",
            "base ref, 최신 base SHA와 head SHA",
            "native Codex subagent",
            "P0/P1/P2/P3",
            "unresolved 0",
            "stale",
            "--match-head-commit <FINAL_HEAD_SHA>",
            "--force-with-lease=refs/heads/hotfix/vX.Y.Z:<EXPECTED_SHA>",
            "원래 exclude 목록을 먼저 복원",
            "`creation`과 `update` freeze",
            "creation/update/deletion을 모두 차단",
            "remote ref 부재를 최종 확인",
            "임시 freeze ruleset의 ID와 이름이 live 목록에 없음을 확인",
            "`reviews-complete` label",
            "potentialMergeCommit",
            "refs/pull/<N>/merge",
            "git rev-list --parents -n 1 FETCH_HEAD",
            "workflow가 default branch에 들어간 뒤 새 PR에는 bootstrap 예외를 사용하지 않는다",
            "GitHub Actions 성공만으로 리뷰 완료를 선언하지 않는다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

        self.assertNotIn("reviewer_source", contract)
        self.assertNotIn("merge-live", contract)


if __name__ == "__main__":
    unittest.main()
