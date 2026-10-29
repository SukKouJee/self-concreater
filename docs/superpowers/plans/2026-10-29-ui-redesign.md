# yt-coach UI 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로컬 UI를 "영상 한 편 중심" 구조와 새 토큰·셸로 바꾼다(사용자 지시서 Phase 1~7).

**Architecture:** 기존 vanilla ES 모듈·`h()`·해시 라우터를 유지한다. 셸(`app.js`, `index.html`, `app.css`)을 새로 짜고, 영상 작업 공간 페이지가 기존 기획안·콘티·진단 페이지 모듈을 탭으로 품는다. 순수 계산(단계 진행, id↔경로, 첫 문장 요약, 촬영 체크 저장)은 `*-utils.js`로 빼 node 테스트.

**Tech Stack:** vanilla JS ES modules, CSS custom properties, pytest(+ node 헬퍼 테스트), headless Edge 스크린샷.

**Spec:** `docs/superpowers/specs/2026-10-29-ui-redesign-design.md` (지시서 원문 + 7절 구현 결정), 시안 `docs/ui-mockups/*.dc.html`

## Global Constraints

- 백엔드 API·스키마·md 포맷 변경 금지(예외: `feat/scene-conti`에서 오는 것). 필요한 값이 없으면 보고 목록(`docs/ui-mockups/REPORT.md`)에 적는다.
- 기존 라우트는 리다이렉트로 유지: `#/draft` `#/conti` `#/diagnose`(경로 파라미터 포함) → 해당 `#/video/:id/<tab>` 또는 영상 목록, `#/library` → `#/refs` 안의 탭.
- 정적 파일: innerHTML/outerHTML/insertAdjacentHTML 금지, confirm/alert/prompt 금지, `https?://` 금지(외부 폰트 불가 — spec 7절 폰트 스택).
- 토큰: spec 3절 값. 주요 행동(`--rec`) 버튼은 화면당 1개. 이모지 아이콘 금지(★☆ 제외), 그라데이션 금지.
- 크기: 본문 14px, 보조 13px, 라벨 12px; 데스크톱 버튼 32px 높이 13px 글자; 폰 주요 버튼 ≥44px.
- 390px 폭에서 가로 스크롤 없음. 1280px·390px 스크린샷을 Phase마다 `.superpowers/sdd/<plan>/phase-N-shots/`에 남긴다.
- 화면에 로컬 경로·`*.md`·`*-conti.html`·터미널 명령 노출 금지(도움말 문서 본문은 예외).
- 문구 한국어, 쉬운 말. 포커스 표시 유지, 대비 4.5:1.
- 각 Phase 끝 전체 테스트, Conventional Commit, AI 서명 금지.

## Review Focus

1. 기존 북마크(`#/conti?path=drafts/...-conti.md` 등)로 들어오면 같은 영상의 같은 탭이 열린다 → Task 2 `test_legacy_routes_redirect`(node, 라우트 변환 함수).
2. 콘티 편집 중(저장 안 한 초안) 탭을 바꾸면 기존 "저장하지 않은 변경" 보호가 그대로 동작한다 → Task 2 정적 테스트 + 실제 화면 확인.
3. 하위 폴더에 있는 기획안(`drafts/a/b.md`)도 영상 id로 왕복된다 → Task 2 `test_video_id_roundtrip`.
4. 브라우저 저장이 막힌 환경(시크릿 창)에서도 촬영 모드가 열리고 체크는 그 세션 동안만 유지된다 → Task 5 `test_shoot_store_survives_storage_errors`.
5. Claude 작업이 두 종류 이상 동시에 돌 때(수집 + 촬영 영상) 상태 칩이 둘 다 보이거나 "작업 2개"로 묶인다 → Task 6 `test_status_chip_multiple_jobs`.

---

### Task 1 (Phase 1): 토큰·공통 컴포넌트·새 셸

stack: frontend · agent: efl:frontend-developer

**Files:** `scripts/ui/static/app.css`, `scripts/ui/static/app.js`, `scripts/ui/static/index.html`, `scripts/ui/static/menu-utils.js`, 경로·파일명을 보여 주는 모든 `pages/*.js`(찾아서 제거), Create `scripts/ui/static/ui.js`(공통 컴포넌트), Test `tests/test_ui_static.py`, `tests/test_menu_utils_js.py`(있으면), 새 `tests/test_ui_kit_js.py`

