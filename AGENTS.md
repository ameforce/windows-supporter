# Agent 작업 지침

## worktree 실행/등록 정책

- 임시 worktree나 Codex worktree에서 `build.bat` 실행, 테스트, 스모크테스트, `windows-supporter.exe` 단기 실행은 허용한다.
- 임시 worktree나 Codex worktree의 `windows-supporter.exe`를 Windows 시작프로그램, 자동 업데이트, 주기 실행 등 영구 런타임 대상으로 삼지 않는다.
- 영구 런타임/시작프로그램/업데이트 기준 경로는 main 물리 worktree의 `windows-supporter.exe`여야 한다. 이 머신의 main 물리 worktree는 `C:\workspace\daeng\git\tools\windows-supporter`이다.
- 진단 중 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`가 `.codex\worktrees\...` 아래를 가리키면 정상 상태가 아니다. main 물리 worktree 빌드 산출물을 기준으로 재등록되도록 수정하고 검증한다.
- 원격 배포나 SSH 진단에서 main 물리 worktree에 `git pull`, `git reset`, `git switch`, `git merge` 같은 HEAD/working-tree 변경 명령을 실행하기 전에 원격 Windows 세션의 Git GUI(Fork.exe, GitHub Desktop, SourceTree, GitKraken, TortoiseGitProc.exe)가 해당 checkout을 감시 중인지 확인한다. 실행 중이면 Git GUI를 정상 종료하거나, 사용자가 열어둬야 하는 상황이면 main worktree를 직접 변경하지 않는 절차로 우회한다. Fork가 열린 상태에서 외부 Git 명령으로 HEAD를 바꾸면 Fork/libgit2가 `[bug] head ... != ...` Git Error 팝업을 띄울 수 있다.

## PR 및 보호 규칙

- 일반 구현은 `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`에서 task branch를 만들고, 해당 버전형 branch를 base로 하는 PR을 통해서만 합친다. 일반 task branch는 `task/`, `feat/`, `fix/`, `chore/`, `refact/` 중 하나의 prefix를 사용하고, 릴리스 정책 변경에는 `policy/`를 사용할 수 있다.
- merge 직전에 PR의 base ref/SHA와 head SHA를 다시 확인한다. base와 head가 모두 기대값일 때만 `gh pr merge <N> --repo ameforce/windows-supporter --merge --match-head-commit <FINAL_HEAD_SHA>` 또는 동등한 API precondition으로 병합한다. 별도 PR validation workflow는 사용하지 않는다. 최종 확인과 merge 사이에는 같은 base에 다른 PR을 merge하지 않는다. 이어 `state=MERGED`, `mergedAt`, base branch, head SHA, merge commit을 확인한다. closed-unmerged는 완료로 인정하지 않는다.
- `.github/pr-protection/ruleset.json`은 `hotfix/*`와 `release/*`에 PR-only merge와 force-push·deletion 보호를 적용하며 required status check를 두지 않는다.
- 공개된 `main`, `develop`, tag의 잘못된 이력은 revert 또는 다음 patch release로 교정한다. 정상 release 절차에서 `--force-with-lease`로 공개 ref를 다시 쓰지 않는다.

## 검증 범위 정책

이 레포의 기본 검증은 항상 변경 범위에 직접 대응하는 최소 집합이다.

1. 구현 전에 변경 파일, 직접 호출 경로, 영향받는 test module과 native scenario를 명시한다.
2. 변경된 동작을 직접 검증하는 test module·test case만 선택해 실행한다. `unittest discover` 전체 실행이나 전체 E2E는 기본 검증으로 사용하지 않는다.
3. 사용자 UI 변경은 해당 화면과 상태만 포함하는 native/E2E scenario subset으로 검증한다. 변경과 무관한 화면, 앱, 브라우저, 시나리오는 실행하지 않는다.
4. 전체 test suite 또는 전체 E2E는 사용자가 명시적으로 요청했거나, 공용 기반 코드의 광범위한 영향으로 targeted selection이 불가능하다는 구체적 증거가 있을 때만 실행한다. 실행 전 그 이유를 기록한다.
5. 동일 source tree에 대해 이미 통과한 targeted 검증을 task branch, main merge, develop back-merge에서 반복하지 않는다. merge conflict나 content drift가 없으면 기존 immutable evidence를 재사용한다.
6. runtime·packaging 코드가 변경된 경우 targeted 검증 통과 후 `cmd /c build.bat`으로 새 `windows-supporter.exe`를 만든다. 문서·정책만 변경된 경우 build를 강제하지 않는다.
7. 실패 시 원인을 수정한 뒤 실패한 test와 직접 영향 범위부터 다시 실행한다. 관련성 증거 없이 전체 suite로 확대하지 않는다.

## 변경 분류 게이트

- Hotfix는 원래 의도한 동작의 버그, 회귀, 누락 또는 불완전 구현을 복구한다.
- Release는 기존 의도에 없던 사용자 기능이나 제품 정책을 의도적으로 도입한다.
- 내부 재설계, 마이그레이션, 코드량 또는 UI 수정 규모만으로 hotfix를 release로 승격하지 않는다.
- 문서와 확인된 제품 의도가 충돌하면 그 증거를 남긴다. 문서와 구현이 함께 잘못됐다면 같은 hotfix에서 수정한다.
- 분류 대상 branch를 만들기 전에 `의도한 계약 → 현재 동작 → 차이 → 판정`을 기록한다.
- repo 고유 정책과 확인된 제품 계약을 일반 SemVer 추정보다 우선한다.

## hotfix 완료 정책

Codex가 이 레포에서 버그 수정, 개선, 운영 지침 보강을 구현했다면 단순히 테스트/빌드로 멈추지 않고 버전형 hotfix 브랜치에서 커밋하고 핫픽스를 완료한다. 사용자가 명시적으로 `로컬 빌드만`, `커밋/푸시 금지`, `핫픽스 금지`라고 제한한 경우에만 핫픽스 완료를 생략한다.

사용자가 `hotfix 진행`, `핫픽스`, `release finish`, `릴리즈 닫기`처럼 핫픽스/릴리즈 완료를 요청하면 단순히 main worktree에 수정하고 빌드하는 것으로 끝내지 않는다. 사용자가 명시적으로 `로컬 빌드만`, `커밋/푸시 금지`라고 제한하지 않는 한 아래 절차를 완료 조건으로 삼는다. `AGENTS.md`, 빌드 스크립트, 릴리즈 정책 같은 운영 지침 변경도 예외가 아니며, 핫픽스 절차를 보강하는 수정 자체도 진행 중인 버전형 hotfix 브랜치에서 처리한다.

1. 시작 전 현재 상태를 확인한다.
   - `git status --short --branch`
   - `git log --graph --decorate --oneline --branches --remotes --tags --max-count=12`
   - 다음 버전의 local/remote branch/tag 충돌 여부
2. 반드시 버전형 hotfix 브랜치 `hotfix/vX.Y.Z`를 만들고, 그 branch에서 task branch를 파생해 작업한다.
   - `hotfix/설명`, `hotfix/<기능명>`, `codex/...` 같은 비버전 브랜치명으로 hotfix를 닫지 않는다.
   - `main` 또는 `develop`에 직접 커밋하지 않는다. 실수로 직접 커밋했으면 그 커밋을 버전형 `hotfix/vX.Y.Z` 브랜치 tip으로 옮기고 main/develop merge commit을 다시 만든다.
   - 브랜치명이나 커밋 경로가 이 규칙과 맞지 않으면 새 설명형 브랜치를 만들지 말고, 현재 핫픽스 버전 또는 사용자가 명시한 버전의 `hotfix/vX.Y.Z`로 복구한다.
   - 진행 중인 핫픽스를 검토하거나 수정하는 과정에서 `AGENTS.md` 같은 절차 보강이 필요해졌다면 새 패치 버전을 만들지 말고 같은 `hotfix/vX.Y.Z` 대상 task PR에 포함한다.
   - task PR은 테스트·빌드 검증을 완료한 뒤 merge한다.
   - 서로 다른 원인/롤백 단위는 커밋을 분리한다.
   - 커밋 메시지는 `fix|feat|chore|refact: ...` 형식을 따른다.
3. 모든 task PR이 병합된 뒤 `main`으로 돌아와 `hotfix/vX.Y.Z`를 `--no-ff`로 merge하고 annotated `vX.Y.Z` 태그를 만든다.
   - merge commit 메시지는 기본 형태인 `Merge branch 'hotfix/vX.Y.Z'`를 유지한다.
   - 최종 릴리즈 빌드는 dirty build가 아니라 태그가 붙은 clean `main`에서 만든다.
4. `main` 태그 기준으로 최종 artifact와 해당 SHA의 local release evidence 일치를 확인한다.
   - task branch에서 통과한 targeted test·native scenario evidence가 `main`의 source tree와 동일한지 확인한다.
   - merge conflict나 content drift가 없으면 같은 test나 E2E를 다시 실행하지 않는다. 차이가 있으면 그 차이에 직접 대응하는 targeted test만 실행한다.
   - `cmd /c build.bat`
   - `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments`가 태그 버전과 일치하는지 확인한다.
5. `develop`으로 돌아와 같은 `hotfix/vX.Y.Z` 브랜치를 trusted release controller 절차로 `--no-ff` merge한다.
   - `main`을 develop에 merge하지 않는다.
   - release tag merge로 대체하지 않는다.
   - merge commit 메시지는 `Merge branch 'hotfix/vX.Y.Z' into develop`를 유지한다.
   - merge conflict나 content drift가 없으면 task branch의 targeted 검증을 반복하지 않는다. develop 고유 차이가 생긴 경우에만 그 차이에 직접 대응하는 targeted test를 실행한다.
   - develop build는 develop 고유 packaging 변경을 검증해야 할 때만 실행한다.
6. GitHub Actions 없이 main, tag, develop을 원격에 push하고 exact remote ref를 read-back한다.
   - 이 저장소는 GitHub Actions를 비활성화하고 `.github/workflows`를 두지 않는다. 릴리스 검증은 task의 targeted evidence, clean tagged `main` artifact와 local/remote ref read-back이 소유한다.
   - `git push origin main`
   - `git push origin vX.Y.Z`
   - `git push origin develop`
   - 이미 잘못된 release graph를 push했다면 공개 ref를 force rewrite하지 않는다. revert 또는 다음 patch release로 복구한다.
7. merge된 hotfix 브랜치를 정리한다.
   - 로컬 브랜치: `git branch -d hotfix/vX.Y.Z`
   - 원격 branch는 main/tag/develop push와 exact remote ref read-back 뒤 tip을 다시 읽고, 그 tip이 main과 develop 양쪽의 ancestor인지 확인한다.
   - 삭제 전에 별도 임시 ruleset으로 exact `refs/heads/hotfix/vX.Y.Z`에 `creation`과 `update` freeze를 적용하고 effective read-back한다. 이 freeze는 branch tip 이동과 삭제 뒤 같은 이름의 재생성을 막되 deletion은 막지 않아야 한다.
   - 이어 `.github/pr-protection/ruleset.json`의 live ruleset을 읽어 다른 규칙과 기존 exclude를 보존한 채 삭제할 exact ref만 일시 exclude하고 read-back한다. `git push origin --force-with-lease=refs/heads/hotfix/vX.Y.Z:<EXPECTED_SHA> :refs/heads/hotfix/vX.Y.Z`로 compare-and-delete하고 remote exact ref 부재를 확인한다.
   - 성공 여부와 관계없이 canonical protection의 원래 exclude 목록을 먼저 복원하고 live read-back한다. 삭제가 성공했다면 freeze를 유지한 상태에서 remote ref 부재를 다시 확인한 뒤에만 임시 freeze ruleset을 제거한다. 제거 API 결과만 신뢰하지 않고 임시 freeze ruleset의 ID와 이름이 live 목록에 없음을 확인한 다음, canonical ruleset 일치와 remote ref 부재를 최종 확인한다. freeze가 남아 있으면 cleanup 완료로 보고하지 않는다.
   - canonical 복원이나 read-back이 실패하면 임시 freeze에 `deletion`도 추가해 exact ref의 creation/update/deletion을 모두 차단하고 effective read-back한 뒤 freeze를 유지한다. 이 비상 보호까지 확인되지 않으면 즉시 중단하고 cleanup 미완료와 live ruleset/ref 상태를 보고한다. canonical 보호가 복구되기 전에는 비상 freeze를 제거하지 않는다. 삭제가 실패해 ref가 남아 있지만 canonical 복원은 성공했다면 freeze를 제거하되 cleanup 미완료로 보고한다.
8. 최종 영구 런타임 기준을 다시 확인한다.
   - 현재 branch를 `main`으로 돌려둔다.
   - step 4에서 태그가 붙은 clean `main`으로 만든 artifact가 같은 SHA의 산출물이고 그대로 존재하면 재사용한다. artifact가 없거나 source SHA가 달라졌을 때만 `cmd /c build.bat`를 다시 실행한다.
   - `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`가 `C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe`를 가리키는지 확인한다.
   - `git status --short --branch`에서 `main...origin/main`이 clean/synced인지 확인한다.
   - hotfix 브랜치 tip이 `main`과 `develop` 양쪽의 ancestor인지 확인한다.
   - `git rev-list --parents -n 1 main`과 `git rev-list --parents -n 1 develop`에서 각각 부모 2개짜리 merge commit인지 확인한다.
   - 최종 release graph 보고는 `git log --graph --decorate --oneline --branches --remotes --tags --max-count=<N>` 기준으로 한다. `git log main develop`처럼 인자 순서가 출력 순서를 왜곡할 수 있는 명령을 최종 그래프 근거로 쓰지 않는다.
   - `git log --all`은 Codex 앱의 로컬 내부 ref인 `refs/codex/turn-diffs/...`까지 포함할 수 있으므로 hotfix 완료 그래프의 기준 명령으로 쓰지 않는다. 내부 ref가 의심되면 `git for-each-ref --format='%(refname) %(objectname)' refs/codex`로 별도 식별하고, release refs 검증과 섞어 보고하지 않는다.
   - Git-flow hotfix finish 후 최종 release graph에서 `develop` back-merge commit이 `main` release merge commit보다 위에 보이는지 확인한다. 불명확하면 parent/ref 검증 결과를 함께 보고하고, 필요하면 `develop` merge commit을 `main` merge commit 이후 다시 만든다.
   - 브랜치/태그 정리 여부는 `git log --all`의 시각 출력만으로 판정하지 않는다. `git show-ref`, `git for-each-ref --format='%(refname:short) %(objectname:short)' refs/heads refs/remotes refs/tags`, `git ls-remote --heads --tags origin`으로 실제 local/remote refs를 확인한다.
   - 이전에 잘못 만든 commit이 `git fsck`, reflog, unreachable object, 또는 장식 없는 그래프 출력에만 보이고 refs에 없으면 남은 브랜치/태그로 보지 않는다. 반대로 잘못된 이름이나 잘못된 commit을 가리키는 ref가 하나라도 있으면 정리 완료로 보고하지 않는다.

핫픽스 완료 보고에는 최소한 다음을 포함한다.

- hotfix 브랜치 이름, main merge commit, tag, develop back-merge commit
- push 대상과 결과(`main`, tag, `develop`)
- 삭제한 local/remote hotfix 브랜치
- 최종 local/remote refs 검증 결과
- 최종 release graph에서 `develop` back-merge가 `main` release merge 위에 표시되는지
- 테스트/빌드 명령과 결과
- 최종 `windows-supporter.exe` 버전과 시작프로그램 등록 경로
