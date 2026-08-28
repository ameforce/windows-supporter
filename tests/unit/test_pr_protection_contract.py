from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PullRequestProtectionContractTest(unittest.TestCase):
    def test_server_protection_keeps_pr_only_merge_and_ref_safety(self) -> None:
        ruleset = json.loads(
            (REPO_ROOT / ".github/pr-protection/ruleset.json").read_text(
                encoding="utf-8"
            )
        )
        rule_types = [rule["type"] for rule in ruleset["rules"]]
        self.assertEqual(rule_types, ["pull_request", "non_fast_forward", "deletion"])
        self.assertNotIn("required_status_checks", rule_types)

        pull_request_rule = ruleset["rules"][0]["parameters"]
        self.assertEqual(pull_request_rule["allowed_merge_methods"], ["merge"])

    def test_pull_request_template_keeps_verification_evidence(self) -> None:
        template = (REPO_ROOT / ".github/pull_request_template.md").read_text(
            encoding="utf-8"
        )
        for required_text in (
            "## 변경 요약",
            "## 별도 검증",
            "테스트·정적검사",
            "빌드·artifact SHA",
            "Windows 실제 실행",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, template)

    def test_agents_keeps_merge_release_and_safety_contracts(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required_text in (
            "해당 버전형 branch를 base로 하는 PR을 통해서만 합친다",
            "--match-head-commit <FINAL_HEAD_SHA>",
            "PR의 base ref/SHA와 head SHA를 다시 확인",
            "state=MERGED",
            "closed-unmerged는 완료로 인정하지 않는다",
            "PR-only merge와 force-push·deletion 보호",
            "변경된 동작을 직접 검증하는 test module·test case만 선택해 실행",
            "`unittest discover` 전체 실행이나 전체 E2E는 기본 검증으로 사용하지 않는다",
            "GitHub Actions를 비활성화하고 `.github/workflows`를 두지 않는다",
            "cmd /c build.bat",
            "실패 시 원인을 수정한 뒤 실패한 test와 직접 영향 범위부터 다시 실행",
            "정상 release 절차에서 `--force-with-lease`로 공개 ref를 다시 쓰지 않는다",
            "--force-with-lease=refs/heads/hotfix/vX.Y.Z:<EXPECTED_SHA>",
            "원래 exclude 목록을 먼저 복원",
            "`creation`과 `update` freeze",
            "creation/update/deletion을 모두 차단",
            "remote ref 부재를 최종 확인",
            "임시 freeze ruleset의 ID와 이름이 live 목록에 없음을 확인",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract)

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


if __name__ == "__main__":
    unittest.main()
