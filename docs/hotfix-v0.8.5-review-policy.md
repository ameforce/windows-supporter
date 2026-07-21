# v0.8.5 자문·리뷰 정책 RCA

## 범위와 직접 증거

- 조사 기준은 clean `main`의 `824dd23f4bc557c9f1590ff4efc4f0d6b4d2c4a8`이다.
- 공개 PR #32는 `CLOSED`, `mergedAt=null`인 폐기 감사 기록으로만 확인했으며 해당 branch, commit, finding, 테스트, 수치, 구현은 현재 근거로 사용하지 않았다.
- 현재 저장소에는 별도 PR validation workflow와 label 기반 gate가 남아 있었고, global 정책은 계획 전·구현 후 자동 자문과 Terra final reviewer를 요구했다.
- 의도한 계약은 구체적 미해결 결정이 있을 때만 자문하고, 독립 완료 가능한 일반 subagent만 사용하며, 완성된 exact head를 Sol high read-only reviewer가 한 번 검토하는 것이다.

## 원인과 수정 경계

직접 원인은 자문 trigger, 일반 delegation, final review가 하나의 넓은 자동 품질 gate로 결합된 것이었다. 구조적 원인은 repo 계약과 global skill의 역할 분리가 없어서 미완성 head에도 반복 review를 시작하고, 별도 validation과 P3 기록 의무가 merge를 다시 막은 것이다.

수정 경계는 다음으로 제한한다.

- global `AGENTS.md`, `consult-latest-chatgpt`, `route-codex-work`
- repo `AGENTS.md`, PR template, review contract test
- 폐기 승인된 별도 PR validation workflow와 GitHub label 제거

제품 코드, 공개 API, 설정, ruleset, `release-chain-gate`, WSL, 원격 호스트, plugin cache 원본은 변경하지 않는다.

## final review 불변조건

- 미완성 head에는 final review를 요청하지 않는다.
- Cursor/local에서는 독립 reviewer를 먼저 시작하고, `P0=P1=P2=0`일 때만 `@codex review`를 호출한다. 두 리뷰의 finding/결론은 서로 전달하지 않는다.
- 각 review가 terminal이 되기 전에는 그 exact head를 바꾸지 않는다.
- 동일 review key를 중복 사용하지 않는다. connector 명시 오류만 같은 key 1회 재시도를 허용한다.
- 새 finding으로 head가 바뀌면 main Codex가 RCA와 전체 검증을 다시 끝낸 후 새 key에서 독립 review를 먼저 반복하고, 통과한 뒤에만 `@codex review`를 반복한다.

## raw finding 판정 사례

### 증상 patch

Raw finding: "이 조건문 줄에 예외를 하나 더 넣으면 실패가 사라진다."

판정: 증상 줄만 고치는 patch는 거부한다. 실제 재현, 직접·구조적 원인, 영향과 인접 실패 경로, red test가 없으므로 유효한 수정 근거가 아니다.

### 근본 원인 수정

Raw finding: "동일 review key가 두 실행 경로에서 생성되어 중복 dispatch된다. 두 경로가 공유하는 key 생성 경계에서 중복을 차단하고 red test로 두 호출을 재현했다."

판정: 근본 원인 수정은 수용한다. 공통 원인 경계의 최소 완전 수정과 불변조건·실패 모드·side effect 검증을 요구한다.

### 무효 finding 반증

Raw finding: "P3을 기록하지 않으면 merge gate가 실패한다."

판정: 무효 finding은 직접 반증한다. 계약 테스트와 실제 merge 조건이 `P0=0, P1=0, P2=0`이며 P3 기록 의무가 없음을 증명하면 코드를 바꾸지 않는다.

### P3 미수정

Raw finding: "문단 순서를 바꾸면 조금 더 읽기 쉽다."

판정: P3 미수정은 허용한다. 순수 권고는 비차단이며 처분·owner·만료일·후속 이슈를 요구하지 않는다.
