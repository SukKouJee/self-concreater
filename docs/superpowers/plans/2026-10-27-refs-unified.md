# 레퍼런스 화면 합치기 + 추가 흐름 다듬기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 레퍼런스 추가와 목록을 `#/refs` 한 화면으로 합치고, 링크 입력·옵션·확인을 다듬어 분석이 확인을 기다리지 않고 다음 영상으로 넘어가게 한다.

**Architecture:** 서버의 수집 작업(`kind="collect"`)을 "작업 줄(queue)" 모델로 바꾼다. 워커 스레드는 줄에서 일(분석·다시 분석·샷 분류)을 하나씩 꺼내 처리하고, 분석이 끝난 영상은 `state="awaiting", ask="review"`로 두고 바로 다음 일로 간다. 저장·건너뛰기는 HTTP 요청이 바로 처리하고, 다시 분석·샷 분류는 줄 끝에 붙는다. 화면은 `refs.js`가 목록을, `add.js`(라우트 페이지가 아니라 영역 모듈로 바뀜)가 추가 칸·진행 카드·확인 대기를 맡는다.

**Tech Stack:** Python 3.11 표준 라이브러리 HTTP 서버(`scripts/ui/server.py`의 `@route`), pytest; 빌드 없는 ES 모듈 + `h()` DOM 도우미(`/static/dom.js`), node로 순수 함수 테스트.

**Spec:** `docs/superpowers/specs/2026-10-27-refs-unified-design.md` (A~F절이 결정, "범위 밖"·"빌더에게 맡기는 것" 포함)

## Global Constraints

- 영상 md는 사용자가 저장을 눌러야 쓴다. 자동 저장 경로를 만들지 않는다(`save-all`도 그 순간 확인 대기에 있는 것만).
- 무거운 일(받기·컷·분석·샷 분류)은 워커 하나가 하나씩 한다. 저장(`videomd.write`)만 요청 스레드에서 바로 한다.
- 채널·재생목록 링크를 펼치지 않는다. 화면은 외부 주소를 쓰지 않는다(`tests/test_ui_static.py::test_no_external_resources`).
- 사용자 문구는 한국어, 식별자는 영어. 오류는 `ScriptError(code, message)` → HTTP 400.
- 디자인은 `app.css`의 shadcn 토큰·규칙만 쓴다. 새 색 토큰을 만들지 않는다.
- 프로세스를 이미지 이름으로 끄지 않는다(테스트 서버는 PID/포트로).
- 커밋 제목은 Conventional Commit, Claude 서명·Co-Authored-By 없음.

## Review Focus

1. 확인 대기가 남은 채로 작업이 취소·중단되면, 그 영상들은 계속 저장·건너뛰기 할 수 있어야 한다(Task 2 테스트).
2. 같은 항목에 저장 요청이 두 번(더블 클릭, 모두 저장과 겹침) 오면 md는 한 번만 쓰이고 두 번째는 `not_awaiting`(Task 3 테스트).
3. 작업 도중 링크를 더 넣을 때 이미 줄에 있는 영상과 같은 링크는 줄에 다시 오르지 않는다(Task 3 테스트).
4. 끝난 작업에 확인 대기가 남아 있는데 새 작업을 시작하면 그 확인 대기가 새 작업으로 넘어와 사라지지 않는다(Task 3 테스트).
5. 확인 대기를 모두 처리하면 작업이 `done`(또는 이미 멈췄으면 그대로)으로 닫히고 진행 카드가 사라진다(Task 3 테스트, Task 5 화면 확인).

---

### Task 1: 링크 미리 확인 API와 샷 분류 보강 개수 API

stack: python · agent: efl:python-developer

**Files:**
- Modify: `scripts/ui/api_add.py`
- Test: `tests/test_ui_add.py`

**Interfaces:**
- Produces:
  - `POST /api/add/check` body `{category: str, role: "ref"|"mine", links: str | list[str], force?: bool}` →
    `{"rows": [{"link": str, "id": str|None, "status": "new"|"exists"|"other_role"|"dup"|"invalid"|"reanalyze", "title": str|None}], "count": int}`.
    `count` = `new` + `reanalyze` 줄 수. 줄 순서는 입력 순서, 빈 줄은 뺀다.
    `exists`/`other_role`의 `title`은 그 md 프런트매터의 `title`(못 읽으면 None). `force=true`면 `exists`는 `reanalyze`가 된다(`other_role`은 그대로).
  - `GET /api/add/shots-pending?category=<cat>` → `{"count": int}` (`shots.pending(category)["pending"]` 길이).
