# 콘티 이력 + 컷 편집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티에 한 줄 이력(자동 묶음·이름 붙이기·비교·되돌리기)을 붙이고, 화면에서 컷을 넣기·빼기·옮기기·복제하고 길이·샷 크기·장면 설명까지 고칠 수 있게 한다.

**Architecture:** 컷마다 오래 남는 `id`를 둔다(`schema_version` 3). 새 모듈 `scripts/conti_history.py`가 `workspace/conti_history/<stem>/NNNN.json`에 상태를 쌓고, `scripts/conti.py`의 모든 쓰기 경로(save·update_cut·새 컷 편집)가 쓰기 뒤 `conti_history.record(...)`를 부른다. 컷 편집은 `conti.edit_cuts(path, op, expected_hash)` 하나로 모으고, 편집 뒤 섹션 시간은 컷 길이 합으로 다시 쓴다. 화면은 `conti.js` 편집기에 섹션 머리·끌어 놓기·⋯ 메뉴·이력 패널을 더한다.

**Tech Stack:** Python 3.11 표준 라이브러리, pytest; 빌드 없는 ES 모듈 + `h()`; 순수 함수는 node로 pytest에서 테스트.

**Spec:** `docs/superpowers/specs/2026-10-27-conti-history-and-editing-design.md` (A·B절 + C절의 결정이 B절보다 우선)

## Global Constraints

- `## 내 메모`와 본문 서술은 절대 덮어쓰지 않는다. 콘티 쓰기는 지금 `update_cut`처럼 프런트매터 + 다시 그린 본문 표, 메모·줄바꿈(CRLF/LF) 보존.
- 모든 쓰기는 파일 hash 확인(다르면 `ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")`).
- 섹션마다 컷이 하나 이상(마지막 컷 삭제·밖으로 이동 금지: `ScriptError("last_cut", "섹션에 컷이 하나는 있어야 해요")`).
- 컷 길이 0.5~120초, 0.5초 단위. 샷 크기는 `features.SHOT_SIZES`만. `action`·`guide`는 비울 수 없음.
- 새 컷 기본값: `size "미디엄"`, `sec 4`, `action "새 컷"`, `guide "촬영 지시를 적어 주세요"`.
- 자동 이력 묶음 시간 5분(`GROUP_SEC = 300`), 묶음 덮어쓰기는 최신 버전 `kind == "auto"`일 때만.
- 이름 1~30자, 후보 "기획 완료", "촬영 후", "편집 중".
- AI 호출 없음. 사용자 문구 한국어. 새 색 토큰 없음. 외부 주소 없음.

## Review Focus

1. 옛 콘티(id 없음, schema 2)를 처음 열면 id가 붙어 저장되고 메모·줄바꿈이 그대로이며, 그 쓰기가 `external` 이력을 만들지 않는다(Task 1·2 테스트).
2. 사용자가 md에서 메모만 고친 뒤 열면 이력이 늘지 않고, 컷 대사를 고친 뒤 열면 `external`이 하나 생긴다(Task 2 테스트).
3. 컷을 다른 섹션으로 옮기거나 길이를 바꾼 뒤 SRT 내보내기·스토리보드 HTML·`GET /api/conti`가 오류 없이 새 시간으로 나온다(Task 3 테스트).
4. 되돌린 뒤 바로(5분 안) 편집하면 `restored` 버전이 덮어써지지 않고 새 `auto`가 생긴다(Task 2 테스트).
5. 낡은 화면이 보낸 `n`/hash로는 쓰지 못한다(`conti_changed`)(Task 3 테스트).

---

### Task 1: 컷 id와 편집 뒤 섹션 시간 다시 계산

stack: python · agent: efl:python-developer

**Files:** Modify `scripts/conti.py` · Test `tests/test_conti.py`

**Interfaces:**
- Produces:
  - `SCHEMA_VERSION = 3`. `validate(data, sections)`는 컷의 `id`(문자열)를 보존하고, 없으면 `new_cut_id() -> str`(8자 소문자+숫자 무작위)로 붙인다.
  - `retime(fm: dict) -> dict` — `fm["cuts"]` 순서·길이로 `fm["script"][i]["start"/"end"]`를 다시 계산(섹션 0은 0에서 시작, 각 섹션 길이 = 그 섹션 컷 `sec` 합)하고 `validate`로 컷 `n/start/end`를 다시 붙인 새 fm을 돌려준다. 섹션 `text`도 그 섹션 컷 `line`을 이어 다시 만든다.
  - `ensure_ids(path) -> bool` — 파일 컷에 id가 없으면 붙여 쓰고(메모·줄바꿈 보존, HTML 다시 그림) True. 이미 있으면 False.
  - `_load_conti`는 그대로 검증하되 id를 보존한다.
