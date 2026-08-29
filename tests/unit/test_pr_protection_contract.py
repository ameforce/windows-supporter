from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_ROOT = REPO_ROOT / "docs" / "runbooks"


class PullRequestProtectionContractTest(unittest.TestCase):
    def _read_runbook(self, name: str) -> str:
        return (RUNBOOK_ROOT / name).read_text(encoding="utf-8")

    def test_server_protection_keeps_pr_only_merge_and_ref_safety(self) -> None:
        ruleset = json.loads(
            (REPO_ROOT / ".github/pr-protection/ruleset.json").read_text(
                encoding="utf-8"
            )
        )
        rule_types = [rule["type"] for rule in ruleset["rules"]]
        self.assertEqual(rule_types, ["pull_request", "non_fast_forward", "deletion"])
        self.assertNotIn("required_status_checks", rule_types)
        self.assertEqual(
            ruleset["rules"][0]["parameters"]["allowed_merge_methods"], ["merge"]
        )

    def test_repository_keeps_actions_disabled_and_pr_evidence_template(self) -> None:
        self.assertFalse((REPO_ROOT / ".github" / "workflows").exists())
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

    def test_agents_entrypoint_keeps_stable_invariants_and_runbook_links(self) -> None:
        contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for contract_id in (
            "[INV-PR-VERSION-BASE]",
            "[INV-PR-MERGE-PRECONDITION]",
            "[INV-PROTECTED-RULESET]",
            "[INV-NO-ACTIONS]",
            "[INV-NO-PUBLIC-REF-REWRITE]",
            "[INV-MAIN-RUNTIME]",
            "[INV-GIT-GUI-GUARD]",
            "[INV-FAIL-CLOSED-CLEANUP]",
            "[CLASS-INTENT-FIRST]",
            "[VAL-SCOPE-MINIMUM]",
            "[VAL-NO-UI-FOR-NONUI]",
            "[REL-CLEAN-TAGGED-BUILD]",
            "[SAFE-CLEANUP-PROVENANCE]",
            "[SAFE-CANONICAL-RESTORE]",
            "[EVIDENCE-RELEASE-CLOSE]",
        ):
            with self.subTest(contract_id=contract_id):
                self.assertEqual(contract.count(f"**{contract_id}**"), 1)

        for runbook in (
            "docs/runbooks/release-delivery.md",
            "docs/runbooks/validation-and-release-build.md",
            "docs/runbooks/topic-worktree-cleanup.md",
            "docs/runbooks/protected-version-branch-cleanup.md",
            "docs/runbooks/release-evidence.md",
            "docs/runbooks/runtime-registration.md",
        ):
            with self.subTest(runbook=runbook):
                self.assertIn(runbook, contract)
                self.assertTrue((REPO_ROOT / runbook).is_file())

    def test_classification_routes_only_compatible_changes_to_active_lane(self) -> None:
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        delivery = self._read_runbook("release-delivery.md")
        for text in (agents, delivery):
            self.assertIn("판정이", text)
            self.assertIn("현재 lane과 호환", text)
            self.assertIn("호환되지 않으면 현재 lane에 섞지 않고", text)
        self.assertIn("Hotfix는 원래 의도한 동작", agents)
        self.assertIn("Release는 기존 의도에 없던", agents)
        self.assertIn("의도한 계약 → 현재 동작 → 차이 → 판정", agents)

    def test_delivery_enforces_exact_pr_and_release_topology_order(self) -> None:
        delivery = self._read_runbook("release-delivery.md")
        required = (
            "hotfix는 확인된 clean `main`",
            "release는 확인된 clean `develop`",
            "PR의 base ref/SHA, head ref/SHA와 remote version tip",
            "--match-head-commit <FINAL_HEAD_SHA>",
            "state=MERGED",
            "main merge가 두 parent이고 second parent가 final version tip",
            "같은 tip을 develop에 `--no-ff` merge",
            "main 또는 release tag를 develop에 merge하지 않는다",
            "git push origin main",
            "GitHub Actions run은 completion gate가 아니다",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, delivery)
        self.assertLess(delivery.index("--match-head-commit"), delivery.index("state=MERGED"))
        self.assertLess(delivery.index("## 4. main merge"), delivery.index("## 6. develop back-merge"))
        self.assertLess(delivery.index("## 6. develop back-merge"), delivery.index("## 7. publish"))
        self.assertLess(delivery.index("## 7. publish"), delivery.index("## 8. cleanup"))

    def test_validation_forbids_ui_for_non_ui_and_keeps_no_launch_build(self) -> None:
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        runbook = self._read_runbook("validation-and-release-build.md")
        for required_text in (
            "실제 Tk 창을 띄우면 UI-visible test",
            "전체 `unittest discover` 또는 전체 E2E는 기본 검증이 아니다",
            "policy/docs/ref/worktree-only 변경에는 UI/runtime smoke를 실행하지 않는다",
            "child environment: `WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1`",
            "clean tagged `main`",
            "실패한 test와 직접 영향 범위를 먼저 수정·재실행",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, f"{agents}\n{runbook}")
        self.assertLess(runbook.index("## task validation 선택"), runbook.index("## final tagged build"))
        self.assertIn("실제 app launch, UI E2E, screenshot을 추가하지 않는다", runbook)

    def test_topic_cleanup_has_one_full_durable_receipt_and_safe_mutation(self) -> None:
        runbook = self._read_runbook("topic-worktree-cleanup.md")
        receipt_start = runbook.index("## immutable cleanup receipt")
        preflight_start = runbook.index("## preflight")
        receipt = runbook[receipt_start:preflight_start]
        json_start = receipt.index("```json\n") + len("```json\n")
        json_end = receipt.index("\n```", json_start)
        schema = json.loads(receipt[json_start:json_end])

        self.assertEqual(set(schema), {"schema_version", "release_tag", "version_lane", "topics"})
        self.assertEqual(schema["schema_version"], 1)
        self.assertEqual(set(schema["version_lane"]), {"ref", "tip_sha"})
        self.assertEqual(len(schema["topics"]), 1)
        topic = schema["topics"][0]
        self.assertEqual(set(topic), {"pr", "base", "head", "worktree"})
        self.assertEqual(set(topic["pr"]), {"number", "state", "merged_at"})
        self.assertEqual(topic["pr"]["state"], "MERGED")
        self.assertEqual(set(topic["base"]), {"repository", "ref", "sha_before_merge"})
        self.assertEqual(set(topic["head"]), {"ref", "final_sha"})
        self.assertEqual(set(topic["worktree"]), {"path", "creation_provenance"})
        self.assertIn("`topics`는 이번 version lane에 merge된 task PR마다 정확히 한 entry", receipt)
        self.assertIn("일부 field만 적은 요약이나 다른 schema marker를 receipt로 인정하지 않는다", receipt)

        policy_text = "\n".join(
            [
                (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8"),
                *(path.read_text(encoding="utf-8") for path in sorted(RUNBOOK_ROOT.glob("*.md"))),
            ]
        )
        self.assertNotIn("cleanup-receipt-schema", policy_text)
        for name in (
            "release-delivery.md",
            "topic-worktree-cleanup.md",
            "protected-version-branch-cleanup.md",
            "release-evidence.md",
        ):
            with self.subTest(name=name):
                self.assertIn("cleanup-receipt-v1", self._read_runbook(name))

        self.assertIn("git worktree list --porcelain", runbook)
        self.assertIn("git ls-files --others --ignored --exclude-standard -z", runbook)
        self.assertIn("git update-ref -d refs/heads/<topic> <EXPECTED_SHA>", runbook)
        self.assertIn("여러 ref는 단일 atomic push", runbook)
        self.assertIn(
            "`git worktree remove --force`, `git branch -D`, recursive force deletion, 광역 `git clean -fdx`를 사용하지 않는다",
            runbook,
        )

    def test_protected_cleanup_keeps_freeze_until_local_remote_final_proof(self) -> None:
        runbook = self._read_runbook("protected-version-branch-cleanup.md")
        create_freeze = runbook.index("`creation`과 `update`를 차단")
        canonical_restore = runbook.index("canonical ruleset의 원래 exclude와 모든 field를 복원")
        local_delete = runbook.index("git update-ref -d refs/heads/<version-lane> <EXPECTED_SHA>")
        final_absence = runbook.index("remote exact ref, local exact ref, remote-tracking ref가 모두 없는지 확인")
        remove_freeze = runbook.index("그 뒤에만 temporary freeze ruleset을 제거")
        self.assertLess(create_freeze, canonical_restore)
        self.assertLess(canonical_restore, local_delete)
        self.assertLess(local_delete, final_absence)
        self.assertLess(final_absence, remove_freeze)
        self.assertIn("creation/update/deletion을 모두 차단", runbook)
        self.assertIn("freeze ID와 name이 live 목록에 없는지 확인", runbook)
        self.assertIn("remote ref 삭제가 실패했지만 canonical 복원은 성공", runbook)

    def test_runtime_and_final_evidence_bind_main_path_and_actual_refs(self) -> None:
        runtime = self._read_runbook("runtime-registration.md")
        evidence = self._read_runbook("release-evidence.md")
        self.assertIn(
            "C:\\workspace\\daeng\\git\\tools\\windows-supporter\\windows-supporter.exe",
            runtime,
        )
        self.assertIn("Fork.exe", runtime)
        self.assertIn("main이 origin/main과 clean/synced", runtime)
        self.assertIn("git show-ref", evidence)
        self.assertIn("git ls-remote --heads --tags origin", evidence)
        self.assertIn("refs/codex/turn-diffs", evidence)
        self.assertIn("temporary freeze ID/name 부재", evidence)
        self.assertIn("시작프로그램 등록 경로", (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
