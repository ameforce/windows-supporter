# v0.9.0 AI Usage Taskbar Display Policy RCA

## 분류 판정

- 의도한 계약(신규): taskbar overlay는 content-fit width만 사용하고, reset countdown은 provider/precision과 무관하게 `DDd HHh MMm SSs` 단일 스키마를 쓴다.
- 현재 동작: wide empty slot inflate로 우측 여백이 생기고, datetime은 `NNh MMm`, date는 `D-N`이다.
- 차이: 제품 표시 정책 도입.
- 판정: `release/v0.9.0`.

## 이슈

| ID | 합리성 | 직접 원인 | 구조 원인 | 수정 |
|---|---|---|---|---|
| #4 width | UX | `_wide_slot_preferred_width` inflate | slot-fill 정책 | content-fit + slot clamp |
| #5+#6 countdown | UX 통일 | precision별 문자열 스키마 | 표시 계층 분기 | `DDd HHh MMm SSs`; date는 `NNd 00h 00m 00s` |

## Track A 의존

`release/v0.9.0`는 Track A tip(`task/fix-ai-usage-cursor-identity-percent`)을 포함해 percent fixture 충돌을 줄인다.
