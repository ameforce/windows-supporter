# Agent 작업 지침

## 목적과 source of truth

- 이 파일은 이 저장소 작업의 적용 조건, 불변조건, 중단 조건을 정의하는 entrypoint다. 명령 순서와 복구 절차는 아래 runbook이 소유한다.
- 관련 작업을 시작하기 전에 해당 runbook을 읽고 따른다. runbook은 이 파일의 contract ID를 생략하거나 완화할 수 없다.
- 규칙이나 live evidence가 충돌하면 mutation을 중단하고 사실·추론·불확실성을 분리해 보고한다.

## 저장소 불변조건

- **[INV-PR-VERSION-BASE]** 모든 승인된 저장소 변경은 변경 분류 후 `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`에서 파생한 task branch로 수행하고, 해당 version branch를 base로 하는 PR을 통해서만 합친다. task prefix는 `task/`, `feat/`, `fix/`, `chore/`, `refact/`, 정책 변경은 `policy/`를 사용한다.
- **[INV-PR-MERGE-PRECONDITION]** merge 직전에 PR의 base ref/SHA와 head SHA를 다시 읽고 expected head precondition으로 merge한다. 같은 base의 다른 merge를 끼우지 않으며, 이후 `state=MERGED`, `mergedAt`, base, head, merge commit을 read-back한다. closed-unmerged는 완료가 아니다.
- **[INV-PROTECTED-RULESET]** `.github/pr-protection/ruleset.json`은 `hotfix/*`와 `release/*`에 merge-only PR, non-fast-forward, deletion 보호를 적용하고 required status check를 두지 않는다.
- **[INV-NO-ACTIONS]** GitHub Actions는 비활성화하고 `.github/workflows`를 두지 않는다. task evidence, clean tagged artifact, exact ref read-back이 release 검증을 소유한다.
- **[INV-NO-PUBLIC-REF-REWRITE]** 공개된 `main`, `develop`, tag는 force rewrite하지 않는다. 잘못된 공개 이력은 revert 또는 다음 patch로 교정한다. merged disposable topic/version branch의 expected-OID compare-and-delete는 공개 release ref rewrite와 별개다.
- **[INV-MAIN-RUNTIME]** 영구 runtime·시작프로그램·자동 업데이트 기준은 main 물리 worktree `C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe`다. 임시/Codex worktree artifact를 영구 등록하지 않는다.
- **[INV-GIT-GUI-GUARD]** main 물리 checkout의 HEAD/worktree를 바꾸기 전에 Fork, GitHub Desktop, SourceTree, GitKraken, TortoiseGitProc가 감시 중인지 확인한다. 실행 중이면 정상 종료하거나 main을 바꾸지 않는 절차를 사용한다.
- **[INV-FAIL-CLOSED-CLEANUP]** ref·worktree·artifact의 expected identity, ownership, clean state, runtime 비사용을 모두 증명하지 못하면 삭제하지 않는다. force fallback이나 광역 clean으로 우회하지 않는다.

## 변경 분류 게이트

