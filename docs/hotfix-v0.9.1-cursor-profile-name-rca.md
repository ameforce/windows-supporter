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

## Round finding 묶음 RCA (identity gate thrash)

### 증상

이중 final review에서 identity harvest 가드가 라운드마다 다른 DOM 표기(`My account`, bare `Account`, `내 계정`, `add-user-trigger`, chrome wrapper sibling, kebab aria/title)에 대해 번갈아 회귀했다.

### 직접 원인

앵커 수락을 “menu/trigger 문자열 유무” 한 축으로 좁히거나, chrome root에서 sibling scan을 끄거나, reject/slug를 일부 후보 경로에만 적용하는 부분 가드가 누적됨.

### 구조 원인

`collectProfileName`가 (1) 노이즈 거부 (2) account/profile 가족 앵커 (3) strong control 표기 (4) loose user/avatar 제외 (5) 후보 sanitize를 한 계약으로 고정하지 않고, finding마다 regex/early-continue를 덧붙이는 대증 구조였음.

### 유사 결함 스캔

| 축 | 케이스 | 기대 |
|---|---|---|
| account/profile 가족 | bare Account/Profile, `data-testid=account`, `내 계정`/`나의 프로필` | 인접/내부 표시명 harvest |
| strong control | User menu, account-trigger, profile_button, userMenu | harvest |
| loose user 노이즈 | Add user, invite-user-row, add-user-trigger | 거부 후 실제 User menu로 진행 |
| chrome geometry | chip 내부, aside 직계 sibling, wrapper+adjacent name | 이름 수집, Overview 미선택 |
| candidate sanitize | User avatar alt, kebab aria/title only | 공란 → Cursor N fallback |

### 근본 수정

앵커 판정을 `reject → account/profile cue ∪ strong control → loose user 단독 거부`로 정리하고, chrome root는 containing-child 기준 인접 sibling만 스캔하며, `componentSlug`/generic/usageNoise를 모든 후보에 동일 적용한다.

### Round-10 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| local name < adjacent sidebar copy | longest-string만으로 선택 | 후보 출처(tier) 없음 | local > chromeAdjacent > chromeMeta 후 길이 |
| aria-labelledby menu cue 무시 | cue를 aria/title/testid만 사용 | a11y label 경로 누락 | labelledby를 identity/strong cue에 포함 |
| `anne-marie` slug 거부 | 모든 후보에 전역 kebab 거부 | display name과 control id 미분리 | control 어휘(menu/button/trigger…) 있는 kebab만 거부 |
| `user-menu` testid 스킵 | strong cue가 `usermenu`/`user menu`만 허용 | kebab/underscore menu 표기 누락 | `user-menu`/`user_menu` strong cue 허용 |

유사 스캔: `user_menu`, lowercase hyphen display attrs, labelledby-only Account menu, chip+Upgrade adjacent.

### Round-11 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| chip 안 CTA가 이름 압도 | 동일 local tier에서 최장 문자열 | CTA/구독 문구를 noise로 안 거름 | subscription/manage/upgrade(+KO) noise |
| `팀 플랜`이 짧은 한글 이름 압도 | EN plan만 noise | locale plan 미포함 | `팀/프로/… 플랜` noise |
| `accountMenuButton` 후보 채택 | kebab-only control filter | camel/snake control id 미차단 | camel/snake control id 거부 |
| local `JD` < adjacent Upgrade | non-initial을 tier 횡단 적용 | initials 선호가 tier 계약을 깨뜨림 | tier별 non-initial 선호 후 상위 tier 우선 |

### Round-16 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| nested wrapper + Privacy Policy → 오탐 | depth≥1 sibling를 unrestricted local로 채택 | footer/legal chrome과 display-name 경계 없음 | depth>0는 `looksLikeDisplayName`만 통과 + privacy/terms noise |
| hidden `aria-labelledby` cue 무시 | labelledby ref에 `isVisible` 요구 | a11y name은 offscreen이 흔함 | labelledby는 exclude만 검사, visibility 비요구 |
| `▾`/nav junk fallback | display-name 실패 시 non-initial longest 반환 | return 가드가 soft preference였음 | tier별 `looksLikeDisplayName` 필수 후 선택 |
| `<span>Jane</span><span>Doe</span>` 부분명 | leaf를 개별 후보만 등록 | split name join 경로 없음 | 2–3 single-word leaf를 join한 전체명 후보 |

유사 스캔: nested name beside wrap>button 유지, Terms/Cookie/개인정보 방침 noise, initials(`JD`)는 display-name으로 허용, Members-only empty menu → 공란.

### Round-17 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `Jane`+`Doe`+`Business` → `Jane Doe Business` | split join이 plan leaf를 이름 토큰에 포함 | join이 sanitize/noise 이전에 동작하고 plan 단일어 제외 없음 | join 전 `planLeaf`·`usageNoise`·account-chrome 토큰 제거 |

유사 스캔: `Team`/`Pro`/`Business` EN plan leaf, 한글 `팀` leaf, 기존 multi-word `Team Plan` splitPlan 경로 유지.

