# 장면 단위 콘티 + 내용 맞춤 레퍼런스 사진 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티의 단위를 "찍을 장면"으로 바꾸고(기존 콘티 자동 합치기 포함), 장면마다 내용이 맞는 레퍼런스 사진(없으면 구도만 맞는 사진)을 붙인다.

**Architecture:** 반복 컷 묶기는 순수 함수(`scripts/scene_merge.py`), 대사 재배치와 사진 고르기는 Claude(글만, 규칙 md), 사진 설명은 기존 `yt-shots` 분류를 모든 대표 프레임으로 넓혀 `desc`를 더한다. 고른 사진은 컷의 `ref_auto`로 저장해 렌더할 때 Claude를 부르지 않는다.

**Tech Stack:** Python 3.11, 기존 `llm.ask_validated`, 로컬 UI(vanilla ES modules, `h()`), pytest + node 헬퍼 테스트.

**Spec:** `docs/superpowers/specs/2026-10-29-scene-conti-design.md`

## Global Constraints

- 의미 판정(대사 나눌 자리, 사진 설명, 사진 고르기)은 Claude, 계산·저장은 스크립트. 스크립트 CLI는 JSON stdout, 실패는 `{"error","message"}` + 0이 아닌 종료.
- AI 출력은 제안: 합치기와 콘티 만들기는 미리보기 → 사용자 저장. 사진 설명(`workspace/shots/<id>.json`)은 기계용 원본층이라 확인 없이 쓴다(기존 shots 라벨과 같음).
- md 본문과 `## 내 메모`는 덮어쓰지 않는다. 기획안 md는 고치지 않는다.
- 사용자 대면 문구는 한국어. 사진 표시: 내용 맞춤 `참고`, 구도 맞춤 `구도만 참고`, 고정 `참고(고정)`.
- 합치기 기준: 같은 섹션 안에서 `action`을 `conti._norm`으로 정규화해 같은 컷(연속 아니어도). 쇼츠도 같음.
- 합친 장면의 `line`을 이으면 원래 컷 순서대로 이은 `line`과 같아야 한다(`_norm` 기준). 글자를 바꾸지 않는다.
- 사진 설명 이미지 상한: 호출당 8장(기존 shots와 같음). 프레임은 이미 저장된 대표 프레임만(영상당 최대 40장).
- 프런트엔드: innerHTML 금지, confirm/alert 금지, 외부 URL 금지, 새 색 토큰 금지(project-notes 참고).
- 커밋은 Conventional Commit, AI 서명 줄 금지.

## Review Focus

1. 고친 대사가 들어 있는 콘티를 합칠 때 대사가 한 글자도 사라지지 않는다 → Task 2 `test_merge_keeps_edited_lines`.
2. 고정 사진(`ref_frame`)이 있는 컷이 합쳐져도 고정이 남는다 → Task 1 `test_merge_keeps_first_pinned_frame`.
3. 사진 설명이 일부 영상에만 있을 때 사진 고르기가 설명 없는 프레임을 내용 맞춤 후보로 쓰지 않는다 → Task 4 `test_candidates_only_described_frames`.
4. Claude가 없는 프레임 경로나 다른 카테고리 프레임을 고르면 버리고 구도 맞춤으로 → Task 4 `test_pick_rejects_unknown_frame`.
5. 반복이 없는 콘티에 합치기를 누르면 아무것도 바꾸지 않고 "합칠 장면이 없어요" → Task 2 `test_merge_nothing_to_merge`.

---

### Task 1: 반복 컷 묶기 (순수 함수)

stack: python · agent: efl:python-developer

**Files:**
- Create: `scripts/scene_merge.py`
- Test: `tests/test_scene_merge.py`

**Interfaces:**
- Produces:
  - `groups(cuts: list[dict]) -> list[list[int]]` — 섹션별로 `_norm(action)`이 같은 컷 번호(n) 묶음, 첫 등장 순서. 묶음 하나짜리도 포함(모든 컷이 정확히 한 묶음).
  - `has_repeats(cuts) -> bool`
  - `merge_fields(members: list[dict]) -> dict` — 묶음의 대표 컷: 첫 컷의 `id`·`section`·`size`·`angle`·`action`; `sec`는 합(0.5 단위 반올림 없이 합 그대로); `guide`·`caption`·`sfx`는 서로 다른 값만 첫 등장 순으로 `" / "`로 이음(빈 값 제외); `ref_frame`은 첫 번째로 있는 값; `line`은 비움(Task 2가 채움); `merged_ids`는 묶인 모든 id(첫 id 포함).
  - `joined_lines(cuts, section: int) -> str` — 그 섹션 컷의 `line`을 원래 순서대로 공백 하나로 이은 글.
  - `transfer_footage(state: dict, id_map: dict[str, str]) -> dict` — 촬영 영상 상태(`footage_match` 형식)에서 사라질 id의 **사용자 판정**(`user: true`)만 남는 id로 옮김(남는 id에 이미 사용자 판정이 있으면 그대로), AI 판정은 사라질 id와 남는 id 모두 삭제. `id_map`: 사라질 id → 남는 id(남는 id 자신은 키에 없음).