- **[CLASS-INTENT-FIRST]** version lane을 만들기 전에 `의도한 계약 → 현재 동작 → 차이 → 판정`을 기록한다.
- Hotfix는 원래 의도한 동작의 버그, 회귀, 누락 또는 불완전 구현을 복구한다.
- Release는 기존 의도에 없던 사용자 기능이나 제품 정책을 의도적으로 도입한다.
- 내부 재설계, 마이그레이션, 코드량 또는 UI 수정 규모만으로 hotfix를 release로 승격하지 않는다.
- 새 capability 없는 표시 polish(가독성·간격·표기·배분의 사용자 지시 개선)는 hotfix로 분류한다. 메커니즘 의도를 뒤집어 보여도 제품 레벨 미완성 구현의 완성으로 본다 (v0.19.0 선례: release로 열었으나 hotfix가 적절했음).
- 문서와 확인된 제품 의도가 충돌하면 그 증거를 남긴다. 문서와 구현이 함께 잘못됐다면 같은 hotfix에서 수정한다.
- repo 고유 정책과 확인된 제품 계약을 일반 SemVer 추정보다 우선한다.
- 운영 계약 결함 복구는 hotfix이고, 의도적인 새 운영 정책은 release다. 진행 중인 lane에서 발견한 절차 보강은 [CLASS-INTENT-FIRST] 판정이 현재 lane과 호환될 때만 같은 lane의 별도 task PR로 통합한다. 호환되지 않으면 현재 lane에 섞지 않고 affected completion을 중단한 뒤 올바르게 분류된 lane의 순서와 base를 결정한다.
- 사용자가 `로컬 빌드만`, `커밋/푸시 금지`, `핫픽스 금지`처럼 delivery 범위를 명시적으로 제한한 경우에만 해당 효과를 생략한다.

## 검증 매트릭스

- **[VAL-SCOPE-MINIMUM]** 구현 전에 변경 파일, 직접 호출 경로, 영향받는 test module과 native scenario를 정하고 변경 동작을 직접 증명하는 최소 집합만 실행한다.
- **[VAL-NO-UI-FOR-NONUI]** 정책·문서·ref graph·worktree cleanup만 변경한 task에서는 UI-visible native/E2E/browser/screenshot/app-window test와 실제 앱 launch를 실행하지 않는다. `tests/unit`에 있어도 실제 Tk 창을 띄우면 UI-visible test로 취급한다.

| 변경 범위 | task/PR 필수 검증 | UI-visible 검증 | task build |
|---|---|---|---|
| 정책·문서 | direct contract test, 구조/read-back, diff check, semantic review | 금지 | 없음 |
| ref graph·worktree cleanup | exact ref/parent/ancestry/ownership/inventory/read-back | 금지 | 없음 |
| 비-UI 내부 코드 | 직접 영향 unit/static test | 금지 | runtime·packaging 영향 시에만 |
| 비-UI runtime | targeted unit/integration | 무관한 UI는 금지 | 필요 |
| UI 동작 | targeted unit + 변경 화면/상태 subset | 변경 subset만 | 필요 |
| packaging·build·update | direct build contract + 최소 artifact smoke | UI가 변경 계약일 때만 | 필요 |

- 전체 `unittest discover` 또는 전체 E2E는 기본 검증이 아니다. 사용자가 명시적으로 요청했거나 공용 기반 변경 때문에 targeted selection이 불가능하다는 구체적 증거가 있을 때만 이유를 먼저 기록하고 실행한다.
- 동일 source tree의 immutable targeted evidence는 task/main/develop에서 재사용한다. conflict/content drift가 있을 때만 그 차이에 직접 대응하는 검증을 추가한다.
- 실패하면 실패한 test와 직접 영향 범위부터 수정·재실행하며 관련성 증거 없이 범위를 확대하지 않는다.
- **[REL-CLEAN-TAGGED-BUILD]** task 범위와 별개로 version release를 닫을 때는 clean tagged `main`의 동일 SHA에서 `WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY=1`로 permanent runtime을 건드리지 않는 final candidate build를 한 번 만들거나, 같은 SHA의 이미 검증된 artifact를 재사용한다. runtime·packaging release는 별도 transactional deploy helper로만 승격하고 readiness/rollback receipt를 read-back한다. metadata와 artifact identity를 read-back한다.

## delivery workflow gates

