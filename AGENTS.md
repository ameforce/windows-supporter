# Agent 작업 지침

## worktree 실행/등록 정책

- 임시 worktree나 Codex worktree에서 `build.bat` 실행, 테스트, 스모크테스트, `windows-supporter.exe` 단기 실행은 허용한다.
- 임시 worktree나 Codex worktree의 `windows-supporter.exe`를 Windows 시작프로그램, 자동 업데이트, 주기 실행 등 영구 런타임 대상으로 삼지 않는다.
- 영구 런타임/시작프로그램/업데이트 기준 경로는 main 물리 worktree의 `windows-supporter.exe`여야 한다. 이 머신의 main 물리 worktree는 `C:\workspace\daeng\git\tools\windows-supporter`이다.
- 진단 중 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`가 `.codex\worktrees\...` 아래를 가리키면 정상 상태가 아니다. main 물리 worktree 빌드 산출물을 기준으로 재등록되도록 수정하고 검증한다.
- 원격 배포나 SSH 진단에서 main 물리 worktree에 `git pull`, `git reset`, `git switch`, `git merge` 같은 HEAD/working-tree 변경 명령을 실행하기 전에 원격 Windows 세션의 Git GUI(Fork.exe, GitHub Desktop, SourceTree, GitKraken, TortoiseGitProc.exe)가 해당 checkout을 감시 중인지 확인한다. 실행 중이면 Git GUI를 정상 종료하거나, 사용자가 열어둬야 하는 상황이면 main worktree를 직접 변경하지 않는 절차로 우회한다. Fork가 열린 상태에서 외부 Git 명령으로 HEAD를 바꾸면 Fork/libgit2가 `[bug] head ... != ...` Git Error 팝업을 띄울 수 있다.

## PR 검토 및 보호 규칙

- 일반 구현은 활성 `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`에서 task branch를 만들고, 해당 버전형 branch를 base로 하는 PR을 통해서만 합친다. 일반 task branch는 `task/`, `feat/`, `fix/`, `chore/`, `refact/` 중 하나의 prefix를 사용하고, gate 보호 파일 변경에만 `policy/`를 사용한다.
- 활성 lane은 `.github/pr-gate/active-release.json`에 base branch, source main SHA, 목표 버전과 정책 버전을 고정한다. 서로 다른 active lane을 동시에 운영하지 않는다.
- task PR merge gate는 고정된 `pr-policy-gate`, `pr-quality-gate` 두 status context가 모두 성공하고 review thread가 모두 해결된 상태다. path filter, matrix job 이름, 임시 context로 필수 체크를 우회하지 않는다.
- PR evidence는 repository ID와 PR 번호, 정확한 base/head ref·SHA, 정책 버전, reviewer source, 생성·만료 시각, `LOW/MEDIUM/HIGH/CRITICAL = 0`, UI 변경 시 SHA-256 evidence를 canonical digest로 결합한다. head가 바뀌면 evidence를 다시 생성하고 재검토한다.
- `pr-policy-gate`와 `pr-quality-gate`는 `pull_request` test-merge revision에 고정 check를 남긴다. policy job은 exact base SHA의 validator를 실행하고, quality job은 test-merge revision에서 전체 테스트 및 artifact-only 빌드를 실행한다.
- task PR은 GitHub UI나 `gh pr merge`로 직접 병합하지 않는다. `scripts/validate_pull_request_gate.py merge-live --repository ameforce/windows-supporter --pr-number <N> --expected-head-sha <SHA> --config .github/pr-gate/active-release.json`만 사용한다. 이 controller는 현재 PR metadata와 정확한 changed-file 수를 다시 읽고 GitHub 서버 시각 기준 evidence 잔여 시간이 최소 300초인지 C snapshot 뒤 재검증한다. 이어 local controller/config bytes가 immutable base SHA의 GitHub contents와 같은지 확인한 다음 expected head SHA를 조건으로 merge commit 방식을 요청한다. 동일 사용자 credential의 client 종류는 GitHub가 구분하지 않으므로 이 제한은 현재 단독 관리자 운영 계약이며 App 전용 보안 경계가 아니다.
- gate workflow, validator, ruleset, `AGENTS.md`를 변경하는 PR은 `policy/` prefix와 maintainer 전용 `pr-gate-policy-change` label을 모두 사용한다. 일반 task PR은 이 경로를 사용하지 않는다.
- 현재 repository는 `ameforce` 단독 write/admin 운영이다. 이 gate는 악성 관리자 방어용 서명 경계가 아니라 단독 관리자와 자동화의 운영 절차를 강제한다. repository 기본 `GITHUB_TOKEN` 권한이 read여도 write actor는 workflow의 `permissions`를 변경할 수 있고, `pull_request` workflow wrapper 자체도 test-merge revision에서 실행된다는 한계를 전제로 한다. 다른 write collaborator나 write-capable integration을 추가하기 전에는 전용 GitHub App이 exact SHA에 check를 게시하고 ruleset이 해당 `integration_id`를 고정하도록 승격한다.
- GitHub ruleset의 stable name은 `windows-supporter-task-pr-gate`다. `.github/pr-gate/ruleset.json`과 `scripts/configure_github_pr_gate.ps1`로 plan/apply/verify하며, 적용 전 현재 ruleset을 export하고 적용 후 canonical/effective rule을 재검증한다.
- ruleset은 `hotfix/*`, `release/*`에 PR, 두 status check, branch 삭제와 force-push 금지를 적용하되 branch 최초 생성은 허용한다. `main`과 `develop`의 `--no-ff` 통합은 아래 trusted release controller 절차로 유지하고 task PR hard gate와 혼동하지 않는다.
- task PR은 `merge-live` 성공 후 `state=MERGED`, `mergedAt`, 정확한 base, 검토한 head SHA와 merge commit을 확인해야 완료다. closed-unmerged는 완료로 인정하지 않는다.
- 최초 gate 도입 PR처럼 base branch에 gate가 아직 없는 bootstrap은 소급 검증이 불가능하므로 릴리스 문서에 한 번의 예외로 기록한다. 이때만 `--allow-bootstrap-local-source`를 사용하며 config의 명시적 허용과 exact `source_main_sha`가 함께 맞아야 한다. ruleset 적용 뒤 실패 canary와 성공 canary PR을 모두 검증하기 전에는 hard gate가 활성화됐다고 보고하지 않는다.
- 공개된 `main`, `develop`, tag의 잘못된 이력은 revert 또는 다음 patch release로 교정한다. 정상 release 절차에서 `--force-with-lease`로 공개 ref를 다시 쓰지 않는다.

이 레포에서 수정사항이 생기면 아래 순서를 항상 지킨다.

1. 변경 후 정상 동작을 먼저 검증한다.
   - 기본 검증 명령: `uv run python -m unittest discover -s tests -p "test_*.py"`
   - 변경 범위가 명확하면 관련 테스트를 추가로 실행한다.
2. 테스트 통과 후 `@build.bat`(=`build.bat`)를 실행해 새로운 `windows-supporter.exe`를 만든다.
   - 실행 예시: `cmd /c build.bat`
3. 빌드 실패/실행 실패 시 원인을 해결한 뒤, 테스트부터 다시 수행하고 재빌드한다.

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
   - hotfix branch 최초 원격 생성 전에 `.github/pr-gate/active-release.json`을 활성화한 activation commit을 포함한다. 이후 update는 task PR과 필수 gate를 통과해야 한다.
   - task PR은 `scripts/validate_pull_request_gate.py merge-live`로만 merge하고, 이어서 `verify-merged`로 exact base/head와 merge 상태를 검증한다.
   - 서로 다른 원인/롤백 단위는 커밋을 분리한다.
   - 커밋 메시지는 `fix|feat|chore|refact: ...` 형식을 따른다.
3. 모든 task PR과 active lane 종료를 확인한 뒤 `main`으로 돌아와 `hotfix/vX.Y.Z`를 `--no-ff`로 merge하고 annotated `vX.Y.Z` 태그를 만든다.
   - merge commit 메시지는 기본 형태인 `Merge branch 'hotfix/vX.Y.Z'`를 유지한다.
   - 최종 릴리즈 빌드는 dirty build가 아니라 태그가 붙은 clean `main`에서 만든다.
4. `main` 태그 기준으로 검증과 빌드를 수행하고, 해당 SHA의 `release-chain-gate` 성공을 확인한다.
   - `uv run python -m unittest discover -s tests -p "test_*.py"`
   - `cmd /c build.bat`
   - `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments`가 태그 버전과 일치하는지 확인한다.
5. `develop`으로 돌아와 같은 `hotfix/vX.Y.Z` 브랜치를 trusted release controller 절차로 `--no-ff` merge한다.
   - `main`을 develop에 merge하지 않는다.
   - release tag merge로 대체하지 않는다.
   - merge commit 메시지는 `Merge branch 'hotfix/vX.Y.Z' into develop`를 유지한다.
   - `uv run python -m unittest discover -s tests -p "test_*.py"`를 실행한다.
   - 필요하면 `cmd /c build.bat`로 develop 빌드도 검증한다.
6. main, tag, develop을 원격에 push하고 각 ref의 `release-chain-gate` 성공을 확인한다.
   - `git push origin main`
   - `git push origin vX.Y.Z`
   - `git push origin develop`
   - 이미 잘못된 release graph를 push했다면 공개 ref를 force rewrite하지 않는다. revert 또는 다음 patch release로 복구한다.
7. merge된 hotfix 브랜치를 정리한다.
   - 로컬 브랜치: `git branch -d hotfix/vX.Y.Z`
   - 원격 branch는 main/develop push와 release-chain 성공 뒤 `scripts/configure_github_pr_gate.ps1 -Mode DeleteLane -LaneRef hotfix/vX.Y.Z`로 삭제한다. 이 명령은 repository numeric ID를 확인하고 exact ref에 creation/update freeze를 먼저 건 뒤 tip과 양쪽 ancestor를 재검증한다. 그 상태에서 exact SHA `--force-with-lease`로 조건부 삭제하고 canonical ruleset을 먼저 복원한 후 freeze를 제거한다. 무조건적인 `git push origin --delete`나 ruleset 전체 비활성화로 정상 정리를 대신하지 않는다.
8. 최종 영구 런타임 기준을 다시 확인한다.
   - 현재 branch를 `main`으로 돌려둔다.
   - main 태그 기준으로 `cmd /c build.bat`를 한 번 더 실행해 영구 실행 파일을 release 버전으로 둔다.
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