**Interfaces — Produces (`/static/ui.js`):**
- `button(label, {size: "s"|"m", variant: "rec"|"primary"|"outline"|"ghost"|"link", onClick, ...attrs}) -> HTMLButtonElement`
- `chip(label, {pressed, count, onClick}) -> HTMLButtonElement`(aria-pressed), `tag(label, kind)`(kind: flow|tech|angle|combo|hook|intro|body|check|outro)
- `card(...kids)`, `tabs(items: [{key,label}], current, onSelect) -> HTMLElement`(role=tablist, 밑줄 탭, 화살표 키 이동)
- `statusChip(el)` 자리만(Task 6에서 채움): 헤더에 `#status-chip` 빈 자리.
- 셸: 데스크톱 사이드바(흰색, 220–260px, 그룹 영감/내 영상/성장 — 내 영상 목록은 `/api/drafts`로 제목 + 진행 막대 + 단계, Task 2 전까지는 링크만), 헤더(로고·카테고리·상태 칩 자리·도움말 버튼; 도움말 메뉴에 "서버 끄기"), 폰(≤640px) 하단 탭 4개 홈/영감/내 영상/미션(아이콘은 인라인 stroke SVG).
- 기존 다크 테마 비활성(라이트 고정), 촬영 모드용 `.shoot-dark` 클래스만 어두운 토큰.

- [ ] Step 1: 실패 테스트 — ui.js 렌더 결과(node+jsdom 없이: 기존 테스트 방식대로 정적 검사/순수 함수), 정적: 사이드바 그룹 3개와 항목, 하단 탭 4개, 헤더에 경로 표시 요소 없음, `pages/*.js`에 `.md`/`-conti.html`/`/yt-coach:` 문자열이 사용자 문구로 남지 않음(허용 목록 외).
- [ ] Step 2–4: 실패 확인 → 구현 → 통과.
- [ ] Step 5: 실제 화면 — 모든 라우트를 1280/390으로 스크린샷, 390px 가로 스크롤 없음(document.scrollingElement.scrollWidth ≤ 390) 확인.
- [ ] Step 6: 전체 테스트, 커밋 `feat(ui): new tokens, shell, sidebar and phone tabs`

### Task 2 (Phase 2): 영상 작업 공간 + 라우트 통합

stack: frontend · agent: efl:frontend-developer

**Files:** Create `scripts/ui/static/pages/video.js`, `scripts/ui/static/video-utils.js`; Modify `app.js`(라우트 `#/video/:id`, `#/video/:id/:tab`, `#/video/:id/shoot`; 레거시 리다이렉트), `pages/draft.js`·`pages/conti.js`·`pages/diagnose.js`(탭 안에 마운트되도록: `mount(el, params, ctx)` 그대로 쓰고 자체 머리말·경로 표시는 영상 페이지가 대신), `pages/home.js`(진행 중 영상 클릭 → 작업 공간); Test `tests/test_video_utils_js.py`, `tests/test_ui_static.py`

**Interfaces — Produces (`video-utils.js`):**
- `videoId(draftPath: string) -> string` / `draftPath(id: string) -> string` (`drafts/` 접두와 `.md` 제거·복원, 하위 폴더 보존, URL 인코딩은 라우터에서)
- `legacyRoute(hash: string) -> string|null` — `#/draft?path=…` → `#/video/<id>/plan`, `#/conti?path=…-conti.md` → `#/video/<id>/conti`, `#/diagnose?…` → 해당 영상 진단 또는 `#/videos`, 파라미터 없는 `#/draft`·`#/conti` → `#/videos`(영상 목록), `#/library` → `#/refs?tab=library`
- `stages(draft, extras) -> [{key, label, state: "done"|"now"|"skip"|"todo"}]` 6단계(spec 7절 규칙). 현재 단계에 따라 오른쪽 행동 `{label, kind}`를 `nextAction(stages)`가 돌려준다.
- 영상 페이지: 브레드크럼, 제목(30px, 왼쪽 `--rec` 세로선), 메타 줄(시작일·목표 길이·모티브 칩·유형 칩 — 기획안 프런트매터에서), 진행 카드, 탭(기획안/콘티/촬영 체크/진단) + 요약·`인쇄`(스토리보드 HTML 열기)·`다시 만들기`(콘티 탭일 때 기존 기능). `#/videos`: 영상 목록 + `+ 새 영상 시작`(모티브로 이동).
- 탭 전환 시 기존 페이지의 `unmount`와 미저장 보호를 그대로 거친다.

