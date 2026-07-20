# v0.8.6 AI Usage Identity / Percent Hotfix RCA

## 분류 판정

- 의도한 계약: `label_mode=auto`는 provider가 수집한 안정적 `profile_name`을 우선 표시하고, taskbar compact `%`는 provider 공통 정수 표기다.
- 현재 동작: Cursor에 `profile_name` 수집 경로가 없어 auto가 stale/`Codex N` fallback으로 떨어지고, `%`는 `:g%`로 float 정밀도가 그대로 노출된다.
- 차이: 미완성/누락 및 표시 계약 미고정.
- 판정: `hotfix/v0.8.6`.

## 이슈

| ID | 합리성 | 직접 원인 | 구조 원인 | 수정 |
|---|---|---|---|---|
| #1+#2 Cursor 표시명 | 합리적 | Cursor probe/monitor가 `profile_name`을 넣지 않음 | identity가 Codex 전용으로 남음 | Cursor probe `profileName` → runtime → auto label; 부재 시 `Cursor N` |
| #3 compact % | 합리적 | `short_value_text`가 `:g%` | taskbar 정밀도 계약 없음 | `int(round(percent))%` |

## 검증 한계

- 실 Cursor DOM/API 표시명 후보는 fixture로 잠근다. live 계정 확인은 자격 증명 가능 시 수행한다.
