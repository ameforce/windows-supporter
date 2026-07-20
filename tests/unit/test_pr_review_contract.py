from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PullRequestReviewContractTest(unittest.TestCase):
    def test_obsolete_review_gate_artifacts_are_absent(self) -> None:
        self.assertFalse((REPO_ROOT / ".github/workflows" / ("pull" + "-request-validation.yml")).exists())

        forbidden_tokens = ("pull" + "-request-validation", "reviews" + "-complete")
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix not in {".md", ".py", ".yml", ".yaml", ".json"}:
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                with self.subTest(path=path, token=token):
                    self.assertNotIn(token, text)

    def test_server_protection_stays_non_review_automation(self) -> None:
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

    def test_final_review_contract_requires_preflight_and_exact_key(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        template = (REPO_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")

        for required_text in (
            "RCA 재현 또는 반증",
            "직접·구조적 원인",
            "인접 실패 경로",
            "red test",
            "원인 경계의 최소 완전 수정",
            "관련·전체 테스트",
            "self diff와 base SHA 안정화",
            "이 preflight 전에는 reviewer를 호출하지 않는다",
            "final review key",
            "동일 review key",
            "명시적 오류",
            "한 번 재시도",
            "동시에 시작",
            "gpt-5.6-sol",
            "reasoning `high`",
            "read-only",
            "상대 요청·중간 결과·결론을 전달하지 않으며",
            "reviewed head를 변경하지 않는다",
            "--match-head-commit <FINAL_HEAD_SHA>",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

        self.assertIn("review_key", template)
        self.assertIn("connector 명시 오류에만 같은 key 1회 재시도", template)
        self.assertIn("두 리뷰를 동시에 시작했고", template)
        self.assertIn("P3은 순수 권고·비차단", template)

    def test_severity_and_finding_rca_boundaries(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "P0/P1/P2 중 하나라도 존재하면 병합을 차단",
            "P3는 순수 권고이며 병합 비차단",
            "처분, owner, 만료일 또는 후속 이슈를 요구하지 않는다",
            "보안·인증·데이터·설정 무결성",
            "공개 호환성",
            "삭제·업데이트·릴리스 무결성",
            "영향 불확실성은 최소 P2",
            "작성자 단독으로 하향하지 않는다",
            "유효한 P0/P1/P2 finding은 증상 patch로 닫지 않는다",
            "불변조건·사이드 이펙트 테스트",
            "새 exact head에서 두 final review를 각각 한 번 다시 수행한다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

    def test_rca_document_records_live_cause_and_scope(self) -> None:
        rca = (REPO_ROOT / "docs/hotfix-v0.8.5-review-policy-rca.md").read_text(
            encoding="utf-8"
        )
        for required_text in (
            "형식화된 요청 95건",
            "원문 `@codex review` 댓글 96건",
            "connector 응답 25건",
            "동일 exact key 중복 4건",
            "SHA가 깨진 요청 1건",
            "사용자 기능, 외부 CLI, 공개 설정 API와 공개 Git 이력은 바꾸지 않는다",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, rca)


if __name__ == "__main__":
    unittest.main()
