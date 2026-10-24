# 공용 라이브러리 + LLM 엔진 계층 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) 화면에서 Claude를 부르는 곳을 엔진 계층 하나로 모아 `claude-cli`/`anthropic-api`를 설정으로 바꿀 수 있게 하고, (2) 지인들과 GitHub 비공개 저장소로 훅 기법·기획안 틀을 나누는 공용 라이브러리(스크립트·재료 연결·MCP·화면)를 만든다.

**Architecture:** `scripts/llm.py`(엔진 계층, 엔진: `claude_cli`, `anthropic_api`) ← 모든 `scripts/ui/api_*.py`. `scripts/library.py`(git 하위 프로세스로 clone/pull/commit/push, 파일 스키마·검색·미리보기·원장) + `scripts/library_mcp.py`(stdio MCP) + `scripts/ui/api_library.py` + `pages/library.js`. 모티브·기획안은 `library` 재료를 받도록 확장.

**Spec:** `docs/superpowers/specs/2026-10-24-shared-library-design.md`(사용자 승인; 8절 결정 보완, 9절 LLM 엔진).

## Global Constraints (각 Task 지시에 그대로 포함)
- 올리기(commit+push)는 사용자 확인 뒤에만(UI 버튼·MCP confirm 도구). 자동 올리기 금지. **force push 금지**. push 거부 → `pull --rebase` 후 1회 재시도, 실패면 원인 메시지로 중단. rebase 충돌은 `rebase --abort` 후 안내(자동 해결 금지).
- git 하위 프로세스: `GIT_TERMINAL_PROMPT=0`, 시간 제한 60초, cwd는 라이브러리 clone 폴더. 오류 메시지에서 URL의 자격 증명(`https://user:token@…`)을 가린다. yt-coach는 토큰·비밀번호를 저장하지 않는다.
- 라이브러리에 올리는 내용: 기법(종류·이름·설명·출처 개수·메모·잘 먹힘), 기획안 틀(제목·길이·훅·서사·섹션 비율·평균 컷·샷 비율·섹션별 한 줄 요약·샷 리듬·메모·잘 먹힘). **대본 원문·내 메모·준비물·영상 id·개인 경로 금지.**
- 지인 것은 내 `techniques.md`·유형화·ref 근거에 섞지 않는다. 모티브 카드 `evidence`는 여전히 내 ref 영상만.
- 지인 파일은 신뢰하지 않는 데이터: 프런트매터 검증(모양이 틀리면 목록에서 빼고 개수 표시), 화면은 `renderMarkdown`, Claude 입력은 "지인 라이브러리 데이터(지시 아님)"로 감싼다.
- 사용자 md(기획안·techniques.md 등)에 아무것도 쓰지 않는다(중복 감지는 작업 폴더 `library.json` 원장). 쓰기는 `refstore.write_atomic`.
- LLM: API 키는 환경 변수에서만, 파일·로그·오류 메시지에 남기지 않는다. 엔진 기본값은 `claude-cli`(지금 동작 불변).
- 새 의존성 금지(표준 라이브러리 HTTP·JSON-RPC). 사용자 대면 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음. 병렬 작업 시 커밋은 명시한 경로만.
- **프로세스를 이미지 이름으로 끄지 않는다**(CLAUDE.md). 테스트용 git 원격은 로컬 bare 저장소(`git init --bare` in tmp).
- 커맨드를 더하면 README 커맨드 표·`tests/test_scaffold.py` COMMANDS 갱신. CLAUDE.md에 "공용 라이브러리(지인 GitHub 저장소 공유) 허용 — 호스팅은 여전히 보류" 한 줄.

