# Validation and release build runbook

## 두 검증 phase

- **Task validation:** 변경 동작의 위험만 증명한다. `AGENTS.md`의 [VAL-SCOPE-MINIMUM]과 matrix를 따른다.
- **Release closure overlay:** task 범위와 별개로 final tagged source에서 installable artifact와 metadata를 한 번 증명한다. [REL-CLEAN-TAGGED-BUILD]를 따른다.

두 phase를 섞어 policy-only task에 runtime/UI 검증을 확장하거나, task build 면제를 final tagged build 면제로 해석하지 않는다.

## task validation 선택

### 정책·문서

필수 범위:
- 직접 contract test
- 문서 구조와 reference read-back
- `git diff --check`
- behavioral/semantic review

금지 범위:
- 전체 `unittest discover`
- native UI/E2E/browser/screenshot/app-window test
- 실제 Tk 창이나 애플리케이션 launch
- task-stage runtime build

테스트 파일이 `tests/unit` 아래에 있어도 실제 창을 만들면 UI-visible test다. test directory 이름이 아니라 effect로 분류한다.

### ref graph·worktree cleanup

필수 범위:
- exact local/remote refs와 old OID
- parent/ancestry/tag peeled commit
- worktree porcelain binding과 clean status
- exhaustive ignored inventory와 process/persistence ownership
- mutation 후 exact absence/read-back

UI, browser, screenshot, 앱 실행은 금지한다.

### code 변경

- 직접 호출 경로와 영향을 받는 test case만 실행한다.
- runtime·packaging 영향이 있을 때만 task build를 실행한다.
- UI 변경은 변경 화면·상태의 최소 subset만 검증한다.
- 전체 suite/E2E는 targeted selection 불가능성의 구체적 증거나 사용자의 명시적 요청이 있을 때만 이유를 먼저 기록한다.

## evidence 재사용과 실패 처리

- source tree가 동일하면 task evidence를 main/develop에서 재사용한다.
- merge conflict/content drift가 있으면 diff를 먼저 고정하고 그 차이만 검증한다.
- 실패하면 실패한 test와 직접 영향 범위를 먼저 수정·재실행한다.
- 관련성 증거 없이 더 넓은 suite, E2E, 앱 launch로 확대하지 않는다.

## final tagged build

1. current branch가 main이고 HEAD가 annotated release tag의 peeled commit인지 확인한다.
2. tracked/untracked source 상태가 clean인지 확인한다.
3. 같은 tagged SHA에서 이미 완료된 artifact가 있고 source/artifact identity를 증명할 수 있으면 재사용한다. 아니면 새로 build한다.
4. build는 permanent root executable과 running process를 변경하지 않는 candidate phase임을 확인한다.
5. Windows native process contract에 따라 stdout/stderr를 분리하고 UTF-8 strict로 실행한다.
6. permanent runtime을 건드리지 않는 상태로 `cmd /d /c build.bat`을 실행한다.
   - child environment: `WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY=1`
   - 이 변수는 build child에 명시적으로 전달하고 read-back한다.
7. policy/docs/ref/worktree-only release에서는 build 내부의 non-UI artifact validation 외에 실제 app launch, UI E2E, screenshot을 추가하지 않는다.
8. build exit code, tagged source SHA, artifact path와 SHA-256을 기록한다.
9. `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments`가 tag와 commit을 가리키는지 확인한다.
10. runtime·packaging release는 candidate 검증 뒤 `tools/deploy_runtime.py`를 별도 호출한다. helper stdout의 단일 UTF-8 JSON success receipt를 보존하고 exact target, candidate/target SHA-256, probe token에 결합된 PID·tray HWND, heartbeat 3회 이상과 startup path를 확인한다. normal `build.bat`은 `WINDOWS_SUPPORTER_DEPLOY_RECEIPT=<path>`로 보존 위치를 알린다.
11. helper 실패 시 rollback receipt가 이전 artifact 복원과 이전 runtime readiness를 모두 증명해야 한다. readiness 또는 rollback이 실패하면 publish·cleanup을 중단한다.
12. helper 또는 updater 성공 후 backup/promotion marker 잔류 여부를 확인한다. `dist/windows-supporter.exe`는 설치기와 배포가 소비하는 공개 후보이므로 보존한다. 기존 `build`, `dist`와 spec을 이름만으로 일괄 삭제하지 않는다.

`build.bat`의 normal mode도 같은 transactional deploy helper를 사용한다. candidate만 root executable로 옮기거나 실행 준비를 확인하지 않은 채 성공으로 종료하는 경로를 추가하지 않는다.

## 빌드 중간 자료의 수명

- `tools/build_workspace.py`가 빌드 또는 설치기 프로세스 안에서 새 작업공간을 할당하고 자식 작업이 끝날 때까지 소유한다. PyInstaller의 work/spec/generated 자료와 설치기의 Core/Bootstrap 파일만 이 경로에 둔다. 설치기는 고정된 최상위 폴더와 Inno Setup의 리소스 갱신이 충돌하지 않도록 새 `installer` 하위 폴더에 출력한다. 기존 폴더를 편입하거나 재사용하지 않는다.
- `dist/windows-supporter.exe`, 최종 Setup.exe와 `.sha256`, 빌드 로그와 배포 영수증은 이 경로 밖에 둔다. 설치기 출력이 이미 있으면 덮어쓰지 않고 새 출력 디렉터리를 요구한다.
- 모든 빌드·검증·내보내기 소비자가 성공으로 종료하면 `release()`를 한 번 호출한다. 실제 삭제는 기존 `CodexDisposableWorkspace` 정리기가 담당한다. 전달 성공은 삭제 완료가 아니며, 전달 실패가 빌드 성공을 실패로 바꾸지 않는다.
- 실패·중단 시에는 중간 자료를 보존한다. `WINDOWS_SUPPORTER_KEEP_BUILD_ARTIFACTS=1` 또는 설치기의 `-KeepBuildArtifacts`도 보존을 선택하며, 설치기에서 시작한 빌드에도 적용된다.
- 개인용 provider가 설치되지 않은 환경은 별도의 일반 임시 폴더에 생성하고 경로를 출력한 뒤 보존한다. 설치된 provider의 로딩·할당 실패는 새 빌드를 시작하기 전에 오류로 보고한다. 자동 정리를 위해 기존 자료를 다른 삭제 경로로 넘기지 않는다.
- updater는 빌드 생산자가 수행한 사용 종료 전달을 다시 시도하지 않는다. 저장소의 이전 build/dist/spec은 소유권과 수명을 새로 확인하는 별도 정리 대상이다.

## UI/runtime smoke admission

- UI-visible smoke는 변경된 계약이 UI 동작일 때만 별도 명령으로 실행한다.
- packaging/update 변경이 실제 app startup을 요구할 때도 이유와 대상 artifact를 먼저 기록한다.
- policy/docs/ref/worktree-only 변경에는 UI/runtime smoke를 실행하지 않는다.
- build 성공만으로 UI acceptance를 주장하지 않고, UI 검증을 하지 않았으면 명시적으로 그렇게 보고한다.