- Consumes: `parse_links`(기존, 줄 하나를 영상 id로 못 바꾸면 그 줄은 `invalid`가 되도록 줄 단위로 쓰는 헬퍼를 이 파일에 둔다), `refstore.find_video(video_id)`(카테고리·role·md를 돌려준다), `fetch`의 영상 id 추출 함수(기존 `parse_links`가 쓰는 것).

- [ ] **Step 1: Write the failing tests** in `tests/test_ui_add.py`

```python
def test_check_links_classifies_rows(client, fakes, tmp_path):
    # a01 이미 ref로 저장, m01은 같은 카테고리에 mine으로 저장되어 있다고 준비(기존 fixture 도우미로 md 작성)
    body = {"category": CAT, "role": "ref",
            "links": f"{url('a01')}\n{url('n01')}\n{url('n01')}\nhttps://www.youtube.com/@channel\n{url('m01')}\n\n"}
    status, data = client.json("POST", "/api/add/check", body)
    assert status == 200
    assert [r["status"] for r in data["rows"]] == ["exists", "new", "dup", "invalid", "other_role"]
    assert data["rows"][0]["title"] and data["rows"][1]["id"] == "n01" and data["count"] == 1

def test_check_links_force_turns_exists_into_reanalyze(client, fakes):
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "ref", "links": [url("a01")], "force": True})
    assert data["rows"][0]["status"] == "reanalyze" and data["count"] == 1

def test_check_links_rejects_bad_category(client):
    status, data = client.json("POST", "/api/add/check", {"category": "../x", "links": []})
    assert status == 400

def test_shots_pending_count(client, fakes):
    status, data = client.json("GET", f"/api/add/shots-pending?category={CAT}")
    assert status == 200 and isinstance(data["count"], int)
```

(`CAT`·md 준비 도우미는 파일 안의 기존 것을 쓴다. 없으면 `tests/typify_fixtures.write_video`로 만든다.)

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_ui_add.py -k "check_links or shots_pending" -q` → 404/route 없음으로 FAIL
- [ ] **Step 3: Implement the two routes** in `scripts/ui/api_add.py` (Claude를 부르지 않으므로 `llm.check()` 없음)
- [ ] **Step 4: Run** — 같은 명령 PASS, 그리고 `pytest tests/test_ui_add.py -q` 전체 PASS
- [ ] **Step 5: Commit** — `feat(add): check pasted links before starting and count videos missing shot labels`

---

### Task 2: 작업(Job)에 줄 붙이기와 "확인 대기는 끝까지 남김"

stack: python · agent: efl:python-developer

**Files:**
- Modify: `scripts/ui/jobs.py`
- Test: `tests/test_ui_jobs_queue.py` (Create)

**Interfaces:**
- Produces (모두 `Job`의 메서드, 기존 kind들의 동작은 바뀌지 않는다):
  - `append_items(items: list[dict]) -> list[int]` — 끝에 붙이고 새 인덱스들을 돌려준다(잠금 안에서).
  - `push_task(task: dict) -> None` / `next_task(timeout: float | None) -> dict | None` — 워커용 FIFO. `push_task`는 기다리는 `next_task`를 깨운다. 취소되면 `next_task`가 `JobCancelled`.
  - `set_idle_status() -> None` — 워커가 할 일이 없을 때 부른다: 항목 중 `state=="awaiting"`이 있으면 `status="awaiting"`, 없으면 그대로.
  - `review_done() -> bool` — `ask=="review"`이고 `state=="awaiting"`인 항목이 하나도 없으면 True.
  - `_close(...)` 변경: `state=="awaiting" and ask=="review"`인 항목은 건너뜀/실패로 바꾸지 않고 그대로 둔다.
  - `take_review(item_id: str) -> int` — 잠금 안에서 그 항목이 `awaiting`+`review`인지 확인하고 `state="running", step="confirm"`으로 바꾼 뒤 인덱스를 돌려준다. 아니면 `ScriptError("not_awaiting")`. (저장 중복 방지의 원자적 관문.)
- Consumes: 없음.

- [ ] **Step 1: Write the failing tests** (`tests/test_ui_jobs_queue.py`): 

```python
def test_append_items_returns_new_indexes():
    job = Job("collect", [new_item("a")]); assert job.append_items([new_item("b"), new_item("c")]) == [1, 2]
    assert [i["id"] for i in job.items] == ["a", "b", "c"]

