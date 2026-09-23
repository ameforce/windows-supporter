# 빌드 계약 시험의 onefile 임시 폴더 기대값 hotfix

## 의도와 변경 분류

- 의도한 계약: onefile 실행 파일은 실행 계정의 `%LOCALAPPDATA%\windows-supporter\runtime` 아래에 압축을 푼다. `build.bat`은 이를 `--runtime-tmpdir "%%LOCALAPPDATA%%\windows-supporter\runtime"`로 넘긴다(hotfix v0.31.5, 커밋 `7c87961`).
- 현재 동작: `tests/unit/test_build_deploy_contract.py`가 예전 값인 `--runtime-tmpdir "."`을 여전히 요구해 실패한다. 같은 커밋에 추가된 `tests/unit/test_onefile_temp_root.py`는 `"."`이 없어야 한다고 검사하므로, 두 시험이 서로 모순된다.
- 차이: 계약을 바꾼 커밋이 기존 시험 하나를 함께 고치지 않았다.
- 판정: 의도한 계약에 맞게 오래된 시험을 복구하므로 `hotfix/v0.33.15`로 분류한다. 제품 코드와 빌드 스크립트는 바꾸지 않는다.

## 수정

`test_build_stages_tcltk_runtime_and_validates_frozen_init`이 실행 계정별 임시 폴더 인자를 요구하고, 실행 파일 옆(`"."`) 인자가 없는지 확인하도록 고친다. 주석도 현재 계약의 이유(관리자 실행 때 시스템 임시 폴더 목록 조회 실패, 쓰기 불가 작업 폴더)에 맞춘다.

## 검증 범위

`tests.unit.test_build_deploy_contract`와 `tests.unit.test_onefile_temp_root`를 실행한다. 두 시험이 같은 계약을 검사하고 모두 통과하는지 확인한다.
