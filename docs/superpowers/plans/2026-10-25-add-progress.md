# 레퍼런스 추가 진행률·예상 시간 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 레퍼런스 추가 화면의 영상 카드에 단계별 진행 막대, 경과/예상 시간, 남은 시간과 예상 완료 시각을 보여 준다.

**Architecture:** 실측 기록과 예상은 `scripts/timings.py`(작업 폴더 `workspace/timings.json`), 작업 항목의 `timing` 필드 관리(갱신 빈도 제한, 진행률 단조 증가, 확인 대기 제외)는 `scripts/ui/progress.py`가 맡는다. 수집 함수들(`fetch.wait_turn`, `cuts.cut_stats`, `stt.transcribe`)은 선택 인자 `progress` 콜백으로 하위 구간 진행을 알린다. 화면은 순수 함수 모듈 `static/progress-utils.js`로 시간 계산을 하고 `pages/add.js`가 단계 줄을 그린다.

**Tech Stack:** Python 3.11 stdlib, OpenCV/PySceneDetect, yt-dlp, faster-whisper(기존), vanilla ES modules, pytest(+node로 JS 순수 함수).

**Spec:** `docs/superpowers/specs/2026-10-25-add-progress-design.md`

## Global Constraints

- 사용자 대면 문구는 한국어. 시간 형식 `m:ss`, 1시간 이상 `h:mm:ss`, 시각 `HH:MM`.
- 진행 갱신은 0.5초에 한 번 이하(`job.update` 호출 기준). 진행률은 줄어들지 않는다. 측정 불가 구간은 `fraction: null`.
- 확인 대기(awaiting) 시간은 어떤 단계에도 넣지 않는다.
- `workspace/timings.json` 읽기·쓰기 실패는 조용히 기본값으로 계속한다. 단계별 최근 20건만 보관. 정상 완료만 기록(실패·취소·캐시 제외).
- 기록 키(단계): `fetch`, `stt`, `cuts`, `analyze`, `shots`. 분석·샷은 엔진별(`llm.config()["engine"]`).
- 새 색 토큰 없음. 진행 막대 `role="progressbar"`, 측정 불가 시 `aria-valuetext`. 1초 시계는 `aria-live`로 읽지 않는다.
- 프로세스를 이미지 이름으로 끄지 않는다. 커밋은 Conventional Commit, Claude 서명 없음. Python은 `.venv/Scripts/python.exe`(bare `python`은 Windows Store 스텁).

## Review Focus

1. 컷 통계가 캐시돼 있어 컷 단계가 즉시 끝날 때: 기록하지 않고, 예상 0·"✓ 0:00 걸림"으로 끝나야 한다 → Task 4 `test_cached_cuts_not_recorded`.
2. yt-dlp가 전체 바이트를 모를 때(`total_bytes`·`total_bytes_estimate` 없음): 받기 구간은 `null`로 남고 오류가 나지 않는다 → Task 3 `test_download_progress_without_total`.
3. `timings.json`이 깨졌거나 쓰기 금지일 때: 수집이 성공한다 → Task 1 `test_corrupt_file_falls_back`, `test_write_failure_is_silent`.
4. 확인 대기 중 오래 머문 뒤 재분석: 분석 줄이 새로 시작하고 대기 시간이 경과에 섞이지 않는다 → Task 4 `test_reanalyze_restarts_analyze_row`.
5. 브라우저 시계가 서버와 다를 때: 경과가 음수가 되지 않는다 → Task 5 `test_elapsed_uses_server_offset`.

---

### Task 1: 실측 기록과 예상 시간 `scripts/timings.py`

stack: python
agent: efl:python-developer

**Files:**
- Create: `scripts/timings.py`
- Test: `tests/test_timings.py`

**Interfaces:**
- Produces:
  - `STEPS = ("fetch", "stt", "cuts", "analyze", "shots")`, `KEEP = 20`
  - `DEFAULTS: dict[str, dict]` — 단계별 `{"per_video_sec": float, "fixed_sec": float}`. 컷·STT는 길이 비례, fetch·analyze·shots는 `per_video_sec=0`과 고정값. 값은 실측해 정한다(참고: 43분 31초 영상의 컷 약 490초).
  - `record(step: str, *, elapsed_sec: float, video_sec: float | None = None, engine: str | None = None, frames: int | None = None, text_chars: int | None = None) -> None` — 실패해도 예외를 내지 않는다.
  - `expected(step: str, *, video_sec: float | None, engine: str | None = None) -> float | None` — 길이 비례 단계인데 `video_sec`가 None이면 None. 기록이 3건 이상이면 비례 단계는 (elapsed/video_sec) 중앙값×video_sec, 고정 단계는 elapsed 중앙값. 3건 미만이면 기본값과 기록을 평균.
  - 저장 위치: `workspace_dir("timings.json")`. 분석·샷은 `f"{step}:{engine}"` 키.

