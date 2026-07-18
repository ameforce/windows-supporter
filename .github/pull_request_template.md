## 변경 요약

- 변경 목적과 범위를 적습니다.

## exact-head 리뷰 기록

> 아래 기록은 실제 GitHub review object와 독립 reviewer 결과를 찾기 위한 체크리스트입니다. 작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다.

- [ ] 최종 base ref/SHA와 head SHA: `<base ref> / <40자리 base SHA> / <40자리 head SHA>`
- [ ] GitHub `@codex review` 요청 URL과 결과: `<URL> / review commit_id 또는 zero-finding 댓글의 유일한 Reviewed commit prefix>`
- [ ] 독립 native Codex read-only review 참조와 대상 SHA: `<참조> / <40자리 SHA>`
- [ ] 두 실제 리뷰의 정규화 결과: `P0=0, P1=0, P2=0, P3=0`
- [ ] GitHub unresolved review thread: `0`
- [ ] 위 기록 이후 push나 base 이동이 없으며 두 리뷰가 동일한 최신 base/head를 검토함
- [ ] 위 조건을 확인한 뒤에만 `reviews-complete` label을 붙여 별도 테스트·빌드 CI를 실행함
- [ ] push 또는 base SHA 변경 시 `reviews-complete` label과 기존 CI를 stale 처리하고, 두 리뷰 뒤 label을 제거했다가 다시 붙임

## 별도 검증

- 테스트·정적검사: `<명령과 결과>`
- 빌드·artifact SHA: `<명령과 결과>`
- Windows 실제 실행: `<결과 또는 검증 한계>`

Actions 성공은 테스트·빌드 증거일 수 있지만 리뷰 완료 증거는 아닙니다.