- [ ] **Step 1: 실패 테스트** — `test_groups_cycle_within_section`(A B C A B C in section 1, A in section 2 → `[[1,4],[2,5],[3,6],[7]]`), `test_groups_normalizes_action`(공백·문장부호만 다르면 같은 묶음), `test_merge_fields_sums_and_dedups`(sec 2.4×3 → 7.2; guide 같으면 하나, 다르면 `"a / b"`), `test_merge_keeps_first_pinned_frame`, `test_joined_lines_order`, `test_transfer_footage_moves_user_only`(사라질 id의 user 판정 → 남는 id, AI 판정 삭제, 남는 id의 기존 user 판정 유지).
- [ ] **Step 2: 실패 확인** — `pytest tests/test_scene_merge.py -v` → ImportError.
- [ ] **Step 3: 구현** — `conti._norm` 재사용.
- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** `feat(conti): group repeated cuts into scenes`

### Task 2: 합치기 — Claude 대사 배치, CLI, API, 이력

stack: python · agent: efl:python-developer

**Files:**
- Create: `skills/yt-conti/merge.md` (대사 나눌 자리 규칙)
- Modify: `scripts/scene_merge.py` (계획·검증·적용), `scripts/conti.py` (CLI 서브커맨드 `merge`), `scripts/conti_history.py` (`KINDS`에 `"before_merge"`), `scripts/ui/api_conti.py` (라우트), `scripts/footage_match.py` 상태 읽고 쓰기 재사용
- Test: `tests/test_scene_merge.py`, `tests/test_ui_conti.py`

**Interfaces:**
- Consumes: Task 1 전부; `llm.ask_validated`; `conti_history.record(path, fm, kind, ...)`; `footage_match.load_state/save` 계열(실제 이름은 코드에서 확인, STATE_LOCK 안에서).
- Produces:
  - `plan(conti_path) -> dict` — `{"before": n_cuts, "after": n_scenes, "sections": [{"idx", "text": joined, "scenes": [merge_fields...]}]}`; 반복이 없으면 `ScriptError("nothing_to_merge", "합칠 장면이 없어요")`.
  - `validate_split(plan, data) -> list[dict]` — Claude 출력 `{"sections":[{"idx", "order":[scene id...], "lines":[str...]}]}` 검증: 순서는 그 섹션 장면 id의 순열, `lines` 길이 = 장면 수, `_norm("".join(lines)) == _norm(text)`; 아니면 `ScriptError("invalid_merge", ...)`. 결과: 새 cuts 목록(n 다시 매김, `line` 채움, `merged_ids` 제거).
  - `apply(conti_path, cuts, expected_hash) -> dict` — `before_merge` 이력 기록 → 콘티 md 쓰기(기존 `_write_conti_file`/retime 경로로 script.text = 섹션 컷 line 이음, 시간 다시 계산, HTML 다시 렌더) → 촬영 영상 상태 `transfer_footage`. 본문·`## 내 메모` 그대로.
  - CLI: `ytc conti merge <conti.md>` → plan JSON; `ytc conti merge <conti.md> --cuts <json> --hash <h>` → apply.
  - API: `POST /api/conti/merge/preview {path}` → 작업(job kind `conti_merge`, Claude 한 번) → `awaiting` + preview `{before, after, cuts, sections}`; `POST /api/conti/merge/apply {path, cuts, hash}` → apply. 미리보기 중 콘티가 바뀌면 `conti_changed`.

- [ ] **Step 1: 실패 테스트** — `test_validate_split_accepts_permutation`, `test_validate_split_rejects_changed_text`(글자 하나 바뀌면 invalid_merge), `test_merge_keeps_edited_lines`(사용자가 고친 line이 결과 이음에 그대로), `test_merge_nothing_to_merge`, `test_apply_records_before_merge_history`, `test_apply_keeps_memo_and_body`, `test_apply_transfers_footage_user_verdicts`, API `test_merge_preview_then_apply`(llm 가짜), `test_merge_apply_conflict_on_changed_hash`.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** — `merge.md`: 섹션별로 장면 목록(action·guide)과 이어진 대사를 받아 장면 순서와 나눌 자리를 정한다; 글자를 고치지 말 것; 말과 화면이 맞게; 출력 형식 고정 예시. 검증 실패 시 `ask_validated`가 한 번 다시 묻는다(기존 동작).
- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** `feat(conti): merge repeated cuts into scenes with preview and history`

