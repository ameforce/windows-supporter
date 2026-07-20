## 변경 요약

- 변경 목적과 범위를 적습니다.

## exact-head 리뷰 기록

> 아래 기록은 실제 GitHub review object 또는 zero-finding connector 결과와 독립 reviewer 결과를 찾기 위한 체크리스트입니다. 작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다.

- [ ] 최종 base ref/SHA와 head SHA: `<base ref> / <40자리 base SHA> / <40자리 head SHA>`
- [ ] main Codex가 RCA·red test·인접 경로·관련/전체 테스트·build·필요 runtime·자체 diff·base 안정화를 완료해 완성된 head로 판정함
- [ ] review key: `<repo>:<base SHA>:<head SHA>:<round>`; 같은 key 중복 요청 없음, connector 명시 오류일 때만 같은 key 1회 재시도
- [ ] GitHub `@codex review` 요청과 결과 — URL: `<URL>`; review `commit_id` 또는 zero-finding 댓글의 유일한 `Reviewed commit` prefix
- [ ] 독립 `gpt-5.6-sol` reasoning `high` read-only review 참조와 대상 SHA: `<참조> / <40자리 SHA>`
- [ ] 두 final review를 같은 exact base/head에서 동시에 시작했고 결과를 격리했으며 둘 다 terminal 전에는 head를 바꾸지 않음
- [ ] 두 실제 리뷰의 병합 차단 결과: `P0=0, P1=0, P2=0`
- [ ] P3는 순수 권고·비차단이며 선택 수정하지 않은 P3 때문에 merge를 막지 않음
- [ ] GitHub unresolved review thread: `0`
- [ ] 위 기록 이후 push나 base 이동이 없으며 두 리뷰가 동일한 최신 base/head를 검토함
- [ ] push 또는 base/head SHA 변경 시 기존 리뷰를 stale 처리하고 main Codex의 완성 판정 뒤 새 review key에서 두 review를 다시 수행함

## 별도 검증

- 테스트·정적검사: `<명령과 결과>`
- 빌드·artifact SHA: `<명령과 결과>`
- Windows 실제 실행: `<결과 또는 검증 한계>`

Actions 성공은 테스트·빌드 증거일 수 있지만 리뷰 완료 증거는 아닙니다.