def test_task_queue_fifo_and_cancel_wakes_waiter():
    job = Job("collect"); job.push_task({"kind": "x", "n": 1}); job.push_task({"kind": "x", "n": 2})
    assert job.next_task(0)["n"] == 1 and job.next_task(0)["n"] == 2 and job.next_task(0) is None
    # 다른 스레드에서 cancel()하면 기다리던 next_task가 JobCancelled

def test_take_review_is_atomic():
    job = Job("collect", [dict(new_item("a"), state="awaiting", ask="review")])
    assert job.take_review("a") == 0
    with pytest.raises(ScriptError) as e: job.take_review("a")
    assert e.value.code == "not_awaiting"

def test_close_keeps_review_items():
    job = Job("collect", [dict(new_item("a"), state="awaiting", ask="review"), dict(new_item("b"), state="running")])
    job._close("stopped", "중단됨", "중단됨")
    assert job.items[0]["state"] == "awaiting" and job.items[1]["state"] == "skipped"

def test_review_done_and_idle_status():
    job = Job("collect", [dict(new_item("a"), state="awaiting", ask="review")])
    job.set_idle_status(); assert job.status == "awaiting" and not job.review_done()
```

- [ ] **Step 2: Run** `pytest tests/test_ui_jobs_queue.py -q` → FAIL (메서드 없음)
- [ ] **Step 3: Implement** in `scripts/ui/jobs.py` (줄은 `collections.deque` + `threading.Condition`을 `self._lock`과 함께 쓴다). 모듈 docstring의 상태 설명에 "collect는 작업 줄 모델" 한 단락을 더한다.
- [ ] **Step 4: Run** `pytest tests/test_ui_jobs_queue.py tests/test_ui_add.py -q` → PASS (기존 add 테스트는 아직 그대로 통과해야 한다)
- [ ] **Step 5: Commit** — `feat(jobs): task queue, appended items and review items that outlive a stopped job`

---

### Task 3: 수집 작업을 줄 모델로 — 확인을 기다리지 않음

stack: python · agent: efl:python-developer

**Files:**
- Modify: `scripts/ui/api_add.py`
- Test: `tests/test_ui_add.py` (기존 테스트 중 "결정을 기다린 뒤 다음 영상" 전제를 새 동작으로 고친다)

**Interfaces:**
- Consumes: Task 2의 `append_items`, `push_task`, `next_task`, `set_idle_status`, `review_done`, `take_review`, `_close` 동작.
- Produces:
  - 항목 필드 `opts`: `{"category", "role", "shots", "force", "refetch", "no_transcript": "stt"|"screen"|"skip"}` (영상마다).
  - `POST /api/add` body에 `no_transcript`(기본 `"stt"`) 추가. 활성 collect 작업이 있으면 **새 작업을 만들지 않고** 그 작업에 항목을 붙인다(`{"job": snapshot, "appended": [item ids]}`). 이미 줄에 있거나 처리 중인 같은 영상 id·같은 링크는 붙이지 않는다. 활성 작업이 없고 직전 collect 작업에 `review` 항목이 남아 있으면 새 작업의 항목 앞쪽에 그대로 옮겨 온다(preview·opts 포함, 옛 작업에서는 `state="skipped", message="새 작업으로 옮김"`).
  - 확인 대기 항목: `state="awaiting", ask="review", step="confirm", preview=<분석 JSON>`. `no_transcript` 질문 항목은 더 이상 생기지 않는다(옵션대로 진행).
  - `POST /api/add/{job_id}/review/{item_id}` body `{action: "save"|"skip"|"reanalyze", analysis?: dict, feedback?: str}`:
    - `save` → `take_review` 후 요청 스레드에서 `_write`. 성공 `state="saved"`(샷 옵션이면 `state="waiting", step="shots"`로 두고 줄에 `{"kind": "shots", "index": i}`). 편집한 분석이 `invalid_analysis`면 `state="awaiting", ask="review"`로 되돌리고 `message`에 이유, 응답 400. `exists`면 `skipped`/"이미 있음".
    - `skip` → `skipped`.
    - `reanalyze` → 작업이 활성일 때만(아니면 `ScriptError("job_stopped", "작업이 멈춰 다시 분석할 수 없습니다. 링크를 다시 넣어 주세요")`). `state="waiting", step="analyze"`, 줄에 `{"kind": "reanalyze", "index": i, "feedback": str}`.
    - 응답 `{"job": snapshot}`.
  - `POST /api/add/{job_id}/save-all` body `{}` → 그 순간 `review` 항목 각각에 `save`와 같은 처리. 하나가 실패해도 계속. `{"job": snapshot, "results": [{"id", "ok": bool, "message": str|None}]}`.
  - 워커(`Collector.__call__`): 시작 항목마다 `{"kind": "collect", "index": i}`를 줄에 넣고, `next_task(timeout=0.5)`로 꺼내 처리. 할 일이 없으면 `set_idle_status()`; `review_done()`이고 줄이 비면 `finish("done", summary)`. 중단 코드(`STOP_CODES`)면 남은 줄의 collect 항목을 건너뜀 처리하고 `finish("stopped", ...)`하되 review 항목은 남긴다(Task 2 `_close`와 같은 규칙).
  - 기존 `POST /api/jobs/{id}/items/{item}/decision`은 collect에 대해 `ScriptError("bad_action")`(새 review 경로를 쓰라는 메시지). 다른 kind는 그대로.
  - `Collector.save_all` 필드와 `save_all` action은 없앤다.

- [ ] **Step 1: Write/adjust failing tests** in `tests/test_ui_add.py`:

```python
def test_next_video_runs_while_first_waits_for_review(client, fakes):
    job = start_add(client, [url("a01"), url("a02")])
    j = wait_for(client, job["id"], lambda j: all(i["state"] == "awaiting" for i in j["items"]))
    assert all(i["ask"] == "review" for i in j["items"]) and j["status"] == "awaiting"

