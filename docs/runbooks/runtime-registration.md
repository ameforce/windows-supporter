# Main runtime and Git GUI runbook

## persistent runtime boundary

- main physical worktree는 `C:\workspace\daeng\git\tools\windows-supporter`다.
- persistent executable은 `C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe`다.
- temporary/Codex worktree에서는 build, test, smoke, 단기 executable 실행이 가능하다.
- temporary worktree executable을 Windows 시작프로그램, 자동 업데이트, 주기 실행 대상으로 등록하지 않는다.

## startup registration

1. `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Windows Supporter`를 읽는다.
2. exact main physical executable을 가리키는지 확인한다.
3. `.codex\worktrees` 또는 다른 temporary path면 정상 상태가 아니다.
4. main tagged artifact를 기준으로 재등록하고 registry value를 read-back한다.
5. 실행 파일 metadata/hash와 startup path를 final evidence에 기록한다.

## Git GUI guard

main 물리 checkout에서 `git pull`, `git reset`, `git switch`, `git merge`처럼 HEAD/worktree를 변경하기 전에 다음 process가 해당 checkout을 감시 중인지 확인한다.

- Fork.exe
- GitHub Desktop
- SourceTree
- GitKraken
- TortoiseGitProc.exe

실행 중이면 정상 종료하고 process 부재를 확인한다. 사용자가 앱을 열어 두어야 하면 main checkout을 직접 바꾸지 않는 alternate worktree 절차를 사용한다. Fork/libgit2가 감시 중인 main HEAD를 외부 Git 명령으로 바꾸면 `[bug] head ... != ...` popup이 발생할 수 있다.

## build와 process

- final release build는 main physical worktree의 tagged source에서 실행한다.
- `build.bat`의 build phase는 running root executable을 중단하거나 root artifact를 교체하지 않는다.
- final candidate build에는 child environment로 `WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY=1`을 전달한다.
- runtime·packaging release의 승격은 `tools/deploy_runtime.py`만 수행한다. helper는 candidate 검증 후 exact-path process tree 종료, backup, atomic replacement, launch, tray/Tk readiness와 heartbeat 검증을 하나의 transaction으로 수행한다.
- helper는 marker를 exclusive-create해 동시 배포를 배제한다. backup과 staged candidate가 모두 검증되기 전에는 running runtime을 건드리지 않으며, preparation 실패는 target-unchanged receipt로 끝낸다.
- marker 선점 경쟁에서 진 호출자는 `transaction_conflict`와 `preserved_transaction`을 기록하고 runtime을 재기동하지 않는다. `rollback.status=target-unchanged`가 아닌 실패도 `target_unchanged` 값만으로 재기동하지 않는다.
- root executable이 없는 fresh checkout은 previous artifact 없는 transaction으로 설치하고, 실패하면 target 부재 상태로 되돌린다.
- 새 runtime의 launch/readiness가 실패하면 helper는 이전 artifact를 복원하고 이전 runtime readiness까지 확인한 뒤 비영 종료 코드와 JSON rollback receipt를 반환한다.
- marker 또는 backup이 이미 존재하면 ownership을 추정하지 않고 그대로 보존한 채 실패한다. 별도 조사 없이 덮어쓰거나 삭제하지 않는다.
- normal `build.bat`은 helper stdout/stderr를 합치지 않고 JSON receipt를 별도 UTF-8 파일에 보존한다. updater는 배포 성공 뒤 build/dist/spec을 exact repo child로 정리한다.
- build worker boundary smoke는 UI acceptance가 아니다. 실제 앱/UI launch를 별도 검증으로 주장하지 않는다.
- build 중단 시 task-owned cmd/PyInstaller tree가 남았는지 확인하고, exact task-owned process만 정리한다.

## cleanup admission

linked worktree를 제거하기 전에:

- 그 경로의 executable/tool을 쓰는 process가 없는지 확인한다.
- startup, automatic update, scheduled execution이 그 경로를 가리키지 않는지 확인한다.
- main tagged artifact가 존재하고 persistent registration이 정상인지 확인한다.

불확실하면 worktree와 artifact를 보존한다.

## final state

- current branch가 main인지 확인한다.
- main이 origin/main과 clean/synced인지 확인한다.
- permanent executable metadata/hash를 read-back한다.
- startup registry가 exact permanent path인지 확인한다.
- temporary task worktree와 그 executable이 없거나 persistent runtime과 무관한지 확인한다.