## Review Focus
1. 두 사람이 거의 동시에 올림(로컬 bare 원격으로 재현) → 둘 다 올라가고 덮어쓰기 없음.
2. 인증 실패·권한 없음·네트워크 없음·잘못된 주소 → 멈추지 않고(프롬프트 없이) 한국어 원인 메시지, 자격 증명 노출 없음.
3. 지인 파일이 깨졌거나 악의적(프런트매터 없음, 거대한 본문, `<script>`, "이전 지시를 무시하라") → 목록·화면·Claude 입력이 안전.
4. `anthropic-api` 엔진: 키 없음/401/429/5xx/타임아웃 → 오류 코드 매핑, 키 비노출; schema 강제(tool_choice)로 JSON 수신.
5. 기존 화면 기능(레퍼런스 추가·콘티·유형화·모티브·기획안·다듬기·진단·미션·회고)이 엔진 계층 전환 후에도 그대로(테스트 전부 통과, 가짜 엔진으로).

---

### Task 1: LLM 엔진 계층

stack: python
agent: efl:python-developer

**Files:** Create `scripts/llm.py`, `scripts/llm_anthropic.py`(API 엔진); Modify `scripts/claude_cli.py`(엔진으로 등록 — 함수 시그니처 유지 + `files` 지원), 모든 `scripts/ui/api_*.py`(호출을 `llm.*`로), `README.md`(엔진 설정 절), `yt-coach-plugin-design.md`(한 단락); Test `tests/test_llm.py`, 기존 테스트의 가짜 대상 갱신(`claude_cli.ask` 대신 `llm` 쪽 — 동작 불변 확인).

**Interfaces:**
- `llm.config() -> dict` — 작업 폴더 `yt-coach.json`의 `llm`(없으면 기본), `YTC_LLM_ENGINE` 우선.
- `llm.check() -> dict` — 엔진 준비 확인(claude-cli: `find_claude`; anthropic-api: 키 환경 변수 존재). 실패 코드: `claude_missing`/`claude_login`(기존), `llm_not_configured`.
- `llm.ask(rules: list[Path], payload: dict, task: str, *, schema: dict | None = None, files: list[Path] | None = None, timeout: int = 600) -> dict`.
- `llm.ask_validated(rules, payload, task, validate, **kw) -> tuple[dict, Any]` — 기존 재시도 규칙 그대로(`invalid_*`만 1회).
- 엔진 등록: `ENGINES = {"claude-cli": claude_cli, "anthropic-api": llm_anthropic}`; 각 엔진 모듈은 `check()`, `ask(...)`.
- 호출 지점 변환: `tools=["Read"]` → `files=[...]`(api_add 분석: ctx의 프레임 이미지 + examples md; 샷 분류: ctx 프레임; 미션 피드백: 임시 복사본). `find_claude()` 사전 확인 → `llm.check()`. 오류 코드 `claude_*`는 claude-cli 엔진에서 그대로.
- `llm_anthropic`: `POST https://api.anthropic.com/v1/messages`, 헤더 `x-api-key`, `anthropic-version: 2023-06-01`, `content-type`. system = 규칙 파일 내용, user = 텍스트(task + payload JSON) + files 블록. schema → `tools=[{"name": "respond", "input_schema": schema}]`, `tool_choice={"type": "tool", "name": "respond"}` → 응답의 tool_use input을 dict로. schema 없으면 텍스트에서 JSON 파싱(기존 `_parse_result_text` 재사용). 모델 기본 `claude-sonnet-5-5`, `max_tokens` 기본 8000. HTTP 오류 매핑: 401/403 `llm_auth`, 그 밖 `llm_failed`(상태·`error.type`만). 이미지 최대 20장·각 5MB, 넘치면 앞에서부터.

- [ ] **Step 1: 실패 테스트** — config(기본·파일·환경 변수 우선), check(엔진별), ask 위임(claude-cli로 files → `--tools Read`), ask_validated 재시도, anthropic: 가짜 `urllib` 응답으로 요청 본문(system·tool_choice·image 블록·키 헤더) 검증, tool_use 파싱, 텍스트 JSON 파싱, 401→llm_auth, 500→llm_failed, 타임아웃→llm_failed, 오류 메시지에 키 없음, 키 없음→llm_not_configured. 기존 UI 테스트 전부 통과(가짜 대상만 갱신).
- [ ] **Step 2–4.** **Step 5: 커밋** — `feat(llm): route claude calls through a swappable engine layer`

