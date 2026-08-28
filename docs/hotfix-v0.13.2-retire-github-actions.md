# Hotfix v0.13.2 — GitHub Actions 폐기와 local release evidence 복구

## 변경 분류

- **의도한 계약:** 변경과 직접 관련된 targeted test·native evidence는 task branch에서 한 번 생성하고, 동일 source tree의 main/develop 통합에서는 재사용한다. 릴리스는 clean tagged `main`의 로컬 Windows build, executable metadata·SHA-256, 실제 실행과 exact local/remote ref read-back으로 닫는다.
- **현재 동작:** v0.13.1에서 `AGENTS.md`는 targeted 검증으로 바뀌었지만 계약 테스트가 제거된 과거 문구 두 개를 계속 요구했고, GitHub Actions workflow는 main·tag·develop push마다 1,177개 전체 test discovery와 중복 build를 실행했다. 세 run은 동일한 stale policy assertion 2개로 실패해 불필요한 알림을 만들었다.
- **차이:** repository 정책·계약 테스트·원격 자동화가 서로 모순되고, 사용자가 금지한 무관 전체 테스트가 release push마다 다시 실행된다.
- **판정:** 제품 기능 추가가 아니라 v0.13.1 검증 정책의 누락과 운영 회귀를 복구한다. 공개된 v0.13.1 refs는 재작성하지 않고 다음 patch인 `hotfix/v0.13.2`에서 교정한다.

## 폐기 계약

- GitHub repository Actions permission은 `enabled=false`로 유지한다.
- `.github/workflows` 아래 tracked workflow를 두지 않는다.
- 현재 `AGENTS.md`와 contract test는 GitHub Actions 또는 원격 gate 성공을 릴리스 완료 조건으로 요구하지 않는다.
- 과거 Action run과 역사적 RCA 문서는 당시 사실의 기록일 뿐 현재 실행 계약이 아니다.
- 검증 실패 시 실패한 test와 직접 영향 범위만 다시 실행하며 전체 discovery로 확대하지 않는다.

## 직접 검증 범위

- `tests.unit.test_pr_protection_contract`
- workflow inventory 부재와 repository Actions permission read-back
- `git diff --check`
- 정책·문서·테스트 삭제만 포함하므로 task branch runtime build와 E2E는 실행하지 않는다. tag-derived v0.13.2 최종 artifact는 clean tagged `main`에서 한 번 빌드하고 metadata·SHA-256·실행 경로를 확인한다.
