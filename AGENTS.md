# Agent 작업 지침

## worktree 실행/등록 정책

- 임시 worktree나 Codex worktree에서 `build.bat` 실행, 테스트, 스모크테스트, `windows-supporter.exe` 단기 실행은 허용한다.
- 임시 worktree나 Codex worktree의 `windows-supporter.exe`를 Windows 시작프로그램, 자동 업데이트, 주기 실행 등 영구 런타임 대상으로 삼지 않는다.
- 영구 런타임/시작프로그램/업데이트 기준 경로는 main 물리 worktree의 `windows-supporter.exe`여야 한다. 이 머신의 main 물리 worktree는 `C:\workspace\daeng\git\tools\windows-supporter`이다.
- 진단 중 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`가 `.codex\worktrees\...` 아래를 가리키면 정상 상태가 아니다. main 물리 worktree 빌드 산출물을 기준으로 재등록되도록 수정하고 검증한다.

이 레포에서 수정사항이 생기면 아래 순서를 항상 지킨다.

1. 변경 후 정상 동작을 먼저 검증한다.
   - 기본 검증 명령: `uv run python -m unittest discover -s tests -p "test_*.py"`
   - 변경 범위가 명확하면 관련 테스트를 추가로 실행한다.
2. 테스트 통과 후 `@build.bat`(=`build.bat`)를 실행해 새로운 `windows-supporter.exe`를 만든다.
   - 실행 예시: `cmd /c build.bat`
3. 빌드 실패/실행 실패 시 원인을 해결한 뒤, 테스트부터 다시 수행하고 재빌드한다.

## hotfix 완료 정책

사용자가 `hotfix 진행`, `핫픽스`, `release finish`, `릴리즈 닫기`처럼 핫픽스/릴리즈 완료를 요청하면 단순히 main worktree에 수정하고 빌드하는 것으로 끝내지 않는다. 사용자가 명시적으로 `로컬 빌드만`, `커밋/푸시 금지`라고 제한하지 않는 한 아래 절차를 완료 조건으로 삼는다. `AGENTS.md`, 빌드 스크립트, 릴리즈 정책 같은 운영 지침 변경도 예외가 아니며, 핫픽스 절차를 보강하는 수정 자체도 진행 중인 버전형 hotfix 브랜치에서 처리한다.

1. 시작 전 현재 상태를 확인한다.
   - `git status --short --branch`
   - `git log --graph --decorate --oneline --branches --remotes --tags --max-count=12`
   - 다음 버전의 local/remote branch/tag 충돌 여부
2. 반드시 버전형 hotfix 브랜치 `hotfix/vX.Y.Z`를 만든 뒤 그 위에서 작업한다.
   - `hotfix/설명`, `hotfix/<기능명>`, `codex/...` 같은 비버전 브랜치명으로 hotfix를 닫지 않는다.
   - `main` 또는 `develop`에 직접 커밋하지 않는다. 실수로 직접 커밋했으면 그 커밋을 버전형 `hotfix/vX.Y.Z` 브랜치 tip으로 옮기고 main/develop merge commit을 다시 만든다.
   - 브랜치명이나 커밋 경로가 이 규칙과 맞지 않으면 새 설명형 브랜치를 만들지 말고, 현재 핫픽스 버전 또는 사용자가 명시한 버전의 `hotfix/vX.Y.Z`로 복구한다.
   - 진행 중인 핫픽스를 검토하거나 수정하는 과정에서 `AGENTS.md` 같은 절차 보강이 필요해졌다면 새 패치 버전을 만들지 말고 같은 `hotfix/vX.Y.Z`에 포함한다.
   - 서로 다른 원인/롤백 단위는 커밋을 분리한다.
   - 커밋 메시지는 `fix|feat|chore|refact: ...` 형식을 따른다.
3. `main`으로 돌아와 `hotfix/vX.Y.Z`를 `--no-ff`로 merge하고 `vX.Y.Z` 태그를 만든다.
   - merge commit 메시지는 기본 형태인 `Merge branch 'hotfix/vX.Y.Z'`를 유지한다.
   - 최종 릴리즈 빌드는 dirty build가 아니라 태그가 붙은 clean `main`에서 만든다.
4. `main` 태그 기준으로 검증과 빌드를 수행한다.
   - `uv run python -m unittest discover -s tests -p "test_*.py"`
   - `cmd /c build.bat`
   - `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments`가 태그 버전과 일치하는지 확인한다.
5. `develop`으로 돌아와 같은 `hotfix/vX.Y.Z` 브랜치를 `--no-ff`로 merge한다.
   - `main`을 develop에 merge하지 않는다.
   - release tag merge로 대체하지 않는다.
   - merge commit 메시지는 `Merge branch 'hotfix/vX.Y.Z' into develop`를 유지한다.
   - `uv run python -m unittest discover -s tests -p "test_*.py"`를 실행한다.
   - 필요하면 `cmd /c build.bat`로 develop 빌드도 검증한다.
6. main, tag, develop을 원격에 push한다.
   - `git push origin main`
   - `git push origin vX.Y.Z`
   - `git push origin develop`
   - 이미 잘못된 release graph를 push한 뒤 복구할 때도 비버전 브랜치를 만들지 않는다. 버전형 `hotfix/vX.Y.Z`에서 이력을 재작성하고 `--force-with-lease`로 `main`, `develop`, 필요한 tag만 갱신한다.
7. merge된 hotfix 브랜치를 정리한다.
   - 로컬 브랜치: `git branch -d hotfix/vX.Y.Z`
   - 원격 hotfix 브랜치를 만들었다면: `git push origin --delete hotfix/vX.Y.Z`
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
