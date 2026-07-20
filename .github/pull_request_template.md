## 변경 요약

- 변경 목적과 범위를 적습니다.

## RCA 및 self-preflight

> 아래 preflight가 끝나기 전에는 final reviewer를 시작하지 않습니다.

- [ ] RCA 재현 또는 반증, 직접·구조적 원인, 영향 범위와 인접 실패 경로를 기록함
- [ ] red test, 원인 경계의 최소 완전 수정, 불변조건·사이드 이펙트 테스트를 완료함
- [ ] 관련·전체 테스트, build, 필요한 Windows runtime, self diff와 base SHA 안정화를 확인함

## final exact-head 리뷰 기록

> 작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다. `review_key`는 `<40자리 base SHA>:<40자리 head SHA>`이고, 같은 key의 GitHub 요청은 한 번만 허용합니다.

- [ ] 최종 base ref/SHA와 head SHA, `review_key`: `<base ref> / <40자리 base SHA> / <40자리 head SHA> / <review_key>`
- [ ] GitHub `@codex review` 요청과 결과 — URL: `<URL>`; connector 명시 오류에만 같은 key 1회 재시도; review `commit_id` 또는 zero-finding 댓글의 유일한 `Reviewed commit` prefix
- [ ] 독립 Codex `gpt-5.6-sol` high read-only review 참조와 대상 SHA: `<참조> / <40자리 SHA>`
- [ ] 두 리뷰를 동시에 시작했고 서로의 요청·중간 결과·결론을 전달하지 않았으며, 둘 다 terminal이 될 때까지 head를 변경하지 않음
- [ ] 두 실제 리뷰의 병합 차단 결과: `P0=0, P1=0, P2=0`; P3은 순수 권고·비차단
- [ ] GitHub unresolved review thread: `0`
- [ ] push 또는 base/head SHA 변경 시 이전 리뷰를 stale 처리하고, 새 self-preflight 뒤 새 `review_key`에서 다시 확인함

## 별도 검증

- 테스트·정적검사: `<명령과 결과>`
- 빌드·artifact SHA: `<명령과 결과>`
- Windows 실제 실행: `<결과 또는 검증 한계>`

Actions 성공은 테스트·빌드 증거일 수 있지만 리뷰 완료 증거는 아닙니다.