- [ ] Step 1: 실패 테스트 — `test_video_id_roundtrip`, `test_legacy_routes_redirect`(위 표의 각 경우), `test_stages_from_draft`(draft만/콘티 있음/filmed_at 있음/진단 있음), 정적: 영상 페이지에 탭 4개와 진행 카드.
- [ ] Step 2–4.
- [ ] Step 5: 실제 화면 — 홈 → 영상 → 탭 전환, 레거시 URL 5종, 콘티 편집 중 탭 이동 시 보호 동작, 1280/390 스크린샷.
- [ ] Step 6: 커밋 `feat(ui): video workspace with plan, conti, shoot and diagnose tabs`

### Task 3 (Phase 3): 콘티 카드 그리드 + 장면 사진 (scene-conti 화면 포함)

stack: frontend · agent: efl:frontend-developer — **`feat/scene-conti`가 이 브랜치에 합쳐진 뒤 시작**

**Files:** `pages/conti.js`(보기 부분), `conti-utils.js`, `app.css`, 필요 시 Create `scripts/ui/static/conti-board.js`; `scripts/conti.py` `render_html`(인쇄용 스토리보드도 사진 우선 — scene-conti 계획 Task 5 내용); Test `tests/test_conti_utils_js.py`, `tests/test_ui_static.py`, `tests/test_conti.py`

**Interfaces — Consumes:** scene-conti 라우트 `/api/conti/merge/preview`·`/apply`, `/api/refs/describe-status`·`/api/refs/describe`, `/api/conti/refs/pick`; `GET /api/conti`의 컷별 `ref`(frame, match: pinned|content|composition, desc).
**Produces:** 도구 줄(검색·구간 필터 칩·카드/리스트 전환 — 전환 상태는 localStorage try/catch), 카드(`repeat(auto-fill, minmax(210px,1fr))`, 프레임 112px: 사진 있으면 이미지, 없으면 샷 설명 텍스트; 좌상단 `01 · 0:00–0:10`; 섹션 태그; 행동 굵게; 대사 따옴표; `참고 · <설명>`/`구도만 참고 · <설명>`/`참고(고정)`; 길이 `약 25초`), 카드 클릭 → 기존 편집 패널. 콘티 탭 상단 보조 행동: `장면으로 합치기`(반복 있을 때만, 미리보기 → 저장), `사진 설명 만들기`(설명 부족할 때 안내 한 줄 + 버튼, 진행은 상태 칩), `사진 다시 고르기`. 인쇄용 HTML: 사진 주 그림, 도식은 사진이 없을 때만, 머리말 `장면 N개`.
- `filterCuts(cuts, {q, section}) -> cuts`, `sectionGroups(script) -> [{key,label,count}]`(도입=hook+intro, 본론=body, 마무리=outro, 체크=transition 등 실제 라벨 매핑은 labels.js 기준).

- [ ] Step 1: 실패 테스트 — `test_filter_cuts`, `test_section_groups`, HTML 렌더(사진 있으면 svg 도식 없음, 표시 세 종류, `장면 N개`), 정적: 합치기·사진 버튼과 미리보기.
- [ ] Step 2–4.
- [ ] Step 5: 실제 화면 — 경전철 콘티 사본에서 합치기 미리보기, 카드 그리드 1280/390, 인쇄 HTML.
- [ ] Step 6: 커밋 `feat(ui): conti card board with scene photos, merge and photo actions`

### Task 4 (Phase 4): 모티브 카드 + 상세 패널

stack: frontend · agent: efl:frontend-developer

**Files:** `pages/motif.js`, Create `scripts/ui/static/motif-utils.js`, `app.css`; Test `tests/test_motif_utils_js.py`, `tests/test_ui_static.py`

**Produces:** 상단(제목·한 줄·주제 입력·`새 카드 뽑기 · 약 30초` — 진행은 상태 칩, 화면 계속 사용), 필터 칩(전체/★/흐름/기법/각도/조합 + 개수), 카드 그리드 `minmax(230px,1fr)`(종류 태그, ☆/★ 토글, 제목, 한 줄 요약, 근거 썸네일 3 + `근거 영상 N`), 상세: 데스크톱 오른쪽 380px 패널 / 폰 바텀시트(포커스 가두기·Esc 닫기), 설명·"이렇게 찍어 보세요"·근거 영상·이 카드로 만든 영상·`이 카드로 새 영상 시작`(기존 기획안 만들기 흐름으로 연결 → 끝나면 새 영상 작업 공간). 터미널 안내 문구 제거.
- `oneLine(text, max=60) -> str`(첫 문장), `kindCounts(cards) -> {...}`, `filterCards(cards, f)`.

