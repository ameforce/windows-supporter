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

    def test_agents_contract_requires_two_exact_head_reviews(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "@codex review",
            "native Codex subagent",
            "P0/P1/P2/P3",
            "unresolved 0",
            "stale",
            "GitHub Actions 성공만으로 리뷰 완료를 선언하지 않는다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

        self.assertNotIn("reviewer_source", contract)
        self.assertNotIn("merge-live", contract)


if __name__ == "__main__":
    unittest.main()
