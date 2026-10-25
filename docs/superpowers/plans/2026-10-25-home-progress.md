# 홈 다음 할 일·영상별 진행 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 홈 맨 위에 다음 할 일 하나, 그 아래 기획안(영상)별 6단계 진행과 직접 체크하는 "촬영함", 카테고리 단계 한 줄을 보여 준다.

**Architecture:** `scripts/status.py`가 카테고리마다 `videos`(기획안별 단계 판정)와 `next_action`을 계산하고 단계에 `count`를 더한다. 촬영 체크는 `POST /api/drafts/filmed`(`scripts/ui/api_draft.py`)가 hash 확인 후 프런트매터 `filmed_at`만 바꾼다. 홈 화면(`pages/home.js`)은 이 데이터로 새 배치를 그린다.

**Tech Stack:** Python 3.11 stdlib 서버, PyYAML 프런트매터(`scripts/refstore.py`), vanilla ES modules, pytest(+ node로 JS 순수 함수).

**Spec:** `docs/superpowers/specs/2026-10-25-home-progress-design.md`

## Global Constraints

- 본문과 `## 내 메모`는 절대 덮어쓰지 않는다. 촬영 체크는 프런트매터 `filmed_at` 한 키만 바꾼다(CLAUDE.md).
- `filmed_at` 형식: `'YYYY-MM-DD'`(로컬 오늘). 해제 = 키 삭제. 계산 입력 아님. `schema_version` 그대로.
- 단계 순서·키·이름: `draft` 기획안, `polish` 다듬기(선택), `conti` 콘티, `filmed` 촬영함, `captions` 자막 영상(선택), `diagnose` 진단.
- 정렬: 진행 정도(마친 필수 수) 내림차순 → 기획안 날짜 내림차순 → 파일 이름 내림차순. 파일 수정 시각은 쓰지 않는다. 완료 영상은 맨 아래.
- 오류: hash 불일치는 `draft_changed` "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요".
- 사용자 대면 문구는 한국어. 커밋 제목 Conventional Commit, Claude 서명 없음.
- 토큰: 선택 상태 `--pick-bg/--pick-fg`, 카드 `--elev-1`, 꼬리표 `--chip`. 이 화면의 `btn-primary`는 다음 할 일 카드의 버튼 하나뿐.

## Review Focus

1. `filmed_at`이 손으로 고쳐 날짜가 아니거나(`yes`, `2026-13-40`, YAML date 객체) 빈 값일 때 → 날짜 객체는 `YYYY-MM-DD`로 인정, 나머지는 촬영 전으로 본다(Task 1 테스트).
2. 한 기획안 md의 프런트매터가 깨졌을 때 → 그 영상만 빠지고 홈은 뜬다(Task 1 테스트).
3. 체크 요청의 `path`가 `drafts/../x.md`, `drafts/a-conti.md`, 절대 경로, 다른 카테고리 기획안일 때 → 400, 파일 변화 없음(Task 2 테스트).
4. 체크를 두 번 빠르게 누를 때(두 번째 요청은 옛 hash) → 두 번째는 `draft_changed`, 화면은 다시 불러오기 안내(Task 2 API 테스트, Task 3 화면 토큰 테스트).
5. 기획안 0개 / 모두 완료 / 카테고리 단계 미완 조합 → 다음 할 일이 카테고리 문구·"새 기획안 만들기"로 맞게 바뀐다(Task 1 테스트).

---

### Task 1: 영상별 단계 판정과 다음 할 일 (status)

stack: python
agent: efl:python-developer

**Files:**
- Modify: `scripts/refstore.py` (`update_frontmatter`)
- Modify: `scripts/status.py`
- Test: `tests/test_status.py`, `tests/test_refstore.py`