- [ ] **Step 1: Write the failing tests** (`YT_COACH_HOME`을 tmp로)

```python
def test_expected_uses_defaults_without_records()      # expected("cuts", video_sec=600) == DEFAULTS 기반 값; expected("cuts", video_sec=None) is None
def test_expected_median_after_three_records()          # cuts 기록 (600,120),(600,60),(600,90) → expected(video_sec=1200) == 180
def test_fixed_step_ignores_length()                    # analyze 기록 30,40,50(engine "claude-cli") → expected("analyze", video_sec=9999, engine="claude-cli") == 40
def test_engine_keys_are_separate()                     # anthropic-api 기록이 claude-cli 예상에 섞이지 않는다
def test_keeps_last_20()                                # 25건 기록 → 파일의 cuts 목록 길이 20, 마지막 값 보존
def test_corrupt_file_falls_back()                      # timings.json에 "{" → expected는 기본값, record는 예외 없음
def test_write_failure_is_silent(monkeypatch)           # 쓰기에서 OSError → record 예외 없음
def test_unknown_step_rejected()                        # record("x", ...) → ValueError (프로그래머 오류)
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_timings.py -v` → FAIL.
- [ ] **Step 3: Implement** `scripts/timings.py`. 쓰기는 임시 파일 후 `os.replace`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(timings): record step durations and estimate expected time`

---

### Task 2: 작업 항목 진행 추적 `scripts/ui/progress.py`

stack: python
agent: efl:python-developer

**Files:**
- Create: `scripts/ui/progress.py`
- Modify: `scripts/ui/jobs.py` (`Job.snapshot()` 최상위에 `"now": time.time()`)
- Test: `tests/test_ui_progress.py`

**Interfaces:**
- Consumes: `timings.expected`, `timings.record` (Task 1), `Job.update(index, **fields)`.
- Produces:
  - `MIN_INTERVAL = 0.5`
  - `class ItemTiming(job, index: int, rows: list[str], *, engine: str | None = None)` — `rows`는 화면 줄 순서(`["fetch", "cuts", "analyze"]`, 샷 포함 시 `+["shots"]`, 샷 전용은 `["shots"]`). 항목에 `timing = {"video_sec": None, "steps": {row: {"state": "pending", "expected": None}}}`를 둔다.
    - `set_video_sec(sec: float | None)` — 길이 비례 줄의 `expected`를 다시 계산.
    - `start(row: str, sub: str | None = None)` — 줄을 running으로, `started_at=time.time()`, `fraction=None`, `elapsed`는 이 회차 기준. 같은 줄을 다시 start하면(재분석) 새 회차로 리셋. `sub`는 기록 키가 줄과 다를 때(자막 줄의 `"stt"`).
    - `phase(name: str, fraction: float | None, text: str)` — 하위 구간 진행. 진행률은 줄지 않는다(`max`). `MIN_INTERVAL` 안의 반복 호출은 마지막 값만 남기고 건너뛴다(구간 이름이 바뀌면 즉시 반영).
    - `pause()` / `resume()` — 확인 대기 동안의 시간을 경과에서 뺀다(`started_at`을 대기 시간만큼 뒤로 민다).
    - `done(row: str, *, cached: bool = False, **record_kw)` — 줄을 done, `elapsed` 확정. `cached`가 아니면 `timings.record(key, elapsed_sec=..., video_sec=..., engine=..., **record_kw)`. 기록 키는 start의 `sub` 또는 row.
    - `fail(row)` — 줄을 failed로 두고 기록하지 않는다.
  - 단계 진행률은 줄 안 하위 구간 가중 합: `PHASE_WEIGHTS = {"cuts": {"wait": 0.0, "download": 0.2, "scenes": 0.6, "camera": 0.2}, "fetch": {"wait": 0.0, "meta": 0.0}, "stt": {"stt": 1.0}}` (컷 가중치는 실측해 조정 가능). 가중치 0 구간은 `fraction`을 바꾸지 않는다.

- [ ] **Step 1: Write the failing tests** (가짜 Job: update 호출 기록, `time.time`·`time.monotonic` monkeypatch)

```python
def test_rows_start_pending_with_expected()      # set_video_sec(600) 뒤 cuts expected == timings.expected("cuts", video_sec=600)
def test_fraction_never_decreases()              # phase("scenes", 0.5) 후 phase("scenes", 0.3) → 줄 fraction 그대로 0.2+0.6*0.5
def test_updates_are_throttled()                 # 0.1초 간격 phase 10회 → job.update 호출 ≤ 2회 + 구간 변경 1회
def test_pause_excludes_waiting_time()           # start → 10초 → pause → 100초 → resume → 5초 → done → elapsed ≈ 15
def test_restart_resets_row()                    # analyze start → done → start(재분석) → state running, elapsed 없음, 새 started_at
def test_cached_done_not_recorded()              # done("cuts", cached=True) → timings.record 호출 없음, elapsed 0
def test_fail_not_recorded()
def test_snapshot_has_now()                      # jobs.Job(...).snapshot()["now"]가 float
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_ui_progress.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS, 이어서 `tests/test_ui_*.py` 전체(스냅샷 모양 변경 확인).
- [ ] **Step 5: Commit** `feat(ui): track per-step progress on job items`

