## Goal
- 현재 `windows-supporter` 작업 폴더를 GitHub 저장소 `https://github.com/ameforce/windows-supporter` 에 연결한다.
- 원격 `main` 의 기존 `README.md` / `LICENSE` 히스토리를 유지하면서, 로컬 프로젝트를 본체로 통합한다.
- 이후 이 폴더에서 일반적인 `git add` / `git commit` / `git push` 흐름이 가능한 상태를 만든다.

## Scope / Non-goals
- Scope
  - 로컬 폴더의 git 초기화 및 `origin` 연결
  - 원격 `main` fetch 및 히스토리 보존 통합
  - 업로드 제외 대상이 추적되지 않도록 ignore 정책 보강
  - 최종 상태 검증(`git status`, 테스트, `build.bat`)
- Non-goals
  - force push
  - 산출물/개인환경 파일 커밋
  - 앱 런타임 기능 변경

## Constraints / Risks
- 현재 폴더는 아직 `.git` 이 없어 먼저 로컬 git repository 초기화가 필요하다.
- 원격 저장소는 빈 저장소가 아니라 `main` 에 최소 1 commit(`README.md`, `LICENSE`)이 있다.
- 잘못된 ignore 설정은 개인환경 파일 또는 산출물을 추적 대상으로 만들 수 있다.
- 인증 상태에 따라 최종 `git push` 는 외부 자격증명 blocker가 될 수 있다.
- repo 지침상 파일 수정이 있으면 테스트 후 `build.bat` 검증이 필요하다.

## Validation commands
- `uv run python -m unittest discover -s tests -p "test_*.py"`
- `cmd /c build.bat`
- `cmd /c git status --short`
- `cmd /c git remote -v`
- `cmd /c git log --oneline --decorate --graph -5`

## Completed
- deep-interview로 요구사항/비목표/의사결정 경계를 고정했다.
- GitHub 저장소가 public `main` + `README.md` / `LICENSE` 최소 히스토리 상태임을 확인했다.
- 현재 작업 폴더가 아직 git repository가 아님을 확인했다.

## Remaining
- 실행용 planning artifact(`.omx/plans/prd-*`, `test-spec-*`) 작성
- `.gitignore` 가 사용자 비목표를 충분히 반영하는지 보강
- 로컬 git 초기화, 원격 연결, `main` fetch/통합
- 테스트 및 `build.bat` 실행
- 최종 git 상태 및 히스토리 검증
- architect 검토, deslop, 재검증, 상태 정리

## Next action
- `.omx/plans/prd-connect-project-to-github.md` 와 `test-spec-connect-project-to-github.md` 를 만들고, `.gitignore` / git 연결 작업으로 이어간다.

## Decision log
- remote 이름은 `origin`, 기본 브랜치는 `main` 으로 고정한다.
- force push 는 허용하지 않는다.
- 원격 `README.md` / `LICENSE` 는 유지하며 통합한다.
- 충돌/선택이 생기면 로컬 프로젝트 내용을 본체로 본다.

## Open issues / Follow-ups
- 최종 `git push` 시 자격증명 필요 여부는 실행 시점에 확인한다.
- 원격 `README.md` 내용과 로컬 프로젝트 설명이 어색하면 후속 정리 커밋이 필요할 수 있다.

## Ignore / Out-of-scope files
- `.venv/**`
- `build/**`
- `dist/**`
- `.idea/**`
- `.cursor/**`
- `.omx/**`
- `clipboard_dumps/**`
- `windows-supporter.exe`
