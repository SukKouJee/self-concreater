# 서버판 준비 S4·S2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티 스토리보드 HTML이 사진·그림을 박지 않고 링크하게 하고(S4), 작업 폴더를 프로세스 전역 대신 실행 문맥(Ctx)에서 읽을 수 있게 한다(S2). 로컬판 동작은 그대로.

**Architecture:** S4 — `conti.render_html`/`footage_out`의 이미지가 data URI 대신 HTML 파일 기준 상대 URL(`../workspace/frames/...`, `../workspace/conti-art/...`)을 쓰고, 한 파일로 들고 다닐 인쇄용은 `embed=True` 옵션. S2 — `scripts/ctx.py`의 `ContextVar[Ctx]`; `common.work_root()`가 Ctx를 먼저 보고 없으면 지금처럼 `YT_COACH_HOME`/cwd. 스레드로 넘어갈 때 문맥을 복사한다.

**Tech Stack:** Python 3.11 표준 라이브러리(contextvars, urllib.parse), pytest.

**Spec:** `docs/superpowers/specs/2026-10-31-multiuser-server-design.md` (2.2, 6절 S2·S4, 7절)

## Global Constraints
- 로컬판 작업 폴더 형식·md 내용·동작은 바꾸지 않는다. 기존 테스트 전부 통과.
- md 본문과 `## 내 메모`는 건드리지 않는다. 새 의존성 없음.
- 커밋 제목 Conventional Commit, AI 서명 금지. 사용자 대면 문구 한국어.

## Review Focus
- 파일 이름에 한글·공백·`#`·`%`가 있는 프레임/그림 경로 → 상대 URL이 퍼센트 인코딩돼 브라우저(`/files/drafts/...` 와 `file://` 둘 다)에서 열린다.
- 그림·사진 파일이 없는 장면 → 지금처럼 도식/다음 순위로 대체(깨진 이미지 없음).
- 작업(job) 스레드·HTTP 요청 스레드 안에서 `work_root()` → 시작 때 설정한 Ctx 또는 지금과 같은 값.
- `YT_COACH_HOME`을 테스트가 monkeypatch로 바꾸는 기존 테스트 → Ctx가 설정되지 않은 한 그대로 동작.

---

### Task 1: S4 콘티 HTML이 사진·그림을 링크
stack: python · agent: efl:python-developer

**Files:**
- Modify: `scripts/conti.py` (`_data_uri` ~510, `ref_figure` ~564, `art_figure` ~579, `render_html` ~598, `_write_html` ~658, CLI)
- Modify: `scripts/footage_out.py` (~182, 같은 방식)
- Test: `tests/test_conti.py`, `tests/test_footage_out.py`, `tests/test_ui_conti.py`

**Interfaces:**
- Produces: `conti.asset_url(rel: str, html_dir_rel: str = "drafts", embed: bool = False) -> str | None` — 작업 폴더 상대 경로 `rel`(예 `workspace/frames/<vid>/0001.jpg`)을 HTML 위치(`drafts/`) 기준 상대 URL(`../workspace/frames/<vid>/0001.jpg`, 경로 조각마다 `urllib.parse.quote`)로; 파일이 없으면 None; `embed=True`면 지금의 data URI.
- `render_html(fm, *, embed: bool = False)`; `_write_html`은 기본 링크. `footage_out`의 HTML도 같은 함수 사용(그 HTML이 놓이는 폴더 기준).
- CLI: `python -m scripts.conti html <콘티 md> [--embed]` → 그 콘티의 HTML을 다시 쓰고 `{"status":"built","path":...,"bytes":N}`; `--all`이면 `drafts/*-conti.md` 전부(기존 HTML 크기 줄이기용). 콘티 md는 건드리지 않는다.