---

### Task 3: 수집 함수의 진행 콜백

stack: python
agent: efl:python-developer

**Files:**
- Modify: `scripts/fetch.py` (`wait_turn`), `scripts/cuts.py` (`download_video`, `analyze_video`, `cut_stats`), `scripts/stt.py` (`transcribe`)
- Test: `tests/test_cuts.py`, `tests/test_stt.py`, `tests/test_fetch.py`(기존 파일에 추가; 없으면 해당 기존 테스트 파일 이름 확인)

**Interfaces:**
- Produces: 모든 콜백은 `Progress = Callable[[str, float | None, str], None]` (`phase`, `fraction`, `text`).
  - `fetch.wait_turn(progress: Progress | None = None)` — 기다릴 때 `("wait", None, "차단 방지 대기 N초")`를 1초마다.
  - `cuts.cut_stats(video_id, force=False, progress: Progress | None = None)` — 차례로 `wait` → `download`(yt-dlp `progress_hooks`: `downloaded_bytes / (total_bytes or total_bytes_estimate)`, 없으면 None; 글: "영상 받는 중 45%") → `scenes`(읽은 프레임/전체; 글: "장면 확인 중 (38,800 / 62,600 프레임)") → `camera`(처리 장면/전체; 글: "카메라 움직임 확인 중 (300 / 688 장면)").
  - 장면 감지 진행: `scenedetect.detect()` 대신 `open_video` + `SceneManager.detect_scenes(video, callback=None)`을 쓰되, 진행은 별도 스레드가 아니라 `detect_scenes`의 `frame_skip=0` 처리 중 `video.frame_number`를 읽는 방식 등 처리 속도를 떨어뜨리지 않는 방법을 고른다. 결과 장면 목록은 지금과 같아야 한다(기존 테스트 유지).
  - `stt.transcribe(video_id, force=False, progress: Progress | None = None)` — segments를 돌며 `("stt", seg.end / duration, "음성 인식 중 12:30 / 43:31")`.
  - 콜백이 None이면 지금과 똑같이 동작한다.

- [ ] **Step 1: Write the failing tests**

```python
def test_wait_turn_reports_remaining(monkeypatch)      # 남은 대기 2.5초 → progress가 "wait" 단계로 호출, sleep은 짧게 monkeypatch
def test_download_progress_hook()                      # 가짜 hook 사전 {"status":"downloading","downloaded_bytes":50,"total_bytes":100} → ("download", 0.5, "영상 받는 중 50%")
def test_download_progress_without_total()             # total 없음 → ("download", None, ...), 예외 없음
def test_analyze_video_reports_scenes_and_camera(tmp_path)  # 기존 테스트용 작은 영상 픽스처로 scenes 진행이 0→1로 증가, camera 마지막 값 1.0; 결과 scenes는 progress 없이 돌린 결과와 같다
def test_stt_reports_segment_progress(monkeypatch)     # 가짜 model.transcribe가 end 10, 20, 40 segments, duration 40 → fraction 0.25, 0.5, 1.0
def test_callbacks_optional()                          # progress=None 경로가 기존 테스트로 계속 통과 (기존 스위트)
```