**Interfaces:**
- Produces:
  - `refstore.update_frontmatter(path: Path, updates: dict[str, Any], remove: Iterable[str] = ()) -> dict` — `updates` 적용 후 `remove`의 키를 지운다(없는 키는 무시). 기존 호출 그대로 동작.
  - `status.VIDEO_STEPS: list[tuple[str, str, bool]]` = `[("draft","기획안",False),("polish","다듬기",True),("conti","콘티",False),("filmed","촬영함",False),("captions","자막 영상",True),("diagnose","진단",False)]` (키, 이름, 선택 여부)
  - `status.filmed_date(value: Any) -> str | None` — `datetime.date`나 `YYYY-MM-DD` 문자열(실제 달력 날짜)만 `"YYYY-MM-DD"`, 그 밖은 None.
  - `status._videos(category: str) -> list[dict]` — 스펙 "데이터" 절의 모양: `{path, title, date, target_sec, steps:[{key, done, optional}], next, progress, filmed_at, hash}`, 정렬 규칙 적용. `hash`는 `conti.content_hash(path)`.
  - 카테고리 항목에 `videos`, `next_action`을 더하고 각 단계에 `count: int | None`(collect=ref 수, typify=None, draft=기획안 수, conti=콘티 수, diagnose=진단 기록 수).
  - `next_action`: `{"kind":"video","path":..., "step": "conti"|"filmed"|"diagnose", "title":...}` | `{"kind":"category","text":..., "menu":...}` | `{"kind":"new_draft"}`.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_refstore.py`:
```python
def test_update_frontmatter_removes_keys_and_keeps_body(tmp_path):
    p = tmp_path / "d.md"
    p.write_text("---\ntitle: a\nfilmed_at: '2026-10-25'\n---\n본문\n\n## 내 메모\n메모\n", encoding="utf-8")
    fm = refstore.update_frontmatter(p, {}, remove=["filmed_at", "nope"])
    assert "filmed_at" not in fm and fm["title"] == "a"
    assert p.read_text(encoding="utf-8").endswith("본문\n\n## 내 메모\n메모\n")
```
`tests/test_status.py` (기존 `_save_draft`, `home` 사용; 콘티·자막·패키징·mine은 파일을 직접 만든다):
```python
def test_video_steps_from_files_and_filmed(home):
    standard_set(home)
    d = _save_draft(home)
    stem = d.stem
    (d.parent / f"{stem}-conti.md").write_text("---\ncategory: mukbang\n---\n", encoding="utf-8")
    v = _cat(status.overview())["videos"][0]
    assert [s["key"] for s in v["steps"]] == ["draft", "polish", "conti", "filmed", "captions", "diagnose"]
    assert {s["key"]: s["done"] for s in v["steps"]} == {
        "draft": True, "polish": False, "conti": True, "filmed": False, "captions": False, "diagnose": False}
    assert [s["optional"] for s in v["steps"]] == [False, True, False, False, True, False]
    assert v["next"] == "filmed" and v["progress"] == 2 and v["filmed_at"] is None
    refstore.update_frontmatter(d, {"filmed_at": "2026-10-25"})
    d.write_text(d.read_text(encoding="utf-8") + "\n## 패키징\n- x\n", encoding="utf-8")
    (d.parent / f"{stem}-captions.mp4").write_bytes(b"")
    (home / "mine").mkdir(exist_ok=True)
    (home / "mine" / "x.md").write_text(f"---\ncategory: {CAT}\ntarget: drafts\\{d.name}\n---\n", encoding="utf-8")
    v = _cat(status.overview())["videos"][0]
    assert all(s["done"] for s in v["steps"]) and v["next"] is None and v["progress"] == 4
    assert v["filmed_at"] == "2026-10-25"

@pytest.mark.parametrize("value, expected", [
    ("2026-10-25", "2026-10-25"), (datetime.date(2026, 10, 25), "2026-10-25"),
    ("2026-13-40", None), ("yes", None), ("", None), (None, None), (True, None), (20261025, None)])
def test_filmed_date(value, expected):
    assert status.filmed_date(value) == expected
