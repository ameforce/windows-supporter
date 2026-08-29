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
- `build.bat`는 running root executable을 중단하고 새 artifact를 promote할 수 있으므로 실행 전에 이를 알린다.
- policy/docs/ref/worktree-only release에서는 child environment에 `WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1`을 전달해 post-build 앱 launch를 막는다.
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
