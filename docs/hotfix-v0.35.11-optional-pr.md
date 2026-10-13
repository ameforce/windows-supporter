# 구형 PR 강제 지침 폐기

- 의도한 계약: 사용자의 현재 운영 기준은 PR을 팀원 리뷰가 필요할 때만 선택하고 생성 전에 승인받는 것이다.
- 기존 동작: 저장소 지침과 원격 ruleset은 모든 version branch 통합에 PR을 강제했다.
- 차이: 사용자가 구형이라고 명시한 규칙이 불필요한 PR과 확인 단계를 만들었다.
- 판정: 현재 운영 계약과 충돌하는 구형 지침의 복구이므로 진행 중인 v0.35.11 hotfix와 호환된다.

PR 강제만 폐기한다. 버전 분류, task branch, 독립 검토, 필요한 시험, merge 부모 확인, 공개 ref 재작성 금지와 삭제 보호는 유지한다. 직접 통합은 확인한 task head를 version branch에 no-ff merge하고 원격 tip을 다시 읽는다. PR을 선택하면 기존 expected-head merge와 MERGED 확인을 적용한다.

태그의 정리 근거는 PR 없이도 base, head와 실제 merge commit으로 확인한다. 과거 릴리스 문서와 태그는 역사적 근거로 보존한다. 정책 변경 검증은 직접 contract test, diff check, 독립 검토와 원격 ruleset read-back만 수행한다.
