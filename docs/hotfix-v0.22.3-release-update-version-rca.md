# hotfix/v0.22.3 Release 업데이트 버전 불일치 RCA

## 분류와 의도

- 의도한 계약: 'v0.22.1'에서 'v0.22.2' Release installer를 적용한 뒤 새
  'windows-supporter.exe'가 실행되고, 메인 UI의 표시 버전과 다음 업데이트
  확인의 현재 버전이 모두 'v0.22.2'를 가리켜야 한다.
- 현재 동작: updater는 installer의 exit code와 설치 경로의 파일 존재만 확인한
  뒤 업데이트 완료와 재실행을 기록한다. 설치 대상이 잠겼거나 Release 이름과
  다른 실행 파일을 담은 installer여도 이전 실행 파일이 남을 수 있다.
- 차이: 설치 후 실제 실행 파일의 hash, 'FileVersion', 'ProductVersion',
  'Comments'를 Release candidate와 대조하지 않아 'v0.22.1'이 실행된 상태에서
  'v0.22.2' 팝업을 다시 띄울 수 있다.
- 판정: 새 기능이 아니라 기존 자동 업데이트 계약의 불완전한 성공 판정과
  Release packaging 검증 누락을 복구하는 hotfix다.

사용자 관찰의 '업데이트 직후 UI는 v0.22.1'이라는 사실만으로는 설치 파일이
교체되지 않았는지, 새 프로세스가 단일 인스턴스 mutex에서 종료되어 기존
프로세스가 남았는지를 구분할 수 없다. 현재 구현에는 두 경로가 모두 존재하며,
둘 다 업데이트 성공으로 오인될 수 있으므로 두 경계를 함께 닫는다.

## 근본 원인

1. 'run_release_update_handoff()'가 installer 성공을 프로세스 종료 코드와
   'windows-supporter.exe' 존재 여부만으로 판정했다.
2. 설치 전 백업은 있었지만 설치 후 파일이 실제로 교체됐는지 확인하지 않았다.
3. 설치된 실행 파일의 Windows version resource와 Release tag를 대조하지 않아
   stale/잘못 패키징된 payload가 성공으로 기록될 수 있었다.
4. 기존 프로세스가 종료되지 않아 새 프로세스가 단일 인스턴스 mutex에서
   종료될 수 있는데도 handoff가 installer 실행을 계속했다.
5. 'tools/build_installer.ps1 -Version'은 명시한 이름과 'HEAD'의 exact tag,
   'dist\windows-supporter.exe'의 embedded version이 같은지 강제하지 않았다.

## 개선 내용

- 설치 전 runtime SHA-256을 보존하고, installer 종료 후 SHA-256이 달라졌는지
  확인한다. 그대로면 성공으로 처리하지 않고 기존 runtime을 복원한다.
- 기존 Windows Supporter 프로세스가 종료되지 않으면 installer를 실행하지 않고
  실패시킨다. 이로써 단일 인스턴스 mutex 때문에 새 버전이 즉시 종료되는
  상태를 만들지 않는다.
- 설치 후 'FileVersion', 'ProductVersion', 'Comments'를 읽어 candidate의
  'major.minor.patch'와 모두 대조한다. 표시 버전('Comments')도 검증하므로
  tray/UI와 updater의 version source가 어긋난 payload를 차단한다.
- 검증 결과를 handoff state와 update log의 'installed_artifact'에 남긴다.
- installer 생성 시 exact 'HEAD' tag와 '-Version'을 대조하고, source exe의
  embedded version resource도 같은 Release인지 확인한다. 잘못된 asset은
  게시 전에 실패한다.
- 검증 실패 시 installer를 완료로 기록하거나 새 runtime을 실행하지 않고,
  백업한 이전 runtime을 복원한 뒤 실패 상태를 남긴다.

## 검증

- Release handoff 성공 경로: 새 runtime 교체, version resource 검증, 재실행,
  'installed_artifact' read-back.
- installer가 기존 파일을 그대로 둔 경로: 실패 처리와 이전 runtime 복원.
- installer가 다른 버전의 실행 파일을 설치한 경로: version mismatch 실패와
  이전 runtime 복원.
- 관련 unit test와 'git diff --check'를 실행한다.
