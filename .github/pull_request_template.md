## 변경 요약

- 변경 목적과 범위를 적습니다.

## exact-head 리뷰 기록

> 아래 기록은 실제 GitHub review object 또는 zero-finding connector 결과와 독립 reviewer 결과를 찾기 위한 체크리스트입니다. 작성자가 체크하거나 0을 적은 사실 자체는 리뷰 증거가 아닙니다.

- [ ] 최종 base ref/SHA와 head SHA: `<base ref> / <40자리 base SHA> / <40자리 head SHA>`
- [ ] GitHub `@codex review` 요청과 결과 — URL: `<URL>`; review `commit_id` 또는 zero-finding 댓글의 유일한 `Reviewed commit` prefix
- [ ] 독립 native Codex read-only review 참조와 대상 SHA: `<참조> / <40자리 SHA>`
- [ ] 두 실제 리뷰의 병합 차단 결과: `P0=0, P1=0, P2=0`
- [ ] P3 처분: `해당 없음` 또는 finding ID별 `수정/기각/위험수용/후속 이슈`; exact base/head, 근거·검증·책임자를 기록함. 기각에는 반증, 위험수용에는 **merge 시점에도 유효한 미래 만료일**, 후속 이슈에는 URL·owner·milestone·완료 조건을 기록함. 만료된 위험수용은 stale·미완료이며 새 처분 또는 P2 재분류가 필요함.
- [ ] GitHub unresolved review thread: `0`
- [ ] 위 기록 이후 push나 base 이동이 없으며 두 리뷰가 동일한 최신 base/head를 검토함
- [ ] push 또는 base/head SHA 변경 시 기존 리뷰와 P3 처분을 stale 처리하고 새 exact base/head에서 다시 확인함

## 별도 검증

- 테스트·정적검사: `<명령과 결과>`
- 빌드·artifact SHA: `<명령과 결과>`
- Windows 실제 실행: `<결과 또는 검증 한계>`

Actions 성공은 테스트·빌드 증거일 수 있지만 리뷰 완료 증거는 아닙니다.