- [ ] Step 1 테스트: `test_validate_keeps_and_assigns_ids`(있는 id 유지, 없는 컷에 8자 id, 서로 다름), `test_retime_moves_later_sections`(섹션 0 컷 하나 +2초 → 섹션 1 start/end +2, 컷 n/start/end 연속), `test_ensure_ids_migrates_v2_keeping_memo_and_crlf`(CRLF 파일 + 메모 → id 붙고 schema 3, 메모·CRLF 그대로, 두 번째 호출 False).
- [ ] Step 2 실패 확인 → Step 3 구현 → Step 4 `pytest tests/test_conti.py tests/test_ui_conti.py -q` PASS
- [ ] Step 5 Commit `feat(conti): stable cut ids, schema 3 and section times recomputed from cut lengths`

### Task 2: 콘티 이력 모듈

stack: python · agent: efl:python-developer

**Files:** Create `scripts/conti_history.py` · Modify `scripts/conti.py`(save·update_cut·trash가 이력을 남기고 옮김) · Test `tests/test_conti_history.py`

**Interfaces:**
- Consumes: Task 1 id·`retime`.
- Produces (`scripts/conti_history.py`):
  - `history_dir(conti_path) -> Path` = `workspace/conti_history/<stem>/`.
  - `state_of(fm) -> dict` = `{"script", "cuts", "target_sec"}`; `same_state(a, b) -> bool`(script label·text·start·end 소수 둘째 자리, cuts는 id 뺀 필드, target_sec).
  - `record(conti_path, fm, kind, name=None, now=None) -> dict` — kind ∈ `created|auto|named|before_recreate|external|restored`. `auto`이고 최신이 `auto`이며 `now - last_touch <= GROUP_SEC`면 최신을 덮어씀, 아니면 새 `NNNN.json`. 돌려주는 값은 버전 요약.
  - `list_versions(conti_path) -> list[dict]`(최신순, state 없이: n, kind, name, at, summary{cuts,total_sec,changed}), `get_version(conti_path, n) -> dict`.
  - `check_external(conti_path, fm, now=None) -> dict | None` — 이력이 없으면 `created`를 만들고 None; 최신 state와 다르면 `external` 기록.
  - `diff(a_state, b_state) -> dict` = `{"changes": [{"kind": "changed"|"added"|"removed"|"moved"|"total", "cut": id, "n_from", "n_to", "field", "from", "to"}], "left": cuts, "right": cuts}`; id 없는 상태끼리는 위치로.
  - `restore(conti_path, n, expected_hash) -> dict` — 그 버전 state로 프런트매터를 쓰고(메모 보존, `retime`, HTML), `restored` 기록.
  - `name(conti_path, label, expected_hash) -> dict` — 라벨 1~30자 검사 후 `named`.
- conti.py 연결: `save` 성공 뒤 `created`(force로 덮어쓸 때는 쓰기 **전에** `before_recreate`), `update_cut` 뒤 `auto`, `trash`는 이력 폴더도 같은 묶음으로 옮김. CLI `history`, `history-name`, `history-restore`, `history-diff`.
- [ ] Step 1 테스트(`now`를 주입해 시간 제어): 묶음 덮어쓰기(4분 뒤 같은 버전, 6분 뒤 새 버전), `restored` 뒤 편집은 새 auto(Review Focus 4), 메모만 바뀜 → external 없음 / 대사 바뀜 → external 1개(Review Focus 2), id 처음 붙이기는 external 아님(Review Focus 1), 다시 만들기 → before_recreate 뒤 created, restore가 메모 보존·hash 다르면 conti_changed, name 길이 검사, diff의 added/removed/moved/changed(line, sec)/total, trash가 이력 폴더도 옮김.
- [ ] Step 2~4 → `pytest tests/test_conti_history.py tests/test_conti.py -q` PASS
- [ ] Step 5 Commit `feat(conti): linear history with grouped auto snapshots, names, diff and restore`

### Task 3: 컷 편집 연산과 API

stack: python · agent: efl:python-developer

**Files:** Modify `scripts/conti.py`, `scripts/ui/api_conti.py` · Test `tests/test_conti.py`, `tests/test_ui_conti.py`

