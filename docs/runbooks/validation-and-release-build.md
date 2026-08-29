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
4. build 전 `build.bat`가 permanent root executable을 중단·교체할 수 있음을 알린다.
5. Windows native process contract에 따라 stdout/stderr를 분리하고 UTF-8 strict로 실행한다.
6. post-build 앱 launch를 막은 상태로 `cmd /d /c build.bat`을 실행한다.
   - child environment: `WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1`
   - 이 변수는 build child에 명시적으로 전달하고 read-back한다.
7. policy/docs/ref/worktree-only release에서는 build 내부의 non-UI artifact validation 외에 실제 app launch, UI E2E, screenshot을 추가하지 않는다.
8. build exit code, tagged source SHA, artifact path와 SHA-256을 기록한다.
9. `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments`가 tag와 commit을 가리키는지 확인한다.
10. `build`, `dist`, generated spec, backup/promotion marker가 의도치 않게 남지 않았는지 확인한다.

`WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY=1`은 root executable을 교체하지 않으므로 permanent release artifact를 설치해야 하는 최종 closure의 대체가 아니다.

## UI/runtime smoke admission

- UI-visible smoke는 변경된 계약이 UI 동작일 때만 별도 명령으로 실행한다.
- packaging/update 변경이 실제 app startup을 요구할 때도 이유와 대상 artifact를 먼저 기록한다.
- policy/docs/ref/worktree-only 변경에는 UI/runtime smoke를 실행하지 않는다.
- build 성공만으로 UI acceptance를 주장하지 않고, UI 검증을 하지 않았으면 명시적으로 그렇게 보고한다.