### Task 3: 레퍼런스 사진 설명

stack: python · agent: efl:python-developer

**Files:**
- Modify: `scripts/shots.py`, `skills/yt-shots/SKILL.md`, `scripts/ui/api_add.py` (기존 `_classify_shots` 재사용), 사진 설명 작업 라우트(레퍼런스 쪽 API 파일, 기존 shots 작업 위치를 따름)
- Test: `tests/test_shots.py`, `tests/test_ui_add.py`(또는 해당 UI 테스트)

**Interfaces:**
- Produces:
  - 라벨에 `desc: str`(1~60자, 장소·피사체·행동 한 줄). `apply`가 검증.
  - `shots.describe_context(video_id, category, limit=8) -> dict` — 그 영상의 저장된 대표 프레임 중 아직 `desc`가 없는 것 최대 `limit`장(기존 `context`와 같은 모양 + 이미 라벨이 있으면 그 size/angle 포함).
  - `shots.apply_desc(video_id, category, labels_path) -> dict` — 라벨 파일에 프레임별로 병합(이미 있는 프레임은 `desc`만 갱신, 없는 프레임은 새 라벨 추가). 영상 md 프런트매터 `shots` 비율은 기존 표본 라벨로만 계산(바꾸지 않음).
  - `shots.desc_status(category) -> {"frames": 전체, "described": 설명 있음, "videos_todo": [...]}`.
  - UI 작업 `refs_describe`: 카테고리의 남은 프레임을 8장씩 Claude에 보내 `apply_desc`. 진행률·멈추기(묶음 사이). collect/footage와 동시에 돌지 않게 `jobs` 배타 목록에 추가. 라우트 `GET /api/refs/describe-status?category=`, `POST /api/refs/describe {category}`.
  - 대략 시간 표시용: 묶음당 걸린 시간으로 남은 시간 추정(기존 진행률 유틸 재사용).

- [ ] **Step 1: 실패 테스트** — `test_describe_context_skips_described`, `test_apply_desc_merges_existing_labels`(size 등 유지, desc만 추가), `test_apply_desc_rejects_empty_or_long_desc`, `test_desc_status_counts`, `test_shots_ratio_unchanged_by_desc`, UI `test_describe_job_runs_batches_and_stops`, `test_describe_exclusive_with_collect`.
- [ ] **Step 2–4: 실패 확인 → 구현 → 통과.**
- [ ] **Step 5: 커밋** `feat(refs): describe every reference frame for scene matching`

### Task 4: 장면에 사진 고르기

stack: python · agent: efl:python-developer

**Files:**
- Create: `scripts/conti_refs.py`, `skills/yt-conti/pick-refs.md`
- Modify: `scripts/conti.py` (`validate`가 `ref_auto` 보존, `cut_refs` 순서, `scenes()` 후보에 `desc`), `scripts/ui/api_conti.py` (만들기 작업에서 고르기, `POST /api/conti/refs/pick`)
- Test: `tests/test_conti_refs.py`, `tests/test_conti.py`, `tests/test_ui_conti.py`

**Interfaces:**
- Consumes: Task 3 라벨 `desc`; `frame_pool`.
- Produces:
  - 컷 필드 `ref_auto: {"frame": <rel path>, "match": "content"}` (선택). `validate`와 저장 경로가 보존. 사용자가 사진을 고정하면(`ref_frame`) 그대로 우선.
  - `conti_refs.candidates(category) -> list[dict]` — `desc`가 있는 같은 카테고리 프레임만 `{frame, desc, size, angle}`.
  - `conti_refs.pick(cuts, category) -> dict[str, str]` — 컷 id → frame. Claude(글만, 이미지 없음)에 장면(id, action, guide, size)과 후보 목록을 주고 장면마다 frame 또는 null. 장면이 많으면 40장면씩 나눠 부름. 후보에 없는 frame은 버림. 같은 사진은 되도록 반복하지 않게 규칙에 적음.
  - `conti_refs.apply_picks(cuts, picks) -> list[dict]` — `ref_auto` 설정/제거.
  - `cut_refs(fm)` 순서: 고정(`pinned`) → `ref_auto`(파일 있음, `content`) → 기존 구도 맞춤(`match_frames`, match는 `composition`으로 표시). 반환 dict에 `desc`(라벨에서) 포함.
  - 만들기 작업: 콘티 검증 뒤 후보가 있으면 `pick` → preview의 cuts에 `ref_auto`. 실패하면 사진 없이 계속(경고 `refs_pick_failed`).
  - `POST /api/conti/refs/pick {path, hash}` → 작업(Claude) → 미리보기 없이 바로 `ref_auto`만 저장(사진 선택은 콘티 내용이 아니라 표시용 — 내 판단: 확인 단계 생략, 이력 auto 스냅샷은 남김).