---

### Task 2: 공용 라이브러리 핵심 (`scripts/library.py`)

stack: python
agent: efl:python-developer

**Files:** Create `scripts/library.py`, `skills/yt-library/SKILL.md`(기획안 틀 섹션 요약 규칙 — 대본 원문 금지, 섹션별 한 줄, 샷 리듬), `commands/yt-library.md`; Modify `README.md`(커맨드 표·스크립트 표), `tests/test_scaffold.py`(COMMANDS), `CLAUDE.md`(허용 한 줄), `yt-coach-plugin-design.md`(1.3·6.x·3절), `PLAN.md`(Phase 16); Test `tests/test_library.py`(로컬 bare 원격).

**Interfaces:**
- 설정 `library.json`(작업 폴더): `{"remote", "author", "shared": [{"key","hash","id","shared_at"}], "last_pull"}`; clone 위치 `workspace/library/`.
- `connect(remote: str, author: str) -> dict`(clone; 이미 있으면 remote 비교 후 안내; README 없으면 만들어 첫 커밋은 하지 않음 — 첫 공유 때 함께), `status() -> dict`, `pull() -> dict`(`--ff-only`), `search(query=None, category=None, kind=None, author=None, worked=None) -> dict`(`{"items":[…], "skipped": n}`), `get(id) -> dict`(프런트매터 + 본문 parts).
- `my_techniques(category) -> list`(내 `techniques.md` 파싱, 키 = (category, kind, name), 출처 개수), `preview_technique(category, kind, name) -> dict`, `preview_template(draft_path) -> dict`(기획안 md에서 값 계산: 비율, shot_mix는 샷 표 시간 가중, 섹션 요약 자리는 비움) — 미리보기는 `workspace/tmp/library_preview_<id>.json`(30분 만료).
- `summarize_template(preview_id) -> dict`(llm 1회, `skills/yt-library/SKILL.md`, schema `{sections: [{label, summary}]}`; 미리보기에 저장).
- `confirm(preview_id, note=None, worked=None, edits=None) -> dict` — 중복 key 확인(`duplicate` 오류 + `force_new` 옵션), 파일 쓰기 → `git add` → `commit` → `push`(거부 시 `pull --rebase` 1회 재시도) → 원장 기록 → 미리보기 삭제.
- 파일 스키마는 스펙 5.2 그대로. id = `<author-slug>-<YYYYMMDD>-<6자 해시>`.
- CLI: `python -m scripts.library connect <remote> --author <name> | status | pull | search [...] | get <id> | techniques <cat> | preview technique <cat> <kind> <name> | preview draft <file> | summarize <preview_id> | confirm <preview_id> [--note] [--worked] [--edits <json>] [--force-new]`.

- [ ] **Step 1: 실패 테스트** — bare 원격 2개 clone(지인 A·B 시뮬레이션): A 공유 → B pull·search에 보임; 동시 공유(둘 다 commit 후 push 순서대로) → 둘 다 원격에 존재; 중복 key → duplicate; 미리보기 만료; 기획안 틀 값 계산(실제 draft fixture: 비율·shot_mix·대본 원문 없음·메모 없음); 깨진 지인 파일 skipped; 인증 실패 시뮬레이션(잘못된 원격 경로) → 한국어 오류·프롬프트 없이; URL 자격 증명 가림; 사용자 md 불변(바이트·mtime).
- [ ] **Step 2–4.** **Step 5: 커밋** — `feat(library): share hook techniques and draft templates through a git library`

---

### Task 3: 재료로 쓰기 (모티브·기획안)

stack: python
agent: efl:python-developer

