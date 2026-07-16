# v0.7.7 Codex usage Playwright renderer OOM 및 영구 timeout RCA

## 결론

이번 장애는 두 문제가 결합해 영구화됐다.

1. 동일한 persistent Chrome renderer/page/context를 성공 시 무기한 재사용해, 반복 reload 중 renderer 메모리 증가에 상한이 없었다.
2. renderer OOM 뒤 Playwright sync 호출이 반환하지 않을 때 Python owner thread에는 강제 취소 수단이 없었다. 90초 command timeout은 호출자를 깨웠지만, `page.evaluate`, `context.close`, `page.close`, `playwright.stop` 중 하나에서 멈춘 owner thread와 Node/Chrome transport를 회수하지 못했다.

정확히 어떤 JavaScript 객체가 `ExternalEntityTable` entry를 retain했는지는 증명하지 못했다. 따라서 특정 retained root를 원인으로 단정하지 않고, 반복 재현된 renderer 수명·조회 횟수·메모리 증거를 근거로 수명을 제한하는 방어와 프로세스 hard-cancellation을 적용했다.

## 직접 확인된 사실

- 장애 로그: `C:\Users\epapyrus\AppData\Roaming\windows-supporter\codex-account-1\codex_usage.log`
- 대표 dump: `C:\Users\epapyrus\AppData\Local\windows-supporter\chatgpt-profile-account-1\Crashpad\reports\66a93162-67ac-4ea2-8319-018d8919f976.dmp`
- 2026-07-15~16 dump 5개가 모두 `ptype=renderer`, `V8 process OOM (ExternalEntityTable::AllocateEntry)`였다.
- 5개 renderer의 최대 GC uptime은 약 9,045~9,068초로, 약 2시간 31분에 반복됐다.
- OOM 직전 V8 live heap은 약 2.1~2.2GB, total/allocator는 약 2.28~2.39GB였다.
- 마지막 renderer는 약 2시간 30분 45초 동안 301회 collect에 성공한 뒤 302번째 collect에서 OOM이 발생했다.
- 2026-07-16 01:19:51 collect 시작 후 내부 Playwright exception 없이 01:21:21 owner timeout만 기록됐다. 같은 generation cleanup 대기는 약 9시간 49분 반복됐다.
- 일반 `Page.goto Timeout 30000ms` 9건은 내부 exception과 retry 로그가 남았고 8~194초 안에 복귀했다. 영구 장애와 로그 형태가 달랐다.
- 기존 driver는 하나의 sync Playwright runtime, persistent context, page를 성공 시 계속 재사용했다.
- Playwright 1.58 sync wrapper의 `page.evaluate`, `page/context.close`, `playwright.stop`은 외부에서 해당 Python thread를 죽일 hard timeout을 제공하지 않는다.
- Python thread는 안전하게 강제 종료할 수 없지만, Windows Job Object에 포함된 별도 worker process와 그 descendants는 `TerminateJobObject`로 함께 종료할 수 있음을 이 호스트에서 재현했다.

## 고확률 추론과 미확정 부분

### 고확률 추론

- 약 2시간 31분의 반복 수명, 301회 성공 뒤 동일 OOM, 2GB대 heap은 장수 renderer에서 누적되는 자원을 bounded lifecycle로 제한해야 함을 강하게 지지한다.
- OOM 직후 내부 exception 없이 owner가 반환하지 않은 현상은 renderer/Node transport 또는 cleanup sync 호출이 protocol 응답을 기다린 상태와 일치한다.
- 같은 `profile_dir`를 새 persistent context에 전달하면 디스크의 로그인 profile을 재사용할 수 있다. 실제 Chrome 통합 테스트에서 worker 강제 종료 뒤 localStorage와 persistent cookie가 보존됐다. `Expires`/`Max-Age`가 없는 HttpOnly session cookie는 성공 응답 때 부모 메모리에 별도 snapshot하고 새 worker에 복원해야 보존됐다.

### 미확정

- `manifest-afefca30.js`가 OOM 당시 실행 중이었다는 사실만으로 해당 script를 원인으로 단정할 수 없다.
- 정확한 retained root와 누적 객체 종류는 dump만으로 입증하지 못했다.
- RSS와 V8 heap은 같은 지표가 아니다. RSS guard는 보조 신호이고 count/age guard가 독립적으로 유지된다.
- 페이지 교체는 DOM/JS execution context 상태를 격리하지만 Chromium renderer process 자체 교체를 보장하지 않는다. renderer 메모리의 확실한 reset 경계는 worker/Job recycle이다.

## 구현 구조

```text
CodexUsagePlaywrightSession owner thread
  -> CodexUsagePlaywrightProcessDriver
      -> spawn worker (Pipe 첫 bootstrap 대기)
          -> Windows Job Object 할당 완료
              -> CodexUsagePlaywrightDriver
                  -> Playwright Node + installed Chrome descendants
```

- `multiprocessing.freeze_support()`를 GUI/Monitor import보다 먼저 실행해 PyInstaller onefile spawn 재진입을 처리한다.
- child는 Pipe의 bootstrap을 받기 전 Playwright driver를 만들지 않는다. 부모가 Job 할당에 실패하면 Chrome을 시작하지 않고 fail closed한다.
- worker는 기존과 같은 계정별 `profile_dir`를 사용한다. profile 삭제나 전역 Chrome 검색/종료는 하지 않는다.
- command timeout 시 부모는 해당 worker Job만 종료한다. root worker 종료와 `Job.ActiveProcesses == 0`을 모두 확인한 뒤에만 같은 profile로 새 owner/worker를 시작한다. Job이 비지 않으면 profile handoff를 금지하고 circuit/app restart fallback으로 전환한다.
- 부모는 성공한 browser command 뒤 session-only cookie만 메모리에 snapshot한다. worker 교체 뒤 같은 persistent profile을 연 다음 이 cookie를 복원하며, cookie 값은 로그나 디스크에 별도로 남기지 않는다.
- 앱 전체 restart는 Job 경계 자체가 실패한 최후 fallback으로만 남는다.