**Interfaces:**
- Consumes: Task 1·2.
- Produces:
  - `conti.edit_cuts(conti, op: dict, expected_hash: str) -> dict` — op: `{"op": "insert", "at": int(0~len), "section": int?, "cut": dict?}` (section 생략 = 앞 컷의 섹션, 맨 앞이면 0), `{"op": "delete", "id"}`, `{"op": "move", "id", "to": int, "section": int?}`, `{"op": "duplicate", "id"}`(바로 뒤에, 새 id), `{"op": "update", "id", "fields": {...}}`(EDITABLE_FIELDS + `sec`, `size`, `action`). `id` 대신 `n`도 받는다. 섹션 순서가 거꾸로 되는 이동은 그 위치의 섹션으로 소속을 바꿔 맞춘다(끌어 놓은 자리의 앞 컷 섹션, 맨 앞이면 다음 컷 섹션). 결과는 `retime` → 쓰기(메모 보존) → HTML → `conti_history.record(..., "auto")`. 반환: `{"status": "updated", "hash", "cut_id"(새로 생긴/고친 컷), "warnings"}`.
  - `update_cut`는 `edit_cuts(update)`를 부르는 얇은 함수로(기존 시그니처·반환 유지).
  - `POST /api/conti/cuts {path, hash, op...}` → `GET /api/conti`와 같은 모양 + `cut_id`. `POST /api/conti/cut`은 그대로 동작.
  - 이력 라우트: `GET /api/conti/history?path=`, `GET /api/conti/history/version?path=&n=`, `GET /api/conti/history/diff?path=&a=&b=`(b 없으면 지금), `POST /api/conti/history/name {path, name, hash}`, `POST /api/conti/history/restore {path, n, hash}`.
  - `GET /api/conti`: 응답 전에 `ensure_ids` + `check_external`. 응답에 `total_sec`, `target_sec`, `outputs_stale: {"srt": bool, "video": bool}`(콘티 mtime > 해당 산출물 mtime이고 산출물이 있을 때).
- [ ] Step 1 테스트: insert(기본값, 섹션 지정), delete(마지막 컷이면 `last_cut`), move(섹션 넘어 소속 변경, 마지막 컷 밖으로 금지), duplicate(새 id), update sec(0.5 단위·범위 밖 거부)·size(어휘 밖 거부)·action(빈 값 거부), 낡은 hash `conti_changed`(Review Focus 5), 편집 뒤 `export_srt`·`render`·`GET /api/conti` 정상 + 새 시간(Review Focus 3), 이력 라우트 왕복(이름 → 목록 → diff → restore), `outputs_stale`.
- [ ] Step 2~4 → `pytest tests/test_conti.py tests/test_ui_conti.py tests/test_conti_history.py -q` PASS
- [ ] Step 5 Commit `feat(conti): insert, delete, move, duplicate and resize cuts with history routes`

### Task 4: 편집 화면 — 섹션 머리, 끌어 놓기, 넣기, ⋯ 메뉴, 길이

stack: frontend · agent: efl:frontend-developer

**Files:** Modify `scripts/ui/static/pages/conti.js`, `scripts/ui/static/app.css` · Create `scripts/ui/static/conti-utils.js` · Test `tests/test_conti_utils_js.py`(Create), `tests/test_ui_static.py`

**Interfaces:**
- Consumes: Task 3 `POST /api/conti/cuts`, `GET /api/conti`의 `total_sec`·`target_sec`·`outputs_stale`.
- Produces:
  - `conti-utils.js`: `fmtDelta(totalSec, targetSec) -> string`(예 `"목표 15:00 → 지금 15:08 (+0:08)"`, 같으면 `"목표 15:00 · 지금 15:00"`), `canRemove(cuts, id) -> boolean`(그 섹션 컷이 2개 이상), `dropTarget(cuts, fromId, toIndex) -> {to, section}`(백엔드 규칙과 같은 섹션 결정), `snapSec(x) -> number`(0.5 단위, 0.5~120).
  - 컷 목록: 섹션 머리(섹션 이름 · 시작–끝), 줄마다 손잡이(⋮⋮, `draggable`), 번호·샷 크기·길이·대사 요약, `⋯` 메뉴 버튼(`aria-haspopup="menu"`; 항목: 위에 넣기, 아래에 넣기, 복제, 삭제 — 삭제는 `canRemove`가 false면 끄고 이유 title). 줄 사이 `＋ 여기에 컷 넣기`(hover·focus-within에서 보임, 키보드로 도달 가능).
  - 선택한 컷이 있을 때 목록 위 도구: `위로`, `아래로`(섹션 경계를 넘으면 소속이 바뀜, 첫/마지막이면 끔).
  - 편집 칸: 기존 필드 + `장면 설명`(action), `길이(초)`(number, step 0.5), `샷 크기`(select).
  - 삭제 뒤 토스트 `컷 N을 지웠습니다` + `되돌리기` 버튼(8초, 누르면 지운 컷을 같은 자리에 insert로 되살림 — 같은 id 유지).
  - 머리: `fmtDelta` 한 줄. `outputs_stale`이면 SRT·자막 영상 칸에 "콘티가 바뀌었어요 — 다시 받아 주세요".
  - 각 조작은 `POST /api/conti/cuts` 후 응답으로 다시 그리고, 포커스는 새/옮긴 컷(삭제면 같은 자리의 다음 컷).
