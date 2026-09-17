# hotfix/v0.25.11 installer 업데이트·자동 종료 RCA

## 범위와 분류

- 사용자 요청은 Release installer 업데이트가 선택한 버전을 설치하고, 설치
  중 Windows Supporter와 그 작업자를 종료한 뒤 새 버전을 실행하는 기존
  계약을 복구하는 것이다.
- 첨부 이미지는 현상 증거로만 사용했다. 이미지 안의 Setup 안내 문구를
  별도의 사용자 요구사항으로 확장하지 않았다.
- 현재 lane의 의도에 없던 capability를 추가하지 않고 기존 자동 업데이트
  성공 판정과 프로세스 종료 경계를 복구하므로 `hotfix/v0.25.11`으로
  분류한다.

## 의도한 계약 → 현재 동작 → 차이 → 판정

### 의도한 계약

1. 업데이트 helper는 설치 대상 `windows-supporter.exe`와 그 frozen child
   worker를 모두 종료한다.
2. installer는 파일 잠금이 해소된 경우에만 실행 파일을 즉시 교체한다.
   잠긴 파일을 재부팅 교체로 예약하고 성공으로 넘기지 않는다.
3. 설치 후 hash, Windows version resource, 새 프로세스의 readiness와
   heartbeat를 검증한 뒤에만 업데이트 완료를 기록한다.

### 현재 동작

- helper는 `source_pid` 하나의 종료만 기다리고, 부모 PID와 분리되었거나
  frozen executable을 직접 사용하는 worker까지 exact-path로 확인하지
  않았다.
- Inno Setup은 `CloseApplications=yes`였다. 이는 Restart Manager의 정상
  종료 요청이므로 hidden Tk root와 multiprocessing worker가 남을 수 있다.
- `[Files]`의 `restartreplace`는 잠긴 파일을 다음 재부팅에 교체하도록
  예약하는 fallback이다. 자동 updater가 기대하는 즉시 hash/version 검증과
  충돌한다.

### 차이와 root cause chain

1. 실행 중인 frozen child가 설치 대상 exe를 계속 사용한다.
2. 정상 종료 요청만으로는 hidden/worker process가 종료되지 않는다.
3. Inno Setup이 파일을 즉시 교체하지 못하면 이전 exe가 남거나 재부팅 교체가
   예약된다.
4. 이전 버전이 다시 실행되어 다음 Release가 계속 newer로 관측되고, 사용자는
   무한 업데이트로 보게 된다.

근본 원인은 installer 한 줄의 옵션만이 아니라, updater의 `source_pid` 단일
관찰과 installer의 graceful-close/restart-replace fallback이 서로 다른
프로세스·파일 교체 계약을 사용한 것이다.

## 교정

- `run_release_update_handoff()`는 installer 실행 직전에 설치 대상 경로와
  일치하는 모든 PID를 `WindowsRuntimeProcessController`로 찾고 process tree를
  종료한다. updater copy는 다른 exact path이므로 대상에 포함하지 않는다.
- 종료 후 exact-path 재조회를 수행한다. 남은 PID가 있으면 installer를
  실행하지 않고 실패한다.
- 실패 복구 전에도 같은 exact-path 종료·재확인을 수행하며, 종료를 증명하지
  못하면 백업을 덮어쓰거나 이전 runtime을 재실행하지 않는다.
- Inno Setup을 `CloseApplications=force`로 바꾸고 자동 handoff에
  `/FORCECLOSEAPPLICATIONS`와 `/NORESTARTAPPLICATIONS`를 명시한다.
- `restartreplace`를 제거해 잠긴 파일을 재부팅 교체로 성공 처리하지 않고
  installer 실패로 반환하게 한다. updater는 그 결과를 hash/version 검증과
  함께 실패·복구 경로로 처리한다.
- installer 실패로 이전 runtime을 복구해 실행할 때 실패한 후보 tag를 one-shot
  환경값으로 전달한다. 다음 자동 확인은 그 후보를 한 번 건너뛰고, 사용자가
  수동 확인을 요청하면 같은 후보를 다시 시도할 수 있다. 따라서 복구 직후
  동일 후보를 즉시 다시 띄우는 자동 업데이트 loop도 차단한다.

## 회귀 기준과 검증 경계

- exact-path로 여러 PID를 종료하고 종료 후 다시 0개임을 검증한다.
- 하나라도 남으면 installer launch를 차단한다.
- release handoff가 새 installer flags, shutdown receipt, artifact metadata와
  readiness 검증을 함께 기록하는지 확인한다.
- installer script가 force-close를 사용하고 `restartreplace`를 포함하지
  않는지 구조 검증한다.
- 변경은 updater/installer 계약에 한정했으므로 무관한 전체 E2E, 실제 Tk
  UI launch, permanent runtime 교체는 task validation에서 실행하지 않는다.
  clean tagged release closure build와 실제 Windows installer smoke는
  version lane을 main/tag로 닫을 때 별도 runbook 단계에서 수행한다.
