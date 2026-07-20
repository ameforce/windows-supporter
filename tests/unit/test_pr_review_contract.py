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

    def test_separate_pull_request_validation_gate_is_absent(self) -> None:
        self.assertFalse(
            (REPO_ROOT / ".github/workflows/pull-request-validation.yml").exists()
        )

        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        template = (REPO_ROOT / ".github/pull_request_template.md").read_text(
            encoding="utf-8"
        )
        for removed_text in ("pull-request-validation", "reviews-complete"):
            with self.subTest(removed_text=removed_text):
                self.assertNotIn(removed_text, contract)
                self.assertNotIn(removed_text, template)

    def test_pull_request_template_is_non_authoritative(self) -> None:
        template = (REPO_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("URL: `<URL>`; review `commit_id`", template)
        self.assertIn("review `commit_id`", template)
        self.assertIn("작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다", template)
        self.assertNotIn("reviewer_source", template)
        self.assertNotIn("review_evidence_digest", template)
        self.assertNotIn("`<URL> / review `", template)

    def test_agents_contract_requires_complete_head_before_parallel_final_reviews(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "완성된 head",
            "RCA, red test, 구현, 인접 경로",
            "관련 테스트, 전체 테스트, build",
            "자체 diff",
            "base 안정",
            "@codex review",
            "chatgpt-codex-connector",
            "review object의 `commit_id`",
            "top-level zero-finding 댓글",
            "`Reviewed commit` prefix",
            "base ref, 최신 base SHA와 head SHA",
            "native Codex subagent",
            "`gpt-5.6-sol`",
            "reasoning `high`",
            "동시에 시작",
            "결과를 격리",
            "둘 다 terminal",
            "head를 바꾸지 않는다",
            "review key",
            "중복 요청",
            "connector가 명시적 오류",
            "같은 key로 1회 재시도",
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
            "GitHub Actions 성공만으로 리뷰 완료를 선언하지 않는다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

        self.assertNotIn("reviewer_source", contract)
        self.assertNotIn("merge-live", contract)

    def test_agents_contract_requires_finding_rca_and_keeps_p3_advisory(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        template = (REPO_ROOT / ".github" / "pull_request_template.md").read_text(
            encoding="utf-8"
        )

        for required_text in (
            "P0/P1/P2 중 하나라도 존재하면 병합을 차단",
            "병합 조건은 `P0=0, P1=0, P2=0`",
            "실제 재현 또는 직접 증거",
            "직접 원인과 구조적 원인",
            "영향과 인접 실패 경로",
            "red test 또는 동등한 증거",
            "원인 경계의 최소 완전 수정",
            "불변조건, 실패 모드, side effect",
            "지적된 줄만 고치",
            "main Codex가 새 head를 완성됐다고 판정",
            "P3는 순수 권고이며 병합을 차단하지 않는다",
            "처분, owner, 만료일 또는 후속 이슈",
            "데이터·설정 무결성",
            "보안·인증",
            "공개 호환성",
            "삭제·업데이트·릴리스 무결성",
            "영향 불확실성",
            "최소 P2",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

        for required_text in (
            "P0=0, P1=0, P2=0",
            "P3는 순수 권고·비차단",
            "unresolved review thread: `0`",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, template)

        for removed_text in (
            "P3 처분",
            "위험수용",
            "미래 만료일",
            "milestone",
        ):
            with self.subTest(removed_text=removed_text):
                self.assertNotIn(removed_text, contract)
                self.assertNotIn(removed_text, template)

    def test_agents_contract_requires_intent_based_hotfix_classification(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "Hotfix는 원래 의도한 동작의 버그, 회귀, 누락 또는 불완전 구현을 복구한다",
            "Release는 기존 의도에 없던 사용자 기능이나 제품 정책을 의도적으로 도입한다",
            "내부 재설계, 마이그레이션, 코드량 또는 UI 수정 규모만으로 hotfix를 release로 승격하지 않는다",
            "문서와 확인된 제품 의도가 충돌하면 그 증거를 남긴다",
            "문서와 구현이 함께 잘못됐다면 같은 hotfix에서 수정한다",
            "의도한 계약 → 현재 동작 → 차이 → 판정",
            "repo 고유 정책과 확인된 제품 계약을 일반 SemVer 추정보다 우선한다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

    def test_review_gate_rca_documents_raw_finding_outcomes(self) -> None:
        policy = (REPO_ROOT / "docs" / "hotfix-v0.8.5-review-policy.md").read_text(
            encoding="utf-8"
        )
        for required_text in (
            "증상 줄만 고치는 patch는 거부",
            "근본 원인 수정은 수용",
            "무효 finding은 직접 반증",
            "P3 미수정은 허용",
            "미완성 head에는 final review를 요청하지 않는다",
            "두 final review를 동시에 시작",
            "동일 review key를 중복 사용하지 않는다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, policy)


if __name__ == "__main__":
    unittest.main()