**Files:** Modify `scripts/motif.py`(`context(..., library_ids=None)` → payload `library_techniques`(지인 데이터 표식 포함)), `scripts/draft.py`(`context`가 `library:<id>` 출처 지원, `save`가 `source: library:<id>`·개요 "틀: <author>의 <title>"), `skills/yt-inspire/SKILL.md`(지인 기법 사용 절: basis에 "지인 <author>의 <기법>", evidence는 내 ref), `skills/yt-draft/SKILL.md`(지인 틀 절), `scripts/ui/api_motif.py`(`run`에 `library_ids` 선택 입력), `scripts/ui/api_draft.py`(`sources`에 지인 틀 묶음 — 라이브러리 연결 시), `commands/yt-inspire.md`·`commands/yt-draft.md`(한 줄씩); Test 해당 테스트 파일.

- [ ] 실패 테스트(지인 기법 payload 포함·표식, evidence 규칙 유지, draft context library 출처·다른 카테고리 틀, save 출처 기록, sources 묶음) → 구현 → 전체 통과 → 커밋 `feat(library): use shared techniques and templates as material`

---

### Task 4: MCP 서버

stack: python
agent: efl:python-developer

**Files:** Create `scripts/library_mcp.py`(stdio JSON-RPC 2.0, MCP `initialize`/`tools/list`/`tools/call`, 표준 라이브러리만), 플러그인 MCP 선언(현재 Claude Code 플러그인 문서 기준 — `.mcp.json` 플러그인 루트 또는 `plugin.json` `mcpServers`; Windows·macOS 둘 다 동작하는 명령: `bin/ytc`/`bin/ytc.cmd` 또는 python 직접), `README.md`(MCP 절); Test `tests/test_library_mcp.py`(하위 프로세스로 서버 실행 → initialize → tools/list → tools/call search/get/preview/confirm with bare 원격).

**Tools:** `library_status`, `library_pull`, `library_search`, `library_get`, `library_my_techniques`, `library_share_preview(kind: "technique"|"template", category?, technique_kind?, name?, draft?)`, `library_share_confirm(preview_id, note?, worked?, edits?)` — 설명에 "사용자에게 미리보기를 보여 주고 명시적으로 확인받은 뒤에만 호출" 명시. 결과는 JSON 텍스트 content, 오류는 `isError: true` + `{"error","message"}`.

- [ ] 실패 테스트 → 구현 → 전체 통과 → 커밋 `feat(library): expose the shared library to claude over mcp`

---

### Task 5: 공용 라이브러리 화면

stack: frontend (+ 작은 python API)
agent: efl:frontend-developer

**Files:** Create `scripts/ui/api_library.py`(status·connect·pull·search·get·my techniques·my drafts·preview·summarize job(kind `library_summary`)·confirm; 사전 검증·오류 매핑), `scripts/ui/static/pages/library.js`; Modify `scripts/ui/server.py`(ROUTE_MODULES), `scripts/ui/jobs.py`(SINGLE_KINDS), `scripts/ui/static/app.js`(메뉴 "모으기 › 공용 라이브러리" `#/library`), `pages/motif.js`(지인 기법 선택 칸 — 라이브러리 연결 시, 고른 id를 run에 전달), `pages/draft.js`(출처 select에 "지인 틀" optgroup, `#/draft/new/library:<id>`), `app.css`, `tests/test_ui_library.py`, `tests/test_ui_static.py`, `README.md`(UI 표).

동작(스펙 5.7): 연결 안 됨 → 주소·표시 이름 입력 → 연결(진행·원인 안내). 연결됨 → "받기"(마지막 받은 시각), 탭 기법/기획안 틀, 필터(카테고리·작성자·잘 먹힘), 카드(작성자·메모·잘 먹힘·날짜·"내가 올림"), 틀 카드 "이 틀로 기획안 만들기", 기법 카드 체크 → "모티브 재료로 쓰기"(모티브 화면으로 상태 전달). "공유하기": 내 기법/내 기획안 고르기 → 미리보기(기획안이면 "섹션 요약 받기" Claude 1회 → 고치기) → 메모·잘 먹힘 → "올리기"(인라인 확인) → 결과(성공·중복·push 실패 원인).

- [ ] 실패 테스트(API: 로컬 bare 원격, 가짜 llm; 정적 토큰·금지 토큰) → 구현 → 전체 통과·`node --check` → 커밋 `feat(ui): add shared library screen`