def test_review_save_writes_and_closes_job(client, fakes, tmp_path):
    job = start_add(client, [url("a01")]); wait_for(client, job["id"], lambda j: item_state(0, "awaiting")(j))
    status, data = client.json("POST", f"/api/add/{job['id']}/review/a01", {"action": "save"})
    assert status == 200 and data["job"]["items"][0]["state"] == "saved"
    wait_for(client, job["id"], lambda j: j["status"] == "done")

def test_double_save_writes_once(client, fakes):        # Review Focus 2
    ...  # 같은 항목에 save 두 번 → 두 번째 400 not_awaiting, videomd.write 호출 1회(fakes로 센다)

def test_save_all_saves_only_current_reviews_and_reports_each(client, fakes):
    ...  # a01·a02 확인 대기 → save-all → results 두 개 ok; 그 뒤 붙인 a03은 확인 대기로 남는다

def test_save_all_continues_after_one_failure(client, fakes):
    ...  # a01 편집 분석을 invalid로 만들 수 없으므로 write를 한 번 실패시키는 fake로: results[0].ok False, a01은 다시 awaiting, a02 saved

def test_reviews_survive_cancel(client, fakes):          # Review Focus 1
    ...  # a01 확인 대기, a02 분석 중 취소 → status stopped, a01 저장 200, a02 skipped

def test_append_links_to_running_job_skips_duplicates(client, fakes):   # Review Focus 3
    ...  # 진행 중에 [a02(이미 줄), a03] 넣기 → appended == ["...a03"], 새 작업 없음

def test_new_job_adopts_leftover_reviews(client, fakes):  # Review Focus 4
    ...  # 멈춘 작업에 a01 확인 대기 → 새 add(a05) → 새 작업 items[0].id == "a01" state awaiting

def test_reanalyze_queues_and_returns_to_review(client, fakes):
    ...  # feedback 전달 확인(기존 test_reanalyze_passes_feedback 대체)

