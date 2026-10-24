# 진단 화면 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로컬 UI "성장 › 진단"(지금 2차 안내)을 실제 화면으로: 대상(내 영상·기획안) 고르기 → 비교(Claude 없음) → 설명 받기(Claude 1회) → 고쳐 저장(`mine/<이름>.md`) → 저장된 진단 보기·의도한 차이 표시.

**Architecture:** 계산·저장은 기존 `scripts/similarity.py`(`compare`, `save`, `intentional`). notes 검증을 `validate_notes(result, notes)`로 뽑아 Claude 응답 검증과 `save`가 같이 쓴다. UI 서버 `scripts/ui/api_diagnose.py`(목록·비교·설명 작업·저장·의도 표시·기록 읽기), 프런트 `pages/diagnose.js`(md 본문은 `static/md.js`의 `renderMarkdown`).

**Spec:** 채팅에서 승인(2026-10-24) — 아래 "승인된 범위". 규칙: `skills/yt-diagnose/SKILL.md`, 터미널 흐름: `commands/yt-diagnose.md`.

### 승인된 범위
- 대상 고르기: "내 영상"(레퍼런스 추가에서 구분을 "내 영상"으로 넣은 것)과 "기획안" 두 목록. 이미 진단한 대상엔 "진단함" 표시·날짜. 유형화를 저장하지 않았으면 "비교할 아키타입이 없습니다" + 유형화로.
- 1단계 "비교하기"(Claude 없음, 바로): 가장 가까운 아키타입, 축별 근접도 표(구조·훅·서사·제목·컷 속도), 갭 목록(숫자 "나 33% / 아키타입 1%"), ref 10개 미만이면 "예비 진단".
- 2단계 "설명 받기"(Claude 1회): 요약·갭마다 설명·다음 행동 1~3개(모두 고칠 수 있음) → "저장" → `mine/<이름>.md`(`## 내 메모` 유지).
- 저장된 진단 보기: 기록을 마크다운으로, 갭마다 "일부러 다르게 한 거예요" 표시(누르면 바로 저장, 비교 다시 해 갭 목록 갱신; 설명이 맞지 않게 되면 "설명 다시 받기" 안내). "오늘 미션 받기"는 터미널 명령 안내(`/yt-coach:yt-mission today`).
- 1단계 비교와 2단계 설명을 나눔(내 결정). 빼는 것: 유튜브 링크 바로 넣기(레퍼런스 추가 화면 사용), 대본 붙여넣기 진단(터미널).

## Global Constraints (각 Task 지시에 그대로 포함할 것)
- Claude는 "설명 받기" 클릭 때만. 저장은 "저장" 클릭 때만. 의도한 차이 표시는 사용자 행동이라 즉시 저장 가능.
- `mine/<이름>.md`의 `## 내 메모`는 절대 덮어쓰지 않는다(기존 `save`가 유지 — 깨지지 않게).
- ref 영상은 진단 대상이 아니다(`not_mine` 그대로). mine은 유형화 기준에 섞지 않는다.
- notes 검증은 하나(`similarity.validate_notes`)로 CLI·UI 공통, 기존 코드 `invalid_notes`와 메시지 유지.
- 새 의존성 금지. 프런트: innerHTML 금지, `h()`/`fill()`, 정적 파일 `http(s)://` 금지, 네이티브 dialog 금지(인라인 확인), 페이지 계약·`alive`/`isCurrent()`·`ctx.api`, 작업 재진입·handled·`job_id`·aborted 처리는 `pages/motif.js` 방식.
- 사용자 대면 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음. 병렬 작업 시 커밋은 명시한 경로만.
- **프로세스를 이미지 이름으로 끄지 않는다**(`taskkill /IM python.exe`/`node.exe`, `Stop-Process -Name`, `pkill` 금지 — CLAUDE.md). 직접 띄운 PID만.

## Review Focus
1. 설명을 받은 뒤 의도한 차이를 표시해 갭이 줄었는데 그 notes로 저장 → 사라진 갭 설명은 무시하고 저장(남은 갭 설명이 다 있으면 통과), 새로 생긴 갭이 있으면 `invalid_notes`로 빠진 갭을 알림.
2. 같은 대상을 다시 진단·저장 → `## 내 메모`·`intentional`·`history` 유지, history 한 줄 추가.
3. 진단 기록 이름 규칙: mine 영상은 영상 id, 기획안은 파일 stem(기존 `_target`) — 화면 목록의 "진단함" 판정도 같은 규칙.
4. 유형화 결과가 바뀌어 아키타입이 없어짐 → 비교 `no_archetype`/`no_typify_result` 안내, 저장된 진단은 그대로 보임.
5. 다른 카테고리 기록·대상은 목록에 섞이지 않음.