- [ ] **Step 2: Run** 해당 테스트 → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_cuts.py tests/test_stt.py` 및 fetch 테스트 → PASS. 실제 영상 하나로 장면 감지 시간이 변경 전과 비슷한지(±10%) 재고 보고서에 적는다.
- [ ] **Step 5: Commit** `feat(collect): report sub-phase progress from fetch, cuts and stt`

---

### Task 4: 레퍼런스 추가 작업에 연결

stack: python
agent: efl:python-developer

**Files:**
- Modify: `scripts/ui/api_add.py` (`_one`, `_transcribe`, `_analyze_and_save`, `_shots_after_save`, `run_shots_only`)
- Test: `tests/test_ui_add.py`

**Interfaces:**
- Consumes: `ItemTiming` (Task 2), 콜백(Task 3), `timings` (Task 1), `llm.config()["engine"]`.
- Produces: 수집 작업 항목의 `timing` 필드(스펙 §5 모양).
  - 줄: `["fetch","cuts","analyze"]` + 옵션 샷이면 `"shots"`; 샷 전용 작업은 `["shots"]`.
  - 영상 길이: 캐시 raw(`fetch.load_raw`) → `no_transcript` 오류의 `details["duration_sec"]` → fetch 결과 메타 `duration` → 컷 통계 `duration_sec` 순.
  - 자막 줄: 메타·자막 받기는 기록 키 `fetch`; STT는 같은 줄을 `start("fetch", sub="stt")`로 새 회차. 자막 없음 결정 대기는 `pause()/resume()`.
  - 확인 대기(`_wait`) 전후로 `pause()/resume()`. 재분석은 `start("analyze")` 새 회차.
  - 분석 기록에 `frames=len(files)`, `text_chars=len(대본 글자)`.
  - 실패·취소 시 해당 줄 `fail`.

- [ ] **Step 1: Write the failing tests** (기존 `tests/test_ui_add.py`의 가짜 fetch/cuts/llm 패턴)

```python
def test_collect_item_has_timing_rows(client, fakes)   # 진행 중 스냅샷의 item["timing"]["steps"] 키 == ["fetch","cuts","analyze"], 저장 후 셋 다 "done"
def test_timing_records_written(client, fakes, home)   # 끝난 뒤 workspace/timings.json에 fetch, cuts, analyze:<engine> 기록
def test_cached_cuts_not_recorded(client, fakes, home) # cut_stats가 status "cached" → cuts 기록 없음, 줄 done
def test_reanalyze_restarts_analyze_row(client, fakes) # 확인에서 reanalyze → analyze 줄 started_at 바뀜, 확인 대기 시간이 elapsed에 없음
def test_shots_only_has_single_row(client, fakes)      # 샷 전용 작업 item["timing"]["steps"] 키 == ["shots"]
def test_failed_step_not_recorded(client, fakes, home) # cut_stats 오류 → cuts 기록 없음
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_ui_add.py` → PASS, 전체 스위트 1회.
- [ ] **Step 5: Commit** `feat(ui): attach step timing to reference collect jobs`

---

### Task 5: 화면 — 단계 줄과 남은 시간

stack: frontend
agent: efl:frontend-developer (로딩: `efl:frontend-convention`, `efl:frontend-design-craft` — React가 아닌 기존 vanilla 구조)

**Files:**
- Create: `scripts/ui/static/progress-utils.js` (import 없음, node로 테스트)
- Modify: `scripts/ui/static/pages/add.js` (`stepChips` 자리를 단계 줄로; `itemSig`에 timing 반영; 1초 시계), `scripts/ui/static/app.css`
- Test: `tests/test_progress_utils_js.py` (node 없으면 skip, `tests/test_remotion_timing.py`의 `run_js` 방식), `tests/test_ui_static.py`

**Interfaces:**
- Consumes: 항목 `timing`(Task 4), 스냅샷 `now`(Task 2).
- Produces (`progress-utils.js`):
  - `fmtDur(sec) -> string` (`0:38`, `8:10`, `1:02:03`; 음수·NaN은 `0:00`)
  - `fmtClock(epochSec, tzOffsetMin?) -> "HH:MM"`
  - `rowView(row, nowSec) -> {elapsed, expected, fraction, over, remaining}` — running이면 elapsed = now - started_at(0 미만이면 0); fraction이 null이면 막대값 = min(1, elapsed/expected); over = expected != null && elapsed > expected; remaining = over ? 0 : (fraction가 0.1 이상이면 elapsed*(1-f)/f, 아니면 expected - elapsed; expected도 없으면 null) — 음수 없음.
  - `itemRemaining(timing, nowSec) -> number | null` — 진행 중 줄 remaining + 남은 pending 줄 expected 합(하나라도 expected null이면 null).
  - 서버 시계 보정: 화면은 `offset = snapshot.now - Date.now()/1000`을 폴링마다 갱신하고 `nowSec = Date.now()/1000 + offset`.

- [ ] **Step 1: Write the failing tests**

```python
def test_fmt_dur()                         # 38 → "0:38", 490 → "8:10", 3723 → "1:02:03", -5 → "0:00"
def test_row_view_measured()               # running, started 302초 전, expected 490, fraction 0.62 → elapsed 302, over False, remaining ≈ 185
def test_row_view_unmeasured_over()        # fraction null, elapsed 220, expected 150 → over True, remaining 0, bar 1
def test_elapsed_uses_server_offset()      # started_at이 now보다 미래(시계 차) → elapsed 0
def test_item_remaining_sums_pending()     # running remaining 185 + pending analyze 150 → 335; pending expected null → null
# test_ui_static.py
def test_static_files_served               # "progress-utils.js" 추가
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `progress-utils.js`, 그리고 add.js 단계 줄(스펙 §6 문구 그대로):
  - 끝난 줄 "✓ 0:38 걸림 (예상 0:40)"(예상 없으면 괄호 생략), 진행 줄 "62% · 5:02 / 예상 8:10"(측정 불가면 % 생략, 예상 없으면 "5:02 · 예상 계산 중"), 다음 줄 "예상 2:30"(없으면 "예상 계산 중").
  - 진행 줄 아래: `phase_text` · "약 3분 남음" · "11:16쯤 끝". 초과면 막대 주황(기존 경고색 변수), "예상보다 길어지는 중", 남은 시간 대신 "곧 끝날 예정".
  - 카드 아래: "이 영상 전체: 약 5분 30초 남음 · 예상 완료 11:19" (itemRemaining null이면 줄 생략).
  - awaiting이면 시계를 멈추고 "확인을 기다리는 중".
  - 1초 타이머는 화면 unmount·작업 종료 시 해제. 다시 그리는 것은 시간 글자와 막대 폭만(카드 전체를 매초 다시 만들지 않는다; 입력 중인 피드백 칸 포커스 보존).
  - 막대: `role="progressbar"`, `aria-valuemin=0`, `aria-valuemax=100`, 측정 시 `aria-valuenow`, 측정 불가 시 `aria-valuetext`.
- [ ] **Step 4: Run** → PASS, 전체 스위트 1회.
- [ ] **Step 5: Commit** `feat(ui): show step progress, remaining time and finish clock on add screen`

---

### Task 6: 문서와 실제 확인

stack: docs
agent: (기본 구현자 — 실제 확인은 컨트롤러)

**Files:**
- Modify: `README.md`(레퍼런스 추가 설명 한 줄), `PLAN.md`(새 Phase, 완료 조건 = 스펙 §7 실제 확인 세 줄, 미체크), `yt-coach-plugin-design.md`(로컬 UI 절 한 단락: timings.json, 진행 표시), `docs/help/add.md`(도움말 브랜치가 main에 있으면 "이렇게 나와요"·"알아 둘 것"에 진행 표시 설명)

- [ ] **Step 1:** 문서 수정.
- [ ] **Step 2: 실제 확인(컨트롤러)** — 임시 작업 폴더에서 10분 이상 실제 영상 하나: 컷 막대가 받기→장면→카메라로 차오르고 줄지 않음, 분석은 경과/예상, `timings.json` 기록, 두 번째 영상 예상에 반영.
- [ ] **Step 3:** 전체 스위트.
- [ ] **Step 4: Commit** `docs: document reference collect progress`
