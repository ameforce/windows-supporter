# Google Calendar 설정 이전 시험의 버전 기대값 복구

## 분류와 계약

- 의도: v8 설정 파일은 시작할 때 이전 Google 인증 키(`vacation_google_oauth_protected`, `vacation_google_oauth_delete_pending`)를 공용 키(`google_calendar_oauth_*`)로 옮긴다. 해석할 수 없는 암호문도 잃지 않고 그대로 옮기며, 파일에는 현재 설정 스키마 버전을 기록한다.
- 기존 동작: v0.25.2(`63311b4`, Flex 브라우저 세션 전환)가 `Wrike`의 설정 스키마 버전을 9에서 10으로 올렸다. 제품 코드는 위 이전을 수행하고 10을 기록하지만, `tests/unit/test_google_calendar_unification.py`의 두 시험은 계속 9를 기대해 실패했다.
- 차이: 제품 동작은 의도와 같고 검증 기대값만 낡았다.
- 판정: 의도된 동작을 지키는 검증의 누락을 복구하므로 `hotfix/v0.36.1` 차선에서 고친다. 새 기능은 없다.

## 수정 범위

- `test_settings_v8_legacy_key_and_v1_envelope_migrate_to_shared_v9`와 `test_undecodable_legacy_ciphertext_is_renamed_without_loss`가 이전 후 버전 10을 기대하도록 고친다.
- Google Calendar 이전의 나머지 단언(공용 키 이전, 이전 키 제거, 인증 묶음 형식 2 변환, 휴가 역할 보존, 해석할 수 없는 암호문 보존)은 그대로 둔다.
- 제품 코드는 바꾸지 않는다. v8 판정(`version < 9`)은 파일에 저장된 원래 버전으로 계산하므로 버전 10에서도 같은 이전이 일어난다.
- 시험 이름의 `v9`는 공용 Google Calendar 설정이 도입된 버전을 뜻하므로 그대로 둔다.

## 검증 범위

대상 모듈 `tests.unit.test_google_calendar_unification`만 실행한다. 제품 코드와 화면이 바뀌지 않으므로 실제 창 시험, 전체 시험과 작업 단계 빌드는 실행하지 않는다. 배포 산출물은 태그에서 한 번 만든다.