---

### Task 1: 진단 API + notes 검증 분리

stack: python
agent: efl:python-developer

**범위:** 위 승인된 범위의 서버 부분 + Global Constraints 전부(위 절을 그대로 따른다).

**Files:** Modify `scripts/similarity.py`(`validate_notes`), `scripts/ui/jobs.py`(`SINGLE_KINDS["diagnose"] = "이미 진단 설명을 받는 중입니다"`); Create `scripts/ui/api_diagnose.py`; Modify `scripts/ui/server.py`(`ROUTE_MODULES`); Test `tests/test_similarity.py`, `tests/test_ui_diagnose.py`.

**Interfaces (Task 2가 씀):**
- `similarity.validate_notes(result: dict, notes: Any) -> dict` — 기존 `save` 검증 그대로(요약 필수, `result["gaps"]` 모든 key 설명, 행동 1~3), 모양이 틀린 notes(dict 아님, gaps dict 아님, actions list 아님, 문자열 아닌 값) → `invalid_notes`. 정리된 notes 반환(한 줄 요약은 공백 정리, 행동 공백 제거). `save`가 이것을 쓴다. 여분 갭 설명은 무시.
- `GET /api/diagnose/targets?category=` → `{"has_typify": bool, "preliminary": bool|None, "videos": [{"path","name","title","date"(md mtime 또는 fm), "diagnosed": {"date","nearest"}|None}], "drafts": [같은 모양]}` — 내 영상 = 그 카테고리 `videos/*.md` 중 `role: mine`; 기획안 = `drafts/*.md`(콘티·SRT 제외) 중 프런트매터 category가 같은 것; `diagnosed`는 `mine/<이름>.md`가 있고 그 프런트매터 category가 같을 때. 깨진 md는 건너뜀.
- `POST /api/diagnose/compare` `{"category","target"}` → `similarity.compare` 결과 그대로(+ 축 이름 한국어 `axis_ko`). 대상 경로는 작업 폴더 안 `refs/ref_<cat>/videos/*.md` 또는 `drafts/*.md`만(밖·콘티 파일 → `bad_path`).
- `POST /api/diagnose/explain` `{"category","target"}` → `{"job"}`: `find_claude()` 먼저, 비교를 작업 전에 한 번 돌려 `no_typify_result` 등은 400으로. 작업 kind `"diagnose"`, 항목 1개(id = target 경로), step `"compare"` → `"claude"` → `awaiting/preview/ask="save"`, preview `{"result": compare 결과, "notes": 검증된 notes}`, 작업 `done`. Claude: `ask_validated([SKILLS_DIR/"yt-diagnose"/"SKILL.md"], payload=compare 결과, task=DIAG_TASK, validate=lambda d: similarity.validate_notes(result, d), schema=NOTES_SCHEMA)`; `DIAG_TASK = "yt-diagnose 규칙대로 입력 비교 결과의 notes JSON(요약, 갭마다 설명, 다음 행동 1~3개)을 만드세요."`; 스키마 `{summary: str, gaps: object<str>, actions: array<str> 1..3}`.
- `POST /api/diagnose/save` `{"category","target","notes","job_id"?}` → 저장 직전에 compare를 다시 해 `validate_notes`(Review Focus 1) → mkstemp(`workspace/tmp/diag_notes_*.json`, finally 삭제) → `similarity.save` → 결과 + `"record"`(아래 GET과 같은 모양). 같은 대상 job 항목이 awaiting이면 `saved`.
- `POST /api/diagnose/intentional` `{"category","name","key","on": bool}` → `similarity.intentional(..., off=not on)` → 결과 + 새 compare 결과(`"compare"`)(대상 경로는 기록의 `target`).
- `GET /api/diagnose/record?category=&name=` → `{"name","path","fm"(target,date,nearest,proximity,gaps,intentional,preliminary),"body": [{"heading","text"}](api_refs._body_parts 규칙)}`; 없으면 `diagnosis_not_found`. name은 `^[A-Za-z0-9가-힣_.\-]{1,120}$` 등 파일 이름 안전 검사(경로 구분자·`..` 금지).

