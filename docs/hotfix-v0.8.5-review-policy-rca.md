# v0.8.5 리뷰 정책 RCA와 복구

## 변경 분류 기록

- 의도한 계약: final exact base/head에서만 GitHub Codex와 독립 read-only reviewer를 한 번씩 병렬 실행하고, 실제 위험인 P0/P1/P2만 병합을 차단한다.
- 현재 동작: PR #23~#31에서 형식화된 요청 95건, 원문 `@codex review` 댓글 96건, connector 응답 25건이 발생했다. 동일 exact key 중복 4건과 SHA가 깨진 요청 1건도 확인됐다.
- 직접 원인: final self-preflight 전에 매 head 변경마다 재검토를 허용하고, exact key 중복을 금지하거나 connector 오류만 재시도하도록 강제하지 않았다. 별도 PR 검증 workflow와 completion label도 final review와 무관한 순서를 추가했다.
- 차이: head가 안정되기 전 요청이 누적되어 reviewer 실행 횟수와 stale review가 증가했고, P3 처리 기록은 병합 품질과 무관한 운영 부담을 만들었다.
- 판정: 기존 리뷰 보호 계약의 불완전 구현을 복구하는 hotfix다. 사용자 기능, 외부 CLI, 공개 설정 API와 공개 Git 이력은 바꾸지 않는다.

## 복구 계약

1. RCA, red test, 최소 완전 수정, 인접 영향, 관련·전체 테스트, build, 필요한 runtime, self diff와 base 안정화를 먼저 끝낸다.
2. `<base SHA>:<head SHA>` review key마다 GitHub 요청은 한 번만 허용한다. connector의 명시적 오류만 같은 key의 한 번 재시도를 허용한다.
3. final exact base/head에서 GitHub Codex와 `gpt-5.6-sol` high read-only 독립 reviewer를 동시에 시작한다. 두 reviewer의 정보는 격리하고 둘 다 terminal이 될 때까지 head를 바꾸지 않는다.
4. P0/P1/P2는 차단한다. P3은 순수 권고이며 비차단이다. 보안·인증·데이터·설정 무결성, 공개 호환성, 삭제·업데이트·릴리스 무결성, 영향 불확실성은 최소 P2다.
5. 차단 finding은 증상 patch가 아니라 재현 또는 반증, 구조적 원인, red test, 원인 경계의 최소 완전 수정, 인접 영향 검증으로 처리한다. 새 head에서는 두 final review를 새로 수행한다.
6. 별도 PR validation workflow와 completion label은 사용하지 않는다. 테스트·build·runtime·release-chain은 리뷰와 분리된 증거다.

## 검증 범위

- repository tree에서 제거 대상 workflow와 두 이전 gate token은 0건이다.
- GitHub label도 삭제하고 live read-back으로 부재를 확인한다.
- ruleset은 PR-only merge, stale review dismiss, unresolved thread 해소, force-push·deletion 보호만 유지한다. 실제 review를 자동 실행하거나 증명하지 않는다.
