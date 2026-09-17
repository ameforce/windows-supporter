# v0.23.1 Updater progress RCA

## 사용자 계약

자동 업데이트에는 두 프로세스가 있다.

1. 기존 앱이 전용 updater를 준비하고 연결을 확인한다.
2. 전용 updater가 기존 앱 종료, 백업, 다운로드, installer 적용, 검증과 재실행을 수행한다.

각 창은 자기 작업을 실제 하위 단계에 따라 0%부터 100%까지 표시해야 한다. installer가 실행 중인 동안에는 실제 관측 신호가 없다는 이유로 activity 표시가 멈추면 안 된다.

## 원인

- 첫 번째 창은 전체 Release 흐름의 전역 범위 12~18%만 사용했고, helper ACK 전에 parent 종료를 요청했다. 따라서 창은 18%에서 닫혔다.
- 두 번째 창은 Inno Setup에 /LOG를 전달했지만 log file을 소비하지 않았다. 종료를 poll()로 기다리는 동안 progress snapshot은 72%에서 유지됐다.
- installer 구간의 light pulse는 indeterminate mode에만 의존했다. log-driven snapshot이 없어서 사용자는 작업이 살아 있는지 알 수 없었다.

## 교정 설계

- 1/2 준비는 요청 접수, Release 준비, 상태 기록, helper 시작, ACK 확인을 독립 0~100% workflow로 표시한다. parent는 ACK를 받은 뒤에만 종료를 요청한다.
- 2/2 설치는 handoff state의 이전 전역 퍼센트를 재사용하지 않고 0%에서 시작한다.
- installer /LOG는 updater wait loop에서 같은 UI 스레드로 incremental tail 한다. 파일 적용·제거 정보·바로가기·installer 완료 이벤트는 release_install 범위에서만 전진시킨다.
- Inno Setup이 전체 파일 수를 제공하지 않으므로 로그 수신만으로 100%를 주장하지 않는다. 설치 완료 exit와 artifact validation은 별도 후속 단계로 남긴다.
- installer log가 잠시 조용해도 indeterminate pulse와 UI pump는 계속 실행된다. log-driven snapshot도 indeterminate mode를 유지한다.

## 회귀 기준

- 첫 dialog는 ACK 확인 후 100%를 표시하고, 그 뒤 parent 종료를 요청한다.
- 둘째 dialog는 0%에서 시작해 실제 단계와 installer log event에 따라 단조 증가하고 완료 시 100%가 된다.
- installer log activity snapshot은 모두 indeterminate mode를 유지하며, 파일 경로 같은 raw log 내용은 UI detail에 노출하지 않는다.