- [ ] **Step 1: 실패 테스트** — validate_notes(기존 save 메시지 유지 확인 특성 테스트 먼저), 모양 오류; targets(내 영상·기획안·진단함·카테고리 분리·깨진 md); compare(대상 경로 검증, not_mine, no_typify_result); explain 작업(가짜 `claude_cli.ask`로 실제 재시도 경로: 성공 preview, 두 번 실패 → failed, 파일 없음); save(메모·intentional·history 유지, 사라진 갭 설명 무시, 새 갭 invalid_notes, tmp 정리, job saved); intentional 토글 → compare 갱신; record(본문, 잘못된 name); 중복 실행 job_running.
- [ ] **Step 2–4.** **Step 5: 커밋** — `feat(ui): add diagnose api`

---

### Task 2: 진단 화면

stack: frontend
agent: efl:frontend-developer

**범위:** 위 승인된 범위의 화면 부분 + Global Constraints 전부(위 절을 그대로 따른다).

**Files:** Create `scripts/ui/static/pages/diagnose.js`; Modify `scripts/ui/static/app.js`(메뉴 "진단" → `route: "#/diagnose"`, `soon` 제거, `parseHash` `case "diagnose"`: `#/diagnose` 목록, `#/diagnose/<encoded target path>` 진단 화면), `scripts/ui/static/pages/home.js`(`MENU_ROUTES.diagnose = "#/diagnose"`), `scripts/ui/static/app.css`, `tests/test_ui_static.py`, `README.md`(UI 메뉴 표), `PLAN.md`(Phase 14 진단 체크).

동작:
- 목록: 상단 "진단" + 아키타입 없음 안내(유형화 링크) / 예비 진단 표시. 두 묶음 "내 영상"(비었으면 "레퍼런스 추가에서 구분을 '내 영상'으로 넣으면 여기 나옵니다" + `#/add`), "기획안"(비었으면 `#/draft`). 행: 제목, 날짜, "진단함 YYYY-MM-DD · A1" 배지, 누르면 진단 화면.
- 진단 화면(대상 하나): 진단 기록이 있으면 먼저 기록 보기(제목, 요약 등 md 본문을 `renderMarkdown`으로) + 갭 목록(키 한국어 표시: structure:intro → "구조: 인트로" 등, 숫자) 각 갭에 토글 "일부러 다르게 한 거예요"(`aria-pressed`, 즉시 저장, 실패 시 되돌림) + 의도한 차이 목록에서 해제 가능. 토글 후 갭이 기록의 갭과 달라지면 "설명이 지금 갭과 맞지 않습니다 — 설명 다시 받기" 안내.
- 버튼 "비교하기"(Claude 없음) → 근접도 표(축 한국어, %, null이면 "정보 없음"), 가장 가까운 아키타입(이름·골격), 다른 아키타입 구조 근접도, 갭 목록(숫자). 버튼 "설명 받기"(Claude 1회 안내) → 진행 → 편집 미리보기: 요약 textarea, 갭마다 textarea(라벨 갭 이름), 다음 행동 1~3 input(추가·삭제, 최대 3), "저장"(빈 칸이면 비활성+이유), "버리기"(인라인 확인). 저장 성공 → 토스트 "진단을 기록했습니다" + 기록 보기로 + `ctx.refreshCategories()`(홈 단계 갱신). 실패(`invalid_notes` 등) → 지속 alert, 입력 유지.
- 하단 안내: "오늘 미션은 터미널에서 /yt-coach:yt-mission today" (명령 복사 버튼).
- `onCategory`: 목록으로.

- [ ] **Step 1: 정적 테스트** — 메뉴 라이브 라우트(`route: "#/diagnose"`, `soon: "diagnose"` 없음, `case "diagnose"`, home.js `"#/diagnose"`); 페이지 계약·토큰("/api/diagnose/targets","/api/diagnose/compare","/api/diagnose/explain","/api/diagnose/save","/api/diagnose/intentional","/api/diagnose/record","kind=diagnose","job_id","비교하기","설명 받기","일부러 다르게 한 거예요","renderMarkdown","/yt-coach:yt-mission today"); 금지 토큰 없음; 서빙 200. COMMANDS 목록에서 `/yt-coach:yt-diagnose`가 빠지면 이전 화면들처럼 맞추고 이유 기록.
- [ ] **Step 2–4:** 구현, `pytest tests -q`, `node --check`. **Step 5:** 문서. **Step 6: 커밋** — `feat(ui): add diagnose screen`
