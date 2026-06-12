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
