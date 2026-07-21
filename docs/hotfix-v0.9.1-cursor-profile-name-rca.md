# v0.9.1 Cursor Profile Name Harvest Hotfix RCA

## 분류 판정

- 의도한 계약: `label_mode=auto`는 Cursor dashboard에서 수집한 안정적 `profile_name`을 우선 표시하고, 부재 시에만 `Cursor N` fallback을 쓴다.
- 현재 동작(수정 전): usage summary scrape는 READY인데 `profile_name`이 비어 taskbar에 `Cursor 2`가 유지됐다.
- 차이: identity harvest 누락.
- 판정: `hotfix/v0.9.1`.

## 이슈

| ID | 합리성 | 직접 원인 | 구조 원인 | 수정 |
|---|---|---|---|---|
| #1 Cursor 표시명 | 합리적 | `User menu` chrome만 매칭되고 인접 span/img alt의 표시명을 수집하지 못함. `nearbyNames`가 `closest('main')`에서 즉시 중단됨 | Cursor dashboard가 sidebar를 `main` 안에 마운트하는데, harvest가 sidebar=main 바깥을 가정함 | identity cue 단어 경계, nearby/img/`data-*`/`aria-labelledby` 수집, main 안 aside 허용, `Team Plan` 등 plan chrome 제외; aside-in-main fixture + live 검증 |

## 증거

- 설정: `account_2` `label_mode=auto`, state `profile_name=""`, usage READY.
- Live DOM: `aside` in `main`, `button[aria-label="User menu"]` 옆 `span`/`img[alt]="종인 김"`.
- 수정 후 live probe: `profileName='종인 김'`, usage summary 유지.
- Red test: aside-in-main fixture에서 `User menu` + 인접 표시명 → `profileName`, uncued menu/email/`Usage events for all users`는 제외.

## 검증

- `uv`/venv `python -m unittest` 관련 Cursor identity tests 및 전체 `tests` (972) OK.
- `cmd /c build.bat` OK.

## 경계

- private API / cookie / email / Usage Events 표 수집은 하지 않는다.
- 표시명이 DOM에 없으면 기존처럼 `Cursor N` fallback을 유지한다.