- [ ] Step 1: 실패 테스트 — `test_one_line_first_sentence`, `test_kind_counts`, `test_filter_cards_star`, 정적: 터미널 문구 없음, 상세 패널 role=dialog(폰).
- [ ] Step 2–5(실제 화면: 카드 → 상세 → 새 영상 시작 흐름, 폰 바텀시트).
- [ ] Step 6: 커밋 `feat(ui): motif cards with detail panel and start-video flow`

### Task 5 (Phase 5): 촬영 모드

stack: frontend · agent: efl:frontend-developer

**Files:** Create `scripts/ui/static/pages/shoot.js`, `scripts/ui/static/shoot-utils.js`; `app.css`(`.shoot-dark`); 영상 페이지 `촬영 체크` 탭(남은 컷 목록 + 촬영 모드 버튼 + 기존 "다 찍음"); Test `tests/test_shoot_utils_js.py`, `tests/test_ui_static.py`

**Produces:** spec 4.4 그대로. 장면 데이터는 `GET /api/conti`. 체크 저장: `shootStore(id)` — `get() -> Set<cutId>`, `toggle(cutId)`, localStorage 키 `ytc.shoot.<id>`, 모든 접근 try/catch(실패 시 메모리). 마지막 `전부 찍었어요!` → `/api/drafts/filmed {filmed:true}`. Wake Lock(`navigator.wakeLock`, 실패 무시). 키보드 ←/→ 이동(이 화면에서만, 입력 중 제외). 촬영 팁 2개는 장면 `guide`를 문장 단위로 나눠 최대 2개.
- `tipsFrom(guide) -> string[]`, `mainLabel(doneSet, i, n) -> "찍었어요 ✓"|"다음 컷으로"|"전부 찍었어요!"`.

- [ ] Step 1: 실패 테스트 — `test_main_label_states`, `test_tips_from_guide`, `test_shoot_store_survives_storage_errors`.
- [ ] Step 2–5(실제 화면: 390px에서 넘기기·체크·새로고침 후 유지).
- [ ] Step 6: 커밋 `feat(ui): phone shoot mode`

### Task 6 (Phase 6): 홈 재구성 + 상태 칩·토스트

stack: frontend · agent: efl:frontend-developer

**Files:** `pages/home.js`, `home-utils.js`, Create `scripts/ui/static/status-chip.js`, `app.js`; Test `tests/test_home_utils_js.py`, `tests/test_status_chip_js.py`, `tests/test_ui_static.py`

**Produces:** 홈 spec 4.1(데스크톱·폰 순서와 카드). 상태 칩: 헤더 한곳, 기존 job kind들(`collect`, `conti`, `conti_merge`, `refs_describe`, `motif`, `draft`, `footage`, `footage_clips`, 그 밖에 코드에 있는 kind 전부)을 `/api/jobs/current?kind=`로 폴링(작업 없을 때 느리게, 있을 때 빠르게), 문구 `Claude가 ○○ 만드는 중 · 18초`, 여러 개면 `작업 2개`로 묶고 눌러 목록. 끝나면 토스트 + 결과로 이동 링크. 각 페이지에 흩어진 진행 표시는 페이지 안 상세 진행(필요한 것)만 남기고 중복 배너 제거.
- `chipText(jobs, now) -> string`, `finishedJobs(prev, next) -> jobs`.

- [ ] Step 1: 실패 테스트 — `test_status_chip_multiple_jobs`, `test_chip_text_elapsed`, `test_finished_jobs_detected`, 홈 순서 정적 검사.
- [ ] Step 2–5(실제 화면: 긴 작업 중 다른 화면 이동, 끝나면 토스트).
- [ ] Step 6: 커밋 `feat(ui): home rebuilt, global Claude job chip and finish toasts`

### Task 7 (Phase 7): 나머지 화면 토큰·정리, 보고

stack: frontend · agent: efl:frontend-developer

**Files:** `pages/refs.js`(공용 라이브러리 탭), `pages/library.js`, `pages/typify.js`, `pages/report.js`, `pages/mission.js`, `pages/guide.js`, `app.css`; `docs/help/*.md`(화면 이름·위치가 바뀐 곳), Create `docs/ui-mockups/REPORT.md`(보고 목록), Test 정적·도움말 테스트

- [ ] 4.5대로 토큰 적용, 파일명 노출 제거, 레퍼런스 안 공용 라이브러리 탭.
- [ ] 도움말 문서의 메뉴 위치 안내를 새 구조로.
- [ ] 보고 목록(spec 7절 "보고 목록" + 진행 중 발견한 것).
- [ ] 전 화면 1280/390 스크린샷 점검, 전체 테스트, 커밋 `feat(ui): apply tokens to remaining screens and help updates`