### Round-18 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| chromeAdjacent `Upgrade` > `Kim` | bare CTA가 usageNoise 밖 | CTA/sidebar 단일어 목록 불완전 | Upgrade/Feedback/Support 등 sidebar chrome exact reject |
| `Jane Doe Owner/Trial` join | role/status badge가 planLeaf 밖 | badge 축이 plan-only였음 | planOrBadgeLeaf에 Owner/Trial/Admin… |
| `Account menu: Jane Doe` 공란 | prefix 붙은 aria가 looksLikeDisplayName 실패 | JS가 Python sanitize prefix strip을 미러하지 않음 | expandCandidates에서 menu/account prefix strip |
| empty menu + Feedback/Support | 임의 sidebar 단어가 display-name 통과 | chromeAdjacent sanitize가 약함 | 동일 sidebar chrome exact reject |

유사 스캔: Support/Feedback/Upgrade/Subscribe bare CTA, Owner/Trial/Admin badge, `My account - Name` dash prefix, KO 피드백/지원/업그레이드.

### Round-19 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| 5+ word 실명 거부 | `looksLikeDisplayName` words≤4 | particle 이름 미고려 | 상한 8 words |
| `내 계정` aside sibling 미수집 | KO my-account가 strong 아님 → chromeAdjacent off | EN My account만 strong | strongMenuCue에 `내/나의 계정·프로필` |
| Avatar menu + img alt 공란 | looseUserOnly early-continue | local img alt 예외 없음 | menu+avatar/user + display-name alt는 local-only harvest |

유사 스캔: `나의 프로필` adjacent, avatar empty alt는 Feedback chromeAdjacent 불가, multipart ES/EN names.

### Round-20 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `Doe, Jane` / `Jean・Luc` / `Jane (Work)` 거부 | display-name charset이 구두점 미허용 | 가드가 letter/digit/space만 허용 | `,` `·` `・` `()` 허용 |
| `J`+`Doe` / `Li`+`Wei` 잘림 | join이 ASCII 1–2글자 leaf 제외 | initials 배제가 join 경로까지 적용됨 | short leaf 허용, non-initial 1개 이상일 때만 join |

유사 스캔: `J`+`D` only-initial join 불가 유지, Business/Owner badge join 제외 유지.

### Round-21 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `O’Connor` / `Anne–Marie` / `Jane（Work）` 거부 | charset이 ASCII/`・`/`()`에 국한 | Unicode 이름 구두점 축 누락 | curly quote·en/em dash·전각 괄호 허용 |

유사 스캔: hyphen/en-dash 계열 `\u2010-\u2015`, quotes `\u2018-\u2019`/`\u201C-\u201D`, split leaf에도 dash/apostrophe 허용.

### Round-22 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `JD`+`Jane`+`Doe` → `JD Jane Doe` | join이 img alt를 text leaf와 섞음 | avatar metadata와 DOM text join 경계 없음 | join은 textLeaves만 사용, img는 단독 후보 |
| 5-split / `Doe,`+`Jane` 잘림 | join 상한 4 + comma 미허용 | looksLikeDisplayName보다 좁은 join 문법 | join 상한 8, 이름 구두점/comma leaf 허용 |

유사 스캔: avatar initials는 단독 후보로 남고 longer `Jane Doe` 선호 유지, Business/Owner badge bare strip 후 제외 유지.

### Round-23 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `Acme Corporation` / auth chrome이 이름 압도 | local sibling이 letter-only면 display-name 통과 | org/auth chrome 축 누락 | corporation/company/inc… + sign/log out noise |
| `<img data-testid=profile-avatar alt>` 공란 | `imageNames`가 descendant img만 질의 | anchor가 img 자체인 경로 누락 | self-img alt/title도 후보로 수집 |

유사 스캔: Sign Out만 있는 chip → 공란, LLC/Ltd/GmbH org suffix, profile-avatar img anchor.

### Round-24 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| `Jane 🌟` 등 emoji/기호 이름 거부 | display-name charset allowlist | Python sanitize가 허용하는 코드포인트를 JS가 선차단 | allowlist 폐기, control/URL 거부 + 최소 1 letter |

유사 스캔: `Jane#1`/`Jane*` 허용, glyph-only/`▾`는 letter 부재로 계속 거부, org/auth chrome 거부는 유지.
side effect: prefix strip이 `user_menu_trigger` → `_menu_trigger`로 찢지 않도록 separator 강제 + leading `_` control id 거부.

### Round-25 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| adjacent `Keyboard shortcuts` 오탐 | nav UI 문구가 display-name 통과 | sidebar command/nav chrome 축 누락 | shortcuts/command palette/preferences… noise |
| probe fixture마다 browser launch | `_evaluate_probe_on_html`가 호출마다 launch/close | shared browser 부재로 70+회 기동 | class-level browser reuse + page-only cycle |

유사 스캔: Command palette / 단축키 / 환경 설정, Preferences/Appearance.

### Round-26 finding 묶음 RCA

| Finding | 직접 원인 | 구조 원인 | 근본 수정 |
|---|---|---|---|
| local `Acme Labs`/`Delete account`/`Available Now`가 `Jane Doe` 압도 | 같은 tier에서 `length` 정렬 | 출처/DOM 순서 없이 longest-wins | sourceRank(labelledBy→join→childText→nearby) + DOM order; length는 최후 타이브레이크 |
| leading org / action·status leaf | labs·delete/available이 display-name 통과 | org/action/status 축 일부 누락 | `\blabs\b` chrome + delete account/available now/online… noise |

유사 스캔: split join(`childJoin`)이 긴 status leaf보다 우선, chromeAdjacent/nearbyLocal은 childText보다 낮음.