def test_reanalyze_refused_on_stopped_job(client, fakes):
    ...  # 400 job_stopped

def test_no_transcript_option_skips_without_asking(client, fakes):
    ...  # no_transcript="skip" → 항목 skipped, ask 없음; "stt"·"screen"은 기존 STT/화면 분석 테스트를 옵션 방식으로 바꿔 유지

def test_old_decision_route_refuses_collect(client, fakes):
    ...  # 400 bad_action
```

  기존 테스트 중 `decide(...)`로 저장하던 것(`test_add_happy_path_awaits_then_saves`, `test_save_uses_edited_analysis`, `test_save_all_skips_further_confirmation`, `test_reanalyze_passes_feedback`, `test_no_transcript_*`, `test_decision_errors`, `test_decide_checks_action_under_lock`, `test_unknown_action_never_writes`, 타이밍 대기 테스트들)는 새 경로로 바꾸거나, 의미가 사라진 것(save_all 이후 자동 저장, no_transcript 대기)은 지운다. 타이밍: 확인 대기 시간은 여전히 걸린 시간에 넣지 않는다(분석 줄이 끝난 뒤 대기하므로 자연히 빠진다) — `test_wait_phase_*`는 다시 분석 대기 기준으로 고친다.

- [ ] **Step 2: Run** `pytest tests/test_ui_add.py -q` → 새 테스트 FAIL
- [ ] **Step 3: Implement** in `scripts/ui/api_add.py`. 쓰기 경합: 저장은 `take_review`(Task 2)로 관문을 지나야 하고, 같은 영상 md 쓰기는 기존 `videomd`의 잠금을 따른다.
- [ ] **Step 4: Run** `pytest tests/test_ui_add.py tests/test_ui_jobs_queue.py tests/test_ui_server.py -q` → PASS
- [ ] **Step 5: Commit** — `feat(add): analysis keeps going while finished videos wait for review; save, skip and reanalyze per video`

---

### Task 4: 레퍼런스 화면 하나 — 메뉴·라우트·추가 칸(링크 목록·옵션)

stack: frontend · agent: efl:frontend-developer

**Files:**
- Modify: `scripts/ui/static/app.js` (메뉴·라우터), `scripts/ui/static/menu-utils.js`(필요하면), `scripts/ui/static/pages/refs.js`, `scripts/ui/static/pages/add.js`, `scripts/ui/static/app.css`
- Create: `scripts/ui/static/add-utils.js` (순수 함수)
- Test: `tests/test_ui_static.py`, `tests/test_add_utils_js.py` (Create, node로 순수 함수 테스트 — `tests/test_progress_utils_js.py` 방식)

**Interfaces:**
- Consumes: Task 1 `POST /api/add/check`, `GET /api/add/shots-pending`; Task 3 `POST /api/add`(옵션 `no_transcript`, 붙이기 응답 `appended`).
- Produces:
  - `add-utils.js`: `startLabel(count) -> string` (`0`이면 `"분석할 영상이 없어요"`, 아니면 ``영상 ${count}개 분석 시작``), `STATUS_TEXT`(`new: "새 영상", exists: "이미 있음", other_role: "다른 구분으로 있음", dup: "중복", invalid: "영상 링크 아님", reanalyze: "다시 분석"`), `removeLine(text, link) -> string`(그 줄 하나만 지운다).
  - `pages/add.js`는 더 이상 라우트 페이지가 아니다. `export function mountAddArea(el, ctx, { open: boolean, onSaved: () => void, onChange: (state) => void }) -> { open(), close(), unmount(), onCategory(cat) }`.
  - 라우트: `#/add` → `{page: "refs", params: {add: true}}`. 메뉴 "모으기" = `레퍼런스`(`#/refs`), `공용 라이브러리`. `#/add`일 때도 `레퍼런스` 메뉴가 현재 화면으로 보인다.
  - refs 목록 머리: 제목 `레퍼런스` + 개수 줄 + 오른쪽 `+ 링크 추가`(주요 버튼, `aria-expanded`, `aria-controls`). 그 아래 추가 영역 자리, 그 아래 기존 필터·목록. 도구줄에 `GET /api/add/shots-pending`의 `count > 0`일 때만 `샷 분류 보강 (N개)` 버튼(누르면 기존 `/api/add/shots-only`).
  - 추가 칸(펼침): 머리 "<카테고리> 카테고리에 넣습니다"; 구분 토글(레퍼런스/내 영상, 기존 `.seg-opt`); 링크 글상자; 바로 아래 링크 목록(줄마다 상태 배지 + id/제목 + `×` 버튼, `aria-live="polite"`로 "새 영상 2개 · 건너뜀 1개"); `샷 분류도 함께` 체크(설명 "영상마다 Claude 호출이 한 번 더 일어나요"); `<details>` 고급: 다시 분석, 자막·컷 새로 받기, 자막이 없을 때(라디오: 음성 인식(기본)/화면만 보고 분석/건너뛰기); 시작 버튼(`startLabel`).
  - 링크 확인은 입력이 멈추고 300ms 뒤 `POST /api/add/check`(구분·다시 분석을 바꿔도 다시 부른다). 응답이 늦게 와 순서가 뒤집히면 최신 요청 것만 쓴다.
  - 카테고리 선택칸은 없다. 레퍼런스 0개인 카테고리면 추가 칸을 처음부터 펼친다. `#/add`로 들어와도 펼친다.

