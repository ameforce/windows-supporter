# Agent 작업 지침

이 레포에서 수정사항이 생기면 아래 순서를 항상 지킨다.

1. 변경 후 정상 동작을 먼저 검증한다.
   - 기본 검증 명령: `uv run python -m unittest discover -s tests -p "test_*.py"`
   - 변경 범위가 명확하면 관련 테스트를 추가로 실행한다.
2. 테스트 통과 후 `@build.bat`(=`build.bat`)를 실행해 새로운 `windows-supporter.exe`를 만든다.
   - 실행 예시: `cmd /c build.bat`
3. 빌드 실패/실행 실패 시 원인을 해결한 뒤, 테스트부터 다시 수행하고 재빌드한다.