- [ ] Step 1 테스트: node — fmtDelta 세 경우, canRemove, dropTarget(섹션 경계·맨 앞·맨 뒤), snapSec(0.3→0.5, 121→120, 4.26→4.5). 정적 — conti.js에 `/api/conti/cuts`, `draggable`, `"aria-haspopup"`, `"여기에 컷 넣기"`, `"되돌리기"`, `"위로"`, `"아래로"`, `fmtDelta(`; 금지 목록(innerHTML·confirm 등) 없음.
- [ ] Step 2~4 → `pytest tests/test_conti_utils_js.py tests/test_ui_static.py tests/test_ui_conti.py -q` PASS. 테스트 서버(복사한 작업 폴더)로 넣기·빼기·옮기기·되돌리기를 실제로 해 보고 1280·380px 캡처.
- [ ] Step 5 Commit `feat(ui): edit the conti cut list — insert, delete with undo, drag or button reorder, length and shot size`

### Task 5: 이력 패널 — 목록, 이름 붙이기, 비교(목록·나란히), 되돌리기

stack: frontend · agent: efl:frontend-developer

**Files:** Modify `scripts/ui/static/pages/conti.js`, `scripts/ui/static/conti-utils.js`, `scripts/ui/static/app.css` · Test `tests/test_conti_utils_js.py`, `tests/test_ui_static.py`

**Interfaces:**
- Consumes: Task 3 이력 라우트.
- Produces:
  - `conti-utils.js`: `KIND_TEXT`(`created: "처음 만듦", auto: "자동", named: <이름>, before_recreate: "다시 만들기 전", external: "md 직접 수정", restored: "되돌림"`), `changeText(change) -> string`(예 `컷 3 대사: 「A」 → 「B」`, `컷 7 삭제`, `컷 9 새로 생김(본문)`, `컷 4 → 6으로 옮김`, `컷 2 길이 4초 → 6초`, `총 길이 15:00 → 15:08`).
  - 머리 `이력` 버튼(`aria-expanded`) → 오른쪽 패널(좁으면 덮개, Esc로 닫고 포커스 복귀): 위에 `지금 상태에 이름 붙이기`(후보 3개 + 직접 입력), 아래 최신순 목록(시각, 이름/종류, 컷 수·총 길이, 바뀐 컷 수, 체크박스로 최대 2개 선택).
  - 하나 선택: `지금과 비교`, `이 버전으로 되돌리기`(인라인 확인 "지금 상태도 이력에 남아 있어 다시 되돌릴 수 있어요"). 둘 선택: `둘 비교`.
  - 비교 보기: 기본 바뀐 점 목록(`changeText`), 토글 `나란히 보기`(두 컷 목록, 바뀐 칸 강조, 빠진 컷 회색 빈 줄, 새 컷 표시 — 색만이 아니라 글자 표시도).
- [ ] Step 1 테스트: node — KIND_TEXT, changeText 6종. 정적 — `/api/conti/history`, `"이름 붙이기"`, `"나란히 보기"`, `"되돌리기"`, `aria-expanded`, Esc 처리(`"Escape"`).
- [ ] Step 2~4 → 위 + `tests/test_ui_conti.py` PASS. 테스트 서버로 이름 붙이기 → 편집 → 비교(두 보기) → 되돌리기 → 다시 되돌리기, 1280·380·다크 캡처.
- [ ] Step 5 Commit `feat(ui): conti history panel with names, change list or side-by-side compare, and restore`

### Task 6: 문서

stack: docs

**Files:** `docs/help/conti.md`(이력·컷 편집 사용법, 묶음 5분, 메모는 되돌리기와 무관, 섹션 마지막 컷), `yt-coach-plugin-design.md`(콘티 절: schema 3·컷 id·이력 위치·편집 규칙), `README.md`(CLI `history*` 하위 명령이 표에 있으면).
- [ ] Step 1 고치기 → Step 2 `pytest tests -q` 전체 PASS → Step 3 Commit `docs(conti): history and cut editing`