- [ ] **Step 1: Write failing tests**: `tests/test_add_utils_js.py`(startLabel 0/3, STATUS_TEXT 키 6개, removeLine이 같은 링크 줄 하나만 지우고 나머지·줄바꿈 유지), `tests/test_ui_static.py`(app.js 메뉴에 `"레퍼런스 추가"` 없음, `case "add": return { page: "refs", params: { add: true } }`; refs.js가 `mountAddArea`를 import; add.js에 `/api/add/check`, `<details>`를 만드는 `"details"`, `no_transcript`, `startLabel(`, `aria-live`; add.js에 카테고리 `select`를 만드는 코드 없음). 기존 add.js 정적 테스트 중 카테고리 선택·페이지 계약(`export async function mount`)을 전제한 것은 새 계약으로 고친다.
- [ ] **Step 2: Run** `pytest tests/test_add_utils_js.py tests/test_ui_static.py -q` → FAIL
- [ ] **Step 3: Implement**. 기존 add.js의 진행·확인 코드는 Task 5에서 다시 짜므로, 이 Task에서는 시작 뒤 기존 진행 화면을 영역 안에서 그대로 보여 주면 된다(Task 5 전까지 기존 확인 카드가 새 review API를 부르도록 최소 연결: 저장=`review` save, 건너뛰기=skip).
- [ ] **Step 4: Run** `pytest tests/test_add_utils_js.py tests/test_ui_static.py tests/test_ui_refs.py -q` → PASS. 테스트 서버를 띄워 `#/refs`, `#/add`, 레퍼런스 0개 카테고리를 1280·380px로 캡처해 확인(가로 넘침 없음, 추가 칸 펼침/접힘, 링크 목록 상태).
- [ ] **Step 5: Commit** — `feat(ui): one 레퍼런스 screen with a fold-out add area, live link check and fewer options`

---

### Task 5: 진행 카드와 확인 대기 묶음(요약 먼저)

stack: frontend · agent: efl:frontend-developer

**Files:**
- Modify: `scripts/ui/static/pages/add.js`, `scripts/ui/static/pages/refs.js`, `scripts/ui/static/app.css`
- Test: `tests/test_ui_static.py`, `tests/test_add_utils_js.py`