1. **분류·preflight:** [CLASS-INTENT-FIRST] 판정, status, release graph, 다음 version의 local/remote branch·tag 충돌을 확인한다.
2. **version/task branch:** 선택한 version lane에서 task branch를 만들고, 원인·rollback 단위가 다르면 commit을 나눈다. commit은 `fix|feat|chore|refact: ...` 형식을 사용한다.
3. **task PR:** matrix-selected 검증과 필요한 build를 완료한 뒤 [INV-PR-MERGE-PRECONDITION]으로 version branch에 merge한다. main/develop에 직접 commit하지 않는다.
4. **main/tag:** 모든 task PR 후 version branch를 `main`에 `--no-ff` merge하고 annotated version tag를 만든다. merge message와 parent topology를 보존한다.
5. **tagged artifact:** [REL-CLEAN-TAGGED-BUILD]와 metadata 검증을 완료한다. 정책-only task 때문에 UI test를 추가하지 않는다.
6. **develop back-merge:** 같은 version tip을 `develop`에 `--no-ff` merge한다. `main`이나 release tag를 대신 merge하지 않는다. drift가 없으면 task 검증을 반복하지 않는다.
7. **publish:** `main`, tag, `develop`을 push하고 exact remote refs를 read-back한다. [INV-NO-PUBLIC-REF-REWRITE]를 지킨다.
8. **cleanup:** merged topic/worktree를 먼저 정리하고 protected version branch를 정리한다. 두 transaction을 섞거나 topic 때문에 canonical ruleset을 변경하지 않는다.
9. **final evidence:** main/tag/develop topology, local/remote refs, artifact, runtime, cleanup receipt를 확인한 뒤에만 완료로 보고한다.

상세 순서와 hotfix/release lane 차이는 `docs/runbooks/release-delivery.md`를 따른다.

## 안전 계약

- **[SAFE-CLEANUP-PROVENANCE]** topic/worktree cleanup은 immutable PR/version-tip receipt, current porcelain binding, exhaustive NUL-safe ignored inventory, expected-OID ref deletion을 요구한다. 하나라도 불명확하면 보존한다.
- **[SAFE-CANONICAL-RESTORE]** protected version branch cleanup은 canonical ruleset 원상복구를 freeze 제거보다 먼저 검증한다. 복구를 증명하지 못하면 emergency full freeze를 유지하고 중단한다.
- `git worktree remove --force`, `git branch -D`, recursive force deletion, 광역 `git clean -fdx`를 cleanup 우회 수단으로 사용하지 않는다.
- local/remote ref는 독립적으로 판단한다. expected SHA의 존재 ref만 compare-and-delete하고, 이미 없는 ref는 최종 read-back에서도 없어야 한다.
- 임시 worktree에서는 build/test/smoke/단기 실행이 가능하지만 persistent runtime으로 등록하지 않는다. 시작프로그램이 임시 경로면 main artifact 기준으로 복구하고 검증한다.

## 완료 증거

- **[EVIDENCE-RELEASE-CLOSE]** 최종 보고는 version lane, main merge, annotated tag, develop back-merge와 parent SHA를 포함한다.
- matrix에서 선택한 검증과 제외한 UI/full-suite 검증의 이유, source identity, tagged artifact SHA-256·`FileVersion`·`ProductVersion`·`Comments`를 포함한다.
- `main`, tag, `develop` push/read-back, 최종 local/remote refs와 release graph를 포함한다.
- 각 topic의 immutable cleanup receipt, 삭제한 refs/worktrees/artifacts, 보존한 대상과 이유를 포함한다.
- protected version branch와 temporary freeze의 삭제, canonical ruleset 복원 read-back을 포함한다.
- 최종 main physical runtime 경로와 시작프로그램 등록 경로를 포함한다.

## 필수 runbook

- delivery와 branch topology: `docs/runbooks/release-delivery.md`
- 검증 선택과 tagged build: `docs/runbooks/validation-and-release-build.md`
- topic/worktree cleanup: `docs/runbooks/topic-worktree-cleanup.md`
- protected version branch cleanup: `docs/runbooks/protected-version-branch-cleanup.md`
- release graph와 완료 증거: `docs/runbooks/release-evidence.md`
- main runtime·Git GUI·시작프로그램: `docs/runbooks/runtime-registration.md`