- [ ] **Step 1: 실패 테스트** — `test_candidates_only_described_frames`, `test_pick_rejects_unknown_frame`, `test_pick_batches_40`, `test_cut_refs_order_pinned_auto_composition`, `test_cut_refs_missing_auto_file_falls_back`, `test_validate_keeps_ref_auto`, `test_scenes_includes_desc`, UI `test_create_preview_has_ref_auto`(llm 가짜), `test_refs_pick_route_saves_ref_auto`.
- [ ] **Step 2–4: 실패 확인 → 구현 → 통과.**
- [ ] **Step 5: 커밋** `feat(conti): pick content-matched reference photos per scene`

### Task 5: 화면 — 스토리보드 HTML과 콘티 편집기

stack: frontend · agent: efl:frontend-developer

**Files:**
- Modify: `scripts/conti.py` (`render_html`, `ref_figure`), `scripts/ui/static/pages/conti.js`, 필요하면 `scripts/ui/static/conti-utils.js`, `scripts/ui/static/app.css`
- Test: `tests/test_conti.py`(HTML), `tests/test_ui_static.py`, `tests/test_conti_utils_js.py`

**Interfaces:**
- Consumes: Task 2 라우트(`/api/conti/merge/preview`, `/apply`), Task 3 라우트(`/api/refs/describe-status`, `/api/refs/describe`), Task 4 라우트(`/api/conti/refs/pick`), `cut_refs`의 `match`/`desc`.

- [ ] **Step 1: 실패 테스트** — HTML: 사진이 있으면 도식 `<svg class="sk">`가 없음, 사진이 하나도 없으면 도식이 있음; figcaption이 `참고 · <desc>` / `구도만 참고 · <desc>` / `참고(고정)`; 머리말 `장면 N개`; 카드에 길이 `약 25초`. 정적 테스트: 합치기 버튼·미리보기(전/후 개수, 섹션별 장면과 대사)·저장, "사진 설명 만들기" 안내(`desc_status`가 전부가 아니면) + 진행률, "사진 다시 고르기", 사진 바꾸기 후보에 설명 표시.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** — 기존 편집기 패턴(작업 진행률, 미리보기 → 저장, conflict 처리, 저장 후 `remapByCutId`로 초안 보존)을 따른다. 합친 뒤에는 편집기를 다시 불러온다.
- [ ] **Step 4: 통과 + 실제 화면 확인** — 테스트 데이터 사본(경전철 콘티)에서 합치기 미리보기와 스토리보드 380px/1280px, light/dark 스크린샷.
- [ ] **Step 5: 커밋** `feat(ui): scene merge, photo descriptions and content-matched photos in the conti editor`

### Task 6: 규칙·문서

stack: docs

**Files:** `skills/yt-draft/SKILL.md` 3절, `skills/yt-conti/SKILL.md` 2·3절, `docs/help/conti.md`, `yt-coach-plugin-design.md`, `README.md`(CLI 표에 `conti merge`)
- [ ] yt-draft: "샷 하나 = 찍을 장면 하나, 같은 장면을 반복해 적지 않는다, 길이는 찍어 쓸 분량(롱폼 대개 10~60초, 쇼츠 1~5초), 아키타입 평균 컷 길이는 편집 메모 근거" — "같은 샷이 연속 반복되면 하나씩 다 적는다" 줄 삭제.
- [ ] yt-conti: 장면 하나 = 컷 하나; 기획안의 같은 섹션 같은 action 샷은 한 장면으로 묶어 길이를 합친다; `guide`에 편집 메모 가능.
- [ ] 도움말: 장면 단위, 합치기(미리보기·이력으로 되돌리기), 사진 설명 만들기(약 105번 호출·한 번), 사진 표시 세 종류, 사진 다시 고르기.
- [ ] 전체 테스트 → 커밋 `docs(conti): scene-based conti and matched reference photos`