## lifecycle 정책

| 경계 | 기본값 | 근거 |
|---|---:|---|
| headless page 교체 | 성공 25회 | 30초 주기에서 약 12.5분. context/Chrome 재시작 없이 page의 DOM/JS 상태를 격리한다. renderer process reset으로 간주하지 않는다. |
| worker/context 교체 | 성공 100회 | 장애의 301회보다 약 3배 이른 상한. 30초 주기에서 약 50분이다. |
| worker/context 최대 수명 | 3,600초 | 반복 OOM 수명 약 9,050초의 약 40% 지점이다. 조회 주기가 길어도 시간 상한을 유지한다. |
| worker tree 단일 process 최대 RSS | 1.5GiB | 새 renderer 실측 약 1.02GB와 OOM dump의 2GB대 지표 사이의 조기 guard다. V8 heap과 직접 등가로 해석하지 않는다. |
| headed login 최대 유예 | 7,200초 또는 단일 process RSS 2GiB | 사용자의 로그인 창을 정상 수집보다 우선 보존하되, 방치된 login이 lifecycle 방어를 무기한 우회하지 못하게 한다. |
| cleanup hard deadline | 5초 | 정상 종료 기회를 주되 `context.close`/`playwright.stop`이 owner를 영구 점유하지 못하게 한다. |

세 조건(count, age, RSS) 중 먼저 도달한 것이 worker/context recycle을 요청한다. headed login 창이 열린 동안 일반 planned recycle만 유예하되 2시간 또는 2GiB emergency cap은 유지한다. crash, timeout, transport EOF는 headed 상태와 무관하게 즉시 hard recycle한다.

## 관측 가능성

로그에 다음을 남긴다.

- `stage`: `playwright_start`, `context_launch`, `navigation`, `evaluate_user_agent`, `evaluate_probe`, `crash_probe`, `page_close`, `context_close`, `playwright_stop`
- stage start/end, `elapsed_ms`, 성공/오류 outcome
- owner generation, process generation, worker PID, context/page generation
- successful collect count, age, process별 최대/전체 RSS와 private bytes
- page crash signal, transport close, recycle/terminate reason, 종료 후 worker alive/Job empty 상태

stage start만 있고 end가 없으면 hard timeout 당시 비복귀 seam을 식별할 수 있다. page crash event가 도착한 뒤 다른 sync 호출이 멈춰도 부모가 마지막 crash signal을 `renderer_crashed`로 보존한다.

## 검증

- 실제 installed Chrome `chrome://crash`로 renderer crash event와 `renderer_crashed` 분류를 확인했다.
- 실제 installed Chrome에서 `/probe` 응답을 끝내지 않아 `page.evaluate`를 비복귀 상태로 만들었다. deadline 뒤 worker tree와 descendants가 0개가 된 후 새 worker가 같은 profile의 localStorage, persistent cookie, HttpOnly session-only cookie를 읽어 정상 collect했다.
- session-only cookie는 정상 success-count recycle과 hard-kill retry 양쪽에서 보존됨을 실제 HTTP `Set-Cookie`로 확인했다.
- 가짜 worker가 descendant process를 만든 뒤 transport에서 멈추도록 해 Job 종료가 worker와 descendant를 모두 제거함을 확인했다.
- `context.close` 및 `playwright.stop` 비복귀를 각각 재현해 cleanup deadline 안에 종료되는지 확인했다.
- 성공 횟수 기반 page recycle과 worker recycle, count/age/RSS 정책, headed login 유예를 테스트했다.
- PyInstaller build는 frozen spawn과 Job termination smoke를 실행한다.

## 검토한 대안

- 앱 restart/circuit만 강화: 가용성은 회복하지만 OOM을 예방하지 않고 정상 앱 상태까지 버린다. 최후 fallback으로만 유지한다.
- Python owner thread 추가 또는 async Playwright 전환: Node protocol/cleanup 자체가 반환하지 않으면 같은 process 안의 task/thread cancellation으로 강제 회수할 수 없다.
- 매 collect마다 Chrome 재기동: 가장 강한 수명 제한이지만 30초 주기 비용이 과도하고 로그인 UX가 불필요하게 흔들린다.
- raw CDP/remote debugging 또는 이름 기반 Chrome kill: 기존 architecture 경계를 깨고 사용자 Chrome을 오종료할 위험이 있어 사용하지 않는다.

## 남은 위험

- upstream ChatGPT/Chrome/Playwright 변화로 메모리 증가율이 바뀔 수 있다. 새 로그의 recycle reason과 RSS를 관찰해 임계값을 조정해야 한다.
- 부모가 한 번도 성공 응답을 받아 snapshot하지 못한 session-only cookie, IndexedDB/service worker의 순수 메모리 상태, 실제 ChatGPT 인증 전체 조합은 별도 장기 검증이 남는다. 현재 구현은 확인된 session cookie를 worker 교체 사이에 메모리로 보존한다.
- 실제 `ExternalEntityTable::AllocateEntry` OOM과 outstanding sync 호출을 한 테스트에서 동시에 재현하지는 않았다. `chrome://crash`, 실제 비복귀 evaluate, transport/cleanup hang을 각각 검증했으며 crash event는 복구의 필수 조건으로 사용하지 않는다.
- Job Object 할당이 시스템 정책으로 거부되는 환경에서는 fail closed하고 기존 앱 restart fallback이 작동한다. 해당 환경을 일반 성공으로 보고하지 않는다.