```
더 쓸 테스트(이름과 단언):
- `test_videos_sorted_by_progress_then_date_then_name` — 기획안 3개(진행 2·날짜 10-20, 진행 2·날짜 10-25, 진행 1·날짜 10-26) → 순서 [10-25, 10-20, 10-26]; 진단까지 끝난 4번째는 맨 끝. mtime을 바꿔도(`os.utime`) 순서 불변.
- `test_broken_draft_is_left_out_of_videos` — 프런트매터가 `---\n: [\n---\n`인 md가 있어도 `overview()` 성공, 그 파일은 `videos`에 없음.
- `test_other_category_and_other_target_do_not_count` — 다른 카테고리 기획안은 `videos`에 없음; `mine` target이 다른 기획안이거나 mine md가 깨졌으면 diagnose False.
- `test_title_date_fallbacks` — title 없으면 stem, date 없으면 파일명 앞 10자, 그것도 날짜가 아니면 None; `target_sec`가 `"abc"`·음수·`.nan`이면 None.
- `test_next_action_choices` — (a) 기획안 0개, 카테고리 미완 → `{"kind":"category", "text": <기존 _next 문구>, "menu":...}`; (b) 미완 영상 있음 → `{"kind":"video", "path": 진행 가장 큰 것, "step":..., "title":...}`; (c) 모든 영상 완료 + 카테고리 단계 모두 완료 → `{"kind":"new_draft"}`; (d) 모든 영상 완료 + 카테고리 미완 → category.
- `test_step_counts` — collect=ref 수, typify None, draft·conti·diagnose 정수.

- [ ] **Step 2: 실행해 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_status.py tests/test_refstore.py -q`
Expected: 새 테스트 FAIL(`remove` 인자 없음, `videos` 키 없음).

- [ ] **Step 3: 구현**

`refstore.update_frontmatter`에 `remove` 인자. `status.py`: `filmed_date`, 영상 판정(패키징은 본문에 `^## 패키징\s*$` 줄, 콘티 `<stem>-conti.md`, 자막 `<stem>-captions.mp4`, 진단은 카테고리의 `mine/*.md` target을 `\`→`/`, 앞 `./` 제거 후 기획안 rel과 같은지 — mine 목록은 카테고리당 한 번만 읽는다), 정렬, `next_action`, `count`. 기존 `stage_text`·`steps`·`current`·`next` 응답 키는 그대로 둔다(다른 화면·도움말 챗봇이 쓴다).

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_status.py tests/test_refstore.py tests/test_ui_help.py -q`
Expected: 모두 PASS.

- [ ] **Step 5: Commit** — `feat(status): per-draft video steps, next action and step counts for the home screen`

---

### Task 2: 촬영함 체크 API

stack: python
agent: efl:python-developer

**Files:**
- Modify: `scripts/ui/api_draft.py`
- Modify: `yt-coach-plugin-design.md` (기획안 프런트매터 설명에 `filmed_at`, UI API 목록에 `POST /api/drafts/filmed`)
- Test: `tests/test_ui_draft.py`

**Interfaces:**
- Consumes: `refstore.update_frontmatter(..., remove=...)`, `status.filmed_date` (Task 1); 기존 `_safe_path(value, conti_file=False)`(`scripts/ui/api_conti.py`에서 api_draft가 이미 import), `conti.content_hash(path)`.
- Produces: `POST /api/drafts/filmed` body `{category: str, path: str, filmed: bool, hash: str}` → `{"path": str, "filmed_at": "YYYY-MM-DD" | None, "hash": str}`.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_ui_draft.py`, 기존 client fixture 방식)
- `test_filmed_toggle_round_trip` — 체크: 200, `filmed_at == date.today().isoformat()`, 파일 프런트매터에 같은 값, 새 hash == `conti.content_hash(path)`; 그 hash로 해제: `filmed_at is None`, 프런트매터에 키 없음. 두 번 모두 본문(`## 내 메모` 포함) 바이트가 처음과 같고 다른 프런트매터 키 값이 같다.
- `test_filmed_rejects_stale_hash` — 옛 hash로 요청 → 400 `error == "draft_changed"`, 파일 바이트 변화 없음.
- `test_filmed_rejects_bad_paths` — `drafts/../profile.md`, `drafts/<x>-conti.md`, 절대 경로, 없는 파일, 다른 카테고리 기획안 → 모두 400, 파일 변화 없음.
- `test_filmed_requires_bool` — `filmed: "yes"` → 400 `bad_request`.

- [ ] **Step 2: 실행해 실패 확인** — Run: `.venv/Scripts/python.exe -m pytest tests/test_ui_draft.py -q -k filmed` → FAIL(404 route).

- [ ] **Step 3: 구현** — `@route("POST", "/api/drafts/filmed")`. 모듈 수준 `threading.Lock()` 안에서 hash 확인 → `update_frontmatter`(체크: `{"filmed_at": date.today().isoformat()}`, 해제: `remove=["filmed_at"]`) → 새 hash 계산. 프런트매터 `category`가 요청 category와 다르면 `draft_not_found`. 설계서 두 곳 갱신.

- [ ] **Step 4: 통과 확인** — Run: `.venv/Scripts/python.exe -m pytest tests/test_ui_draft.py tests/test_status.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(ui): mark a draft as filmed from the home screen`

---

### Task 3: 홈 화면 새 배치

stack: frontend
agent: efl:frontend-developer

**Files:**
- Create: `scripts/ui/static/home-utils.js` (DOM 없는 순수 함수, import 없음)
- Modify: `scripts/ui/static/pages/home.js`, `scripts/ui/static/app.css`
- Modify: `docs/help/home.md` (새 배치 설명: 다음 할 일, 만들고 있는 영상, 촬영함 체크, 카테고리 줄)
- Test: `tests/test_home_utils_js.py` (node, `tests/test_progress_utils_js.py`와 같은 방식), `tests/test_ui_static.py`

**Interfaces:**
- Consumes: Task 1 `videos`/`next_action`/`count`, Task 2 API.
- Produces (home-utils.js):
  - `STEP_NAMES` = `{draft:"기획안", polish:"다듬기", conti:"콘티", filmed:"촬영함", captions:"자막 영상", diagnose:"진단"}`
  - `nextButton(step, path) -> {text, href} | {text, action:"film"} | null` — conti: `"콘티 만들기"`, `#/conti/<encodeURIComponent(path)>`; filmed: `"촬영함 체크"`, action film; diagnose: `"진단하기"`, `#/diagnose/<encodeURIComponent(path)>`; null → null.
  - `NEXT_REASON` = `{conti:"기획안을 촬영용 콘티로 바꿔요", filmed:"콘티대로 찍었으면 체크해 두세요", diagnose:"찍은 영상을 아키타입과 비교해 고칠 점을 찾아요"}`
  - `stepState(step, nextKey) -> "done" | "next" | "todo" | "optional"` 와 화면 읽기 글 `stepSr(state)` = `{done:"완료", next:"다음 단계", todo:"아직", optional:"선택, 아직"}`.
  - `VISIBLE_VIDEOS = 3`.

- [ ] **Step 1: 실패하는 테스트**
  - `tests/test_home_utils_js.py`: `nextButton` 세 경우와 null(경로에 공백·한글·`#`가 있어도 href가 인코딩됨), `stepState` 네 경우(완료가 선택 단계여도 "done"), import 없음.
  - `tests/test_ui_static.py`: `test_home_layout_tokens` — home.js에 `"다음 할 일"`, `"만들고 있는 영상"`, `"/api/drafts/filmed"`, `"aria-pressed"`, `"draft_changed"`, `"다시 불러오기"`, `"개 더 보기"`, `"진단 기록"`, `from "/static/home-utils.js"`가 있고 `btn-primary`는 정확히 한 번; `innerHTML` 없음; app.css에 `.home-next`, `.vid-steps`, `.cat-line` 규칙과 좁은 화면 규칙(`@media (max-width: 720px)` 안에 `.vid-row`).

- [ ] **Step 2: 실행해 실패 확인** — Run: `.venv/Scripts/python.exe -m pytest tests/test_home_utils_js.py tests/test_ui_static.py -q` → 새 테스트 FAIL.

- [ ] **Step 3: 구현** — 스펙 "화면 배치" 절 그대로. 고른 카테고리(`ctx.category`, 없으면 첫 카테고리)만 1~3을 그리고 다른 카테고리는 카테고리 줄만(누르면 `ctx.setCategory`). 영상 줄: 제목 링크 `#/draft/view/<path>`, 날짜·목표 길이(분), 단계 `ol`(각 `li`에 이름 + `.sr-only` 상태), 촬영함 단계는 `button[aria-pressed]`(이름 "<제목> 촬영함"), 줄 끝 `nextButton`. 촬영 토글: 버튼 잠금 → API → 성공하면 홈 다시 불러오기(`mount` 재호출), `draft_changed`면 그 줄 아래에 경고와 "다시 불러오기" 버튼, 다른 오류는 `ctx.toast(e.message, "error")`. 모든 await 뒤 `ctx.isCurrent()` 확인. 미션·최근 결과물 카드는 기존 함수 재사용. 기존 `stepBar`/`stage-notes`는 지운다(쓰는 곳 없음 확인).

- [ ] **Step 4: 통과 확인** — Run: `.venv/Scripts/python.exe -m pytest tests/test_home_utils_js.py tests/test_ui_static.py tests/test_help_content.py -q` → PASS. 그다음 테스트 서버로 실제 확인(컨트롤러가 함).

- [ ] **Step 5: Commit** — `feat(ui): home shows the next action, per-video progress with a filmed toggle and a one-line category stage`