**Interfaces:**
- Consumes: Task 3 review·save-all API와 항목 필드(`ask: "review"`, `preview`, `opts.category`), 기존 `progress-utils.js`(`itemProgress`, `stepWeights` 등), `labels.js`(`hookName`, `narrativeName`, `formatName`, `sectionName`), refs 카드의 섹션 띠(`sectionBand` — refs.js에서 공용 위치로 옮겨도 된다).
- Produces:
  - 진행 카드: 지금 처리 중인 영상 하나만 크게(2026-10-25 원형 진행률·남은 시간·"지금:" 줄·단계 칸 재사용) + "남은 영상 N개" 한 줄 + `멈추기`. 줄은 비고 확인 대기만 남으면 "확인을 기다리는 영상 N개".
  - 확인 대기 묶음: 머리 "확인 대기 N개" + `모두 저장`(주요 버튼) + 한 줄 "저장하지 않은 결과는 UI 서버를 끄면 사라져요". 카드(접힘 기본): 제목·길이·채널, 섹션 띠, 훅·서사·형식 꼬리표, 한 줄 요약(`preview.summary`), 경고 한 줄(`message`), 버튼 `저장`(주요)·`고칠 점 적고 다시 분석`·`건너뛰기`·`자세히`(`aria-expanded`). 자세히 = 기존 섹션 편집 표·시놉시스·기법.
  - 포커스: 저장/건너뛰기 뒤 다음 카드의 `저장`, 없으면 묶음 머리(`tabindex="-1"`).
  - 다시 분석 버튼은 작업이 멈춘 상태면 끄고 이유("작업이 멈춰 다시 분석할 수 없어요")를 옆에 쓴다.
  - 다른 카테고리 화면: 활성 collect 작업의 항목 카테고리가 지금 카테고리와 다르면 진행 카드·확인 대기 대신 한 줄 "travel 카테고리에서 분석 중 · 확인 대기 2개 — 보기"(보기 = 상단 카테고리를 그 카테고리로 바꾼다).
  - 저장 성공마다 `onSaved()` → refs 목록 다시 불러오기.
  - `add-utils.js`에 `reviewItems(job, category) -> item[]`, `queueLine(job) -> string`("남은 영상 N개" / "확인을 기다리는 영상 N개" / "")을 더한다.

- [ ] **Step 1: Write failing tests**: node — `reviewItems`가 `ask==="review" && state==="awaiting" && opts.category===category`만, `queueLine` 세 경우. 정적 — add.js에 `/save-all`, `/review/`, `"모두 저장"`, `"자세히"`, `"확인 대기"`, `aria-expanded`, `"UI 서버를 끄면 사라져요"`; `window.confirm`·`innerHTML` 없음(기존 금지 목록).
- [ ] **Step 2: Run** `pytest tests/test_add_utils_js.py tests/test_ui_static.py -q` → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** 위 + `pytest tests/test_ui_add.py tests/test_ui_refs.py -q` PASS. 테스트 서버에서 가짜 진행 데이터 하네스(스크래치에만, 커밋 금지)로 진행 카드·확인 대기 2개·자세히 펼침·다른 카테고리 줄을 1280·380·다크로 캡처해 확인.
- [ ] **Step 5: Commit** — `feat(ui): progress card for the current video and a review queue that shows a summary first`

---

### Task 6: 문서·안내 문구 정리

stack: docs

**Files:**
- Modify: `docs/help/refs.md`, `docs/help/add.md`(내용을 refs.md로 합치고, 남길지 지울지는 `scripts/ui/api_help.py`가 페이지 이름으로 찾는 방식을 보고 정한다 — `add` 도움말 요청이 refs 도움말을 보이게), `docs/help/*.md`의 `[레퍼런스 추가](#/add)` 링크 문구 → `[레퍼런스 넣기](#/add)`, `scripts/ui/static/pages/{refs,typify,motif,diagnose}.js`의 "레퍼런스 추가로 가기" → "레퍼런스 넣기", `yt-coach-plugin-design.md`(UI 절에 "레퍼런스 화면 하나·확인 대기 줄 모델" 두세 줄), `README.md`(UI 화면 설명에 추가 화면이 있으면).
- Test: `tests/test_ui_static.py`, `tests/test_help*.py`(도움말 링크·페이지 매핑 테스트가 있으면 갱신)

- [ ] **Step 1:** 바꿀 곳을 `rg "레퍼런스 추가" docs scripts README.md`로 모두 찾는다(메뉴 이름이 사라졌으므로 남은 것은 문구뿐이어야 한다).
- [ ] **Step 2:** 고치고 도움말 내용(링크 목록 상태, 고급 옵션, 확인 대기·모두 저장, 서버를 끄면 사라짐)을 refs.md에 쓴다.
- [ ] **Step 3: Run** `pytest tests -q` 전체 PASS
- [ ] **Step 4: Commit** — `docs(ui): help and links for the merged 레퍼런스 screen`