- [ ] Step 1: 실패 테스트 — `test_render_html_links_images_by_default`(img src가 `../workspace/frames/`로 시작, `data:image` 없음), `test_render_html_embed_option`(embed=True면 data URI), `test_asset_url_quotes_korean_and_special`(한글·공백·`#`·`%` 인코딩, 파일 없으면 None), `test_html_size_small_for_many_cuts`(사진 있는 300컷 콘티 HTML < 1MB), `test_conti_html_cli_all_rewrites_html_only`(md 바이트 동일), footage_out 같은 링크 테스트, 기존 HTML 관련 테스트 단언을 새 동작에 맞게(약화 금지).
- [ ] Step 2: FAIL 확인. Step 3: 구현. Step 4: 위 파일들 + `tests/test_conti_art.py` PASS.
- [ ] Step 5: 화면 확인 — 작업 폴더 사본에서 `--all` 실행 전후 HTML 크기 비교, `/files/drafts/<x>-conti.html`을 headless Chrome으로 캡처해 사진·그림이 보이는지.
- [ ] Step 6: 커밋 `feat(conti): storyboard HTML links photos and drawings instead of embedding them`
- 도움말(`docs/help/conti.md`)에 "스토리보드 HTML은 작업 폴더 안에서 열어야 사진이 보입니다. 다른 사람에게 보낼 때는 한 파일로 만들기" 한 줄; 화면에 한 파일 내보내기 버튼은 이번에 만들지 않는다(CLI만).

### Task 2: S2 실행 문맥(Ctx)
stack: python · agent: efl:python-developer

**Files:**
- Create: `scripts/ctx.py`, `scripts/tools/count_work_root.py`(또는 `tools/`가 없으면 `scripts/count_work_root.py`)
- Modify: `scripts/common.py` (`work_root`, `run`), `scripts/ui/server.py`(시작 지점), `scripts/ui/jobs.py`(스레드 시작 때 문맥 복사), HTTP 요청 스레드(ThreadingHTTPServer 핸들러에서 서버 시작 때 잡은 문맥으로 실행)
- Test: `tests/test_ctx.py`, 기존 `tests/test_ui_jobs*.py`

**Interfaces:**
- `ctx.Ctx` (frozen dataclass): `root: Path`, `user_id: str | None = None`
- `ctx.current() -> Ctx | None`, `ctx.use(c: Ctx)` 컨텍스트 매니저(토큰으로 되돌림), `ctx.set_default(c: Ctx)`(프로세스 시작 지점용)
- `common.work_root()`: Ctx가 있으면 `ctx.root`, 없으면 지금처럼 `YT_COACH_HOME` 또는 cwd.
- 로컬판 시작 지점(`common.run`으로 도는 CLI, `ytc ui` 서버)은 지금 값으로 Ctx를 한 번 설정 → 동작 동일.
- JobManager가 작업 스레드를 만들 때 `contextvars.copy_context()`로 실행; HTTP 요청 스레드도 서버의 Ctx로 실행.
- `count_work_root`: `scripts/` 아래 `work_root(`·`workspace_dir(`·`category_dir(` 호출 수를 모듈별로 세어 JSON 출력(테스트는 형식만).

- [ ] Step 1: 실패 테스트 — `test_work_root_prefers_ctx`, `test_work_root_falls_back_to_env_and_cwd`, `test_use_restores_previous`, `test_job_thread_sees_ctx`(Ctx A로 시작한 작업 스레드 안의 work_root == A, 다른 스레드 기본값과 무관), `test_two_ctx_isolated_in_threads`(스레드 두 개가 서로 다른 Ctx로 동시에 work_root를 읽어 섞이지 않음), `test_count_work_root_outputs_modules`.
- [ ] Step 2: FAIL. Step 3: 구현(전역 상태 최소). Step 4: `tests/test_ctx.py tests/test_ui_jobs*.py tests/test_ui_server.py tests/test_common.py`(있으면) PASS.
- [ ] Step 5: 설계 문서 6절 S2 줄 옆에 측정값(현재 호출 수) 기록.
- [ ] Step 6: 커밋 `feat(core): execution context for the work folder`
