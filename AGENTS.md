# Agent 작업 지침

## worktree 실행/등록 정책

- 임시 worktree나 Codex worktree에서 `build.bat` 실행, 테스트, 스모크테스트, `windows-supporter.exe` 단기 실행은 허용한다.
- 임시 worktree나 Codex worktree의 `windows-supporter.exe`를 Windows 시작프로그램, 자동 업데이트, 주기 실행 등 영구 런타임 대상으로 삼지 않는다.
- 영구 런타임/시작프로그램/업데이트 기준 경로는 main 물리 worktree의 `windows-supporter.exe`여야 한다. 이 머신의 main 물리 worktree는 `C:\workspace\daeng\git\tools\windows-supporter`이다.
- 진단 중 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`가 `.codex\worktrees\...` 아래를 가리키면 정상 상태가 아니다. main 물리 worktree 빌드 산출물을 기준으로 재등록되도록 수정하고 검증한다.
- 원격 배포나 SSH 진단에서 main 물리 worktree에 `git pull`, `git reset`, `git switch`, `git merge` 같은 HEAD/working-tree 변경 명령을 실행하기 전에 원격 Windows 세션의 Git GUI(Fork.exe, GitHub Desktop, SourceTree, GitKraken, TortoiseGitProc.exe)가 해당 checkout을 감시 중인지 확인한다. 실행 중이면 Git GUI를 정상 종료하거나, 사용자가 열어둬야 하는 상황이면 main worktree를 직접 변경하지 않는 절차로 우회한다. Fork가 열린 상태에서 외부 Git 명령으로 HEAD를 바꾸면 Fork/libgit2가 `[bug] head ... != ...` Git Error 팝업을 띄울 수 있다.

## PR 검토 및 보호 규칙

- 일반 구현은 `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`에서 task branch를 만들고, 해당 버전형 branch를 base로 하는 PR을 통해서만 합친다. 일반 task branch는 `task/`, `feat/`, `fix/`, `chore/`, `refact/` 중 하나의 prefix를 사용하고, 리뷰·릴리스 정책 변경에는 `policy/`를 사용할 수 있다.
- main Codex는 final review 전에 RCA, red test, 구현, 인접 경로 조사, 관련 테스트, 전체 테스트, build, 필요한 runtime 검증, 자체 diff 검토, base 안정화를 직접 완료한다. 이 조건을 충족하지 않은 미완성 head에는 final review를 요청하지 않는다. 이 조건을 충족한 commit만 완성된 head로 판정한다.
- 완성된 head에서 정확한 base ref, 최신 base SHA와 head SHA를 기록하고 `repo:base_sha:head_sha:round` 형식의 review key를 만든다. 같은 exact base/head에서 GitHub `@codex review` 1회와 native Codex subagent의 독립 review 1회를 동시에 시작하고 결과를 격리한다. 서로의 finding이나 결론을 전달하지 않으며 둘 다 terminal이 될 때까지 head를 바꾸지 않는다.
- 독립 reviewer는 `gpt-5.6-sol`, reasoning `high`, read-only로 고정한다. 구현 중 탐색, 반복 점검, incremental review에 사용하지 않는다. 동일 review key의 중복 요청을 금지한다. connector가 명시적 오류를 반환한 경우에만 같은 key로 1회 재시도할 수 있다.
- GitHub finding이 있으면 `chatgpt-codex-connector` review object의 `commit_id`가 최신 40자리 head SHA와 일치해야 한다. finding이 없어 connector가 top-level zero-finding 댓글만 남기면 그 댓글의 `Reviewed commit` prefix가 최신 head에 유일하게 해석되고, 바로 앞 review 요청에 full base/head SHA와 review key가 기록돼 있으며 요청 전후 head가 바뀌지 않았음을 확인한다.
- 두 리뷰의 finding은 `P0/P1/P2/P3`로 정규화한다. P0/P1/P2 중 하나라도 존재하면 병합을 차단하며, 병합 조건은 `P0=0, P1=0, P2=0`이다. GitHub review thread도 unresolved 0이어야 한다. 작성자가 PR 본문에 적은 finding 수, reviewer 이름, digest 또는 Actions status는 실제 리뷰 증거를 대신하지 않는다.
- 유효한 P0/P1/P2 finding이 하나라도 나오면, 같은 round의 두 final review가 모두 terminal이 된 뒤에만 수정을 시작한다. 수정 중·수정 직후에는 `@codex review`와 독립 reviewer subagent를 호출하지 않는다. finding을 “지적된 줄 패치 → 즉시 재리뷰”로 소비하는 것을 금지한다.
- finding 처분의 완료 조건(모두 충족해야 새 review key를 만들 수 있다):
  1. **재현/증거**: 해당 finding을 실제 재현하거나 동등한 직접 증거를 확보한다.
  2. **RCA**: 직접 원인과 구조적 원인(왜 그 가드/계약이 깨졌는지)을 기록한다. reviewer 문장 재서술만으로 RCA를 대체하지 않는다.
  3. **유사 결함 스캔**: 같은 성격의 인접 실패 경로(동일 가드, 동일 정규식 축, 동일 DOM 가정, 동일 sanitize 경계, locale/표기 변형, kebab/underscore/camelCase, bare label, wrapper/sibling 변형)를 조사해 같이 잠근다. 발견된 유사 결함은 같은 head에 포함한다.
  4. **근본 수정**: 원인 경계의 최소 완전 수정을 적용한다. 한 케이스 regex만 덧붙이는 대증 요법은 근본 수정으로 치지 않는다.
  5. **회귀 잠금**: red test 또는 동등한 증거로 원 finding과 유사 결함을 잠근다.
  6. **side effect 검증**: 불변조건·실패 모드·인접 경로 테스트, 관련 테스트, 전체 테스트, build, 필요한 runtime을 다시 돌리고, 수정이 기존 통과 계약(허용 앵커, 거부 노이즈, fallback)을 깨지 않았는지 확인한다.
  7. **자체 diff 검토**: 새 head가 “완성된 head” 조건을 충족한다고 main Codex가 판정한다.
- 위 1–7을 끝내지 않은 상태에서는 새 round의 `@codex review`·독립 review를 요청하지 않는다. “일단 리뷰에 넣어 확인” 목적의 예비 호출도 금지한다. 독립 reviewer는 final review 전용이며 구현 중 탐색·반복 점검·incremental review·finding 해석용으로 쓰지 않는다.
- 유효한 P0/P1/P2 finding은 실제 재현 또는 직접 증거를 확보하고, 직접 원인과 구조적 원인, 영향과 인접 실패 경로를 확인한 뒤 red test 또는 동등한 증거로 잠근다. 원인 경계의 최소 완전 수정을 적용하고 불변조건, 실패 모드, side effect 테스트와 관련 테스트, 전체 테스트, build, 필요한 runtime을 다시 검증한다.
- 지적된 줄만 고치거나 reviewer 문구를 옮겨 즉시 재검토하지 않는다. main Codex가 새 head를 완성됐다고 판정한 뒤에만 새 exact base/head와 새 round의 review key에서 두 final review를 각각 다시 1회 수행한다.
- P3는 순수 권고이며 병합을 차단하지 않는다. 처분, owner, 만료일 또는 후속 이슈를 요구하지 않는다. P3을 선택적으로 수정할 때도 같은 RCA와 검증 원칙을 적용한다. 보안·인증·개인정보, 데이터·설정 무결성, 공개 호환성, 삭제·업데이트·릴리스 무결성 또는 영향 불확실성을 침해하거나 그 가능성이 있는 finding은 최소 P2로 분류한다. reviewer 간 severity가 다르면 상위 등급을 적용하며 작성자 단독으로 하향할 수 없다.
- 리뷰 뒤 push로 head SHA가 바뀌거나 base SHA가 이동하면 이전 GitHub Codex review와 독립 review를 모두 stale로 처리한다. 새 head를 다시 완성한 뒤 새 exact base/head와 새 review key에서 두 review를 반복한다. 최종 두 리뷰가 같은 최신 base/head 쌍을 검토하지 않았다면 merge하지 않는다.
- 테스트, 정적 검사, 빌드, Windows 실제 실행, `release-chain-gate`는 리뷰와 별개의 검증 증거다. GitHub Actions 성공만으로 리뷰 완료를 선언하지 않는다.
- merge 직전에 PR의 base ref/SHA와 head SHA, 두 리뷰 대상 base/head, `P0=0, P1=0, P2=0`, unresolved thread 0을 다시 확인한다. base와 head가 모두 기대값일 때만 `gh pr merge <N> --repo ameforce/windows-supporter --merge --match-head-commit <FINAL_HEAD_SHA>` 또는 동등한 API precondition으로 병합한다. 별도 PR validation workflow는 사용하지 않는다. 최종 확인과 merge 사이에는 같은 base에 다른 PR을 merge하지 않는다. 이어 `state=MERGED`, `mergedAt`, base branch, head SHA, merge commit을 확인한다. closed-unmerged는 완료로 인정하지 않는다.
- `AGENTS.md`, 리뷰 절차, workflow, ruleset을 바꾸는 PR에도 같은 이중 리뷰 절차를 적용한다. 리뷰를 실제로 수행하지 않는 workflow나 self-attestation validator를 리뷰 gate라는 이름으로 도입하지 않는다.
- `.github/pr-protection/ruleset.json`은 `hotfix/*`와 `release/*`에 PR-only merge, stale review dismiss, unresolved thread 해소, force-push·deletion 보호만 적용한다. 이 ruleset은 이중 리뷰를 실행하거나 증명하지 않으며 required status check를 두지 않는다.
- 공개된 `main`, `develop`, tag의 잘못된 이력은 revert 또는 다음 patch release로 교정한다. 정상 release 절차에서 `--force-with-lease`로 공개 ref를 다시 쓰지 않는다.

이 레포에서 수정사항이 생기면 아래 순서를 항상 지킨다.

1. 변경 후 정상 동작을 먼저 검증한다.
   - 기본 검증 명령: `uv run python -m unittest discover -s tests -p "test_*.py"`
   - 변경 범위가 명확하면 관련 테스트를 추가로 실행한다.
2. 테스트 통과 후 `@build.bat`(=`build.bat`)를 실행해 새로운 `windows-supporter.exe`를 만든다.
   - 실행 예시: `cmd /c build.bat`
3. 빌드 실패/실행 실패 시 원인을 해결한 뒤, 테스트부터 다시 수행하고 재빌드한다.

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
   - task PR은 위의 exact-head 이중 리뷰를 통과한 뒤 테스트·빌드 검증을 별도로 완료하고 merge한다.
   - 서로 다른 원인/롤백 단위는 커밋을 분리한다.
   - 커밋 메시지는 `fix|feat|chore|refact: ...` 형식을 따른다.
3. 모든 task PR이 병합되고 exact-head 리뷰 증거가 최신임을 확인한 뒤 `main`으로 돌아와 `hotfix/vX.Y.Z`를 `--no-ff`로 merge하고 annotated `vX.Y.Z` 태그를 만든다.
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
   - 원격 branch는 main/develop push와 release-chain 성공 뒤 exact remote tip을 다시 읽고, 그 tip이 main과 develop 양쪽의 ancestor인지 확인한다.
   - 삭제 전에 별도 임시 ruleset으로 exact `refs/heads/hotfix/vX.Y.Z`에 `creation`과 `update` freeze를 적용하고 effective read-back한다. 이 freeze는 branch tip 이동과 삭제 뒤 같은 이름의 재생성을 막되 deletion은 막지 않아야 한다.
   - 이어 `.github/pr-protection/ruleset.json`의 live ruleset을 읽어 다른 규칙과 기존 exclude를 보존한 채 삭제할 exact ref만 일시 exclude하고 read-back한다. `git push origin --force-with-lease=refs/heads/hotfix/vX.Y.Z:<EXPECTED_SHA> :refs/heads/hotfix/vX.Y.Z`로 compare-and-delete하고 remote exact ref 부재를 확인한다.
   - 성공 여부와 관계없이 canonical protection의 원래 exclude 목록을 먼저 복원하고 live read-back한다. 삭제가 성공했다면 freeze를 유지한 상태에서 remote ref 부재를 다시 확인한 뒤에만 임시 freeze ruleset을 제거한다. 제거 API 결과만 신뢰하지 않고 임시 freeze ruleset의 ID와 이름이 live 목록에 없음을 확인한 다음, canonical ruleset 일치와 remote ref 부재를 최종 확인한다. freeze가 남아 있으면 cleanup 완료로 보고하지 않는다.
   - canonical 복원이나 read-back이 실패하면 임시 freeze에 `deletion`도 추가해 exact ref의 creation/update/deletion을 모두 차단하고 effective read-back한 뒤 freeze를 유지한다. 이 비상 보호까지 확인되지 않으면 즉시 중단하고 cleanup 미완료와 live ruleset/ref 상태를 보고한다. canonical 보호가 복구되기 전에는 비상 freeze를 제거하지 않는다. 삭제가 실패해 ref가 남아 있지만 canonical 복원은 성공했다면 freeze를 제거하되 cleanup 미완료로 보고한다.
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
