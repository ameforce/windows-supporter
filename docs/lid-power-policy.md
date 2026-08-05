# 상태형 클램쉘 전원 정책

이 기능은 기본 비활성화이며 Windows가 실제로 보고하는 전원 기능으로만 지원 여부를 판정한다.

## 변경 분류 기록

- 의도한 계약: 기존 원격 세션 지원과 무관하게, 지원 노트북에서 사용자가 명시적으로 활성화한 경우에만 덮개와 AC/DC 실제 전이를 상태형으로 처리한다.
- 현재 동작: 기존 버전에는 전원 capability 판정, lid/ACDC notification 상태 머신, 전원 정책 lease와 watchdog이 없었다.
- 차이: 정적 Windows lid action만으로는 덮개를 닫은 순서와 AC에서 시작한 클램쉘 세션의 AC→DC 연속성을 구분할 수 없었다.
- 판정: 기존 의도에 없던 사용자 기능과 전원 정책을 도입하므로 hotfix가 아닌 `release/v0.10.0`이다.

## 지원 조건

`GetPwrCapabilities()` 결과가 아래 조건을 모두 만족해야 한다.

- `LidPresent`
- `SystemBatteriesPresent`
- `!BatteriesAreShortTerm`

기기 이름, `PCSystemType`, `ChassisTypes`는 정책 gate가 아니다. capability 조회 실패, 덮개 스위치 부재, 데스크톱, UPS형 단기 배터리는 notification 등록·전원 정책 변경·절전 요청을 하지 않는다.

## 상태 전이

- 앱 시작, resume, notification 등록 직후의 최초 lid/ACDC 값은 hydration이며 전환이 아니다.
- DC에서 실제 `open -> closed`가 발생하면 절전을 요청한다.
- AC에서 실제 `open -> closed`가 발생하면 AC 클램쉘 세션을 시작한다.
- 이 세션에서는 DC lid action을 임시로 `Do nothing`으로 바꾸므로 덮개가 닫힌 채 `AC -> DC`가 되어도 세션을 유지한다.
- lid open은 세션을 종료하고 원래 DC lid action을 즉시 복원한다.
- RDP 연결과 해제는 상태 머신 입력이 아니며 절전 조건이 아니다.
- 중복 이벤트는 무시하며 resume 후에는 다시 hydration한다.

## 전원 정책 lease와 안전 경계

활성 전원 구성표의 기존 AC/DC lid action을 lease journal에 먼저 기록한 뒤 AC action을 `Do nothing`으로 설정한다. DC action은 AC 클램쉘 세션 동안만 임시로 `Do nothing`이며 나머지 시간에는 원래 값이다.

다음 경계에서 백업 값을 복원한다.

- 기능 비활성화
- lid open
- 저배터리(15% 이하)
- notification 또는 policy API 실패
- 정상 종료
- 활성 전원 구성표 변경
- 비정상 종료를 감지한 hidden watchdog
- 다음 앱 시작 시 발견한 stale lease

watchdog는 별도 PowerShell이나 터미널이 아니라 동일한 frozen 실행 파일의 `--lid-power-watchdog` hidden 모드다. 부모 프로세스가 종료될 때 journal의 lease ID를 확인한 뒤 원래 설정을 복원한다. 종료 시점에 활성 클램쉘 세션이 남아 있으면 이미 닫힌 덮개의 정책 재평가 여부에 의존하지 않고 절전을 추가로 요청한다. watchdog까지 강제 종료되거나 Windows 자체가 중단된 동안에는 즉시 복구를 보장할 수 없으므로, 다음 시작의 stale-journal 복구를 마지막 방어선으로 사용한다.

저배터리 notification이 AC 클램쉘 세션의 DC 상태에서 15% 이하를 보고하면 DC 정책을 먼저 복원한 뒤 절전을 요청한다. 복구를 증명할 수 없는 오류에서는 기능 설정을 비활성화하고 새 클램쉘 세션을 만들지 않는다.
