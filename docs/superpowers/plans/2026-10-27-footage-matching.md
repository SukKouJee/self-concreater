# 촬영 영상 ↔ 콘티 매칭 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티에 촬영 날짜 폴더들을 연결해, 로컬 처리(장면·사진·받아쓰기) 뒤 컷마다 찍음/확인 필요/남은 컷을 판정하고, 폰용 남은 컷 HTML·위치 CSV·컷 파일을 만든다.

**Architecture:** `scripts/footage.py`(폴더 스캔·파일 지문·장면/사진/모음 이미지·받아쓰기 캐시), `scripts/footage_match.py`(글자 후보 → Claude 묶음 호출 → 판정 저장, 새 컷 후보), `scripts/footage_out.py`(폰용 HTML, CSV, 컷 파일 재포장). UI 서버에 `scripts/ui/api_footage.py`(라우트·작업 kind `footage`/`footage_clips`·지문 id 미디어 서빙)를 더하고, 콘티 편집 화면에 `촬영 영상` 영역(`static/footage-area.js` + `footage-utils.js`)을 붙인다.

**Tech Stack:** Python 3.11, OpenCV(scenedetect), PyAV, faster-whisper(선택), 기존 `llm.ask_validated`; 빌드 없는 ES 모듈 + `h()`; pytest(+node로 순수 JS).

**Spec:** `docs/superpowers/specs/2026-10-27-footage-matching-design.md` (A~E + F의 결정이 우선)

## Global Constraints

- 원본 영상은 읽기만 한다. 쓰기는 작업 폴더 안(`workspace/footage/`, `workspace/footage_match/`, `drafts/`)에만.
- 자동 '찍음'은 판정 표시뿐, 콘티 md는 사용자가 누를 때만(`conti.edit_cuts`) 바뀐다. `## 내 메모`·본문 불변.
- AI 이미지: 모음 이미지 1장 = 1장, 호출당 8장, 콘티 한 번 처리에 120장.
- 작업 kind `footage`·`footage_clips`는 각각 하나만, collect와 동시 실행 금지(`busy`).
- 받아쓰기: `YT_STT_MODEL`(기본 small), CPU int8, 언어 `ko`.
- 확장자 .mp4 .mov .m4v .mkv(대소문자 무시), 하위 폴더 제외. 파일 지문 = sha1(정규화 절대경로) 앞 12자 + 크기 + mtime_ns.
- 판정 kind: `auto`(찍음 자동), `pinned`(사용자 고정 찍음), `review`(확인 필요), `missing`(남은 컷), 사용자 `not_filmed`(못 찍음 고정). 사용자 판정은 다시 처리해도 덮어쓰지 않음.
- 미디어 서빙은 지문 id로만, 연결 폴더 안 실제 파일만, Range 지원.
- 사용자 문구 한국어, 외부 주소 없음, shadcn 토큰만.

## Review Focus

1. HEVC .mov처럼 OpenCV가 못 여는 파일이 있어도 나머지 파일 처리와 판정이 계속된다(Task 1 테스트: 깨진 파일 하나 섞기).
2. 사용자가 고른 판정(pinned/not_filmed)은 폴더를 더하고 다시 처리해도 그대로다(Task 2 테스트).
3. 미디어 라우트로 연결 폴더 밖 파일(.. 경로, 다른 지문)을 받을 수 없다(Task 3 테스트).
4. 원본 파일을 지운 뒤 화면·CSV·컷 파일 만들기가 오류 없이 `원본 없음`을 보인다(Task 4 테스트).
5. 이미지 상한(120장)에 걸린 컷은 `review`로 남고 이유가 붙는다(Task 2 테스트).

---

### Task 1: footage 처리 모듈 (로컬, 비용 0)

stack: python · agent: efl:python-developer

**Files:** Create `scripts/footage.py` · Modify `scripts/stt.py`(로컬 파일 받아쓰기 함수) · Test `tests/test_footage.py`

**Interfaces (Produces):**
- `scan_folder(folder: str|Path) -> list[dict]` — `{"path", "name", "size", "mtime_ns", "fid"}`(정렬: 이름순). 없는 폴더 → `ScriptError("folder_not_found")`.
- `fingerprint(path) -> str`(fid). `footage_dir(fid) -> Path` = `workspace/footage/<fid>/`.
- `process_file(path, progress=None, stt=True) -> dict` — 캐시 있으면 그대로. 결과 `{"fid", "status": "done"|"unreadable", "reason"?, "duration_sec", "scenes": [{"i","start","end","frames":[rel...],"montage": rel}], "transcript": [{"start","end","text"}] | None}`. 장면: 360p 이하·5fps 샘플로 OpenCV 컷 탐지(실패 시 PyAV 디코딩), 장면마다 사진 4~6장(긴 변 320px JPEG), 모음 이미지 1장(사진들을 격자로, 긴 변 960px). 받아쓰기는 faster-whisper 없으면 `transcript: None` + `stt_missing: true`.
- `stt.transcribe_file(path, lang="ko", progress=None) -> list[dict]` (PyAV 16kHz mono 디코딩 재사용).
- CLI: `python -m scripts.footage scan <folder>` / `process <file>` (JSON).
- [ ] 테스트: scan(확장자·대소문자·하위 폴더 제외), fingerprint 안정성(같은 파일 같은 fid, mtime 바뀌면 다름), process 캐시 재사용(두 번째는 디코딩 안 함), 깨진 파일 → unreadable + 다른 파일 계속(Review Focus 1), stt 없음 경로(faster-whisper import 실패를 monkeypatch). 기존 `tests/test_cuts.py`의 `sample_video` fixture 사용.
- [ ] 구현 → `pytest tests/test_footage.py tests/test_cuts.py tests/test_stt.py -q` → 커밋 `feat(footage): scan shoot folders and extract scenes, frames and transcripts locally`

### Task 2: 맞추기와 판정 저장

stack: python · agent: efl:python-developer

**Files:** Create `scripts/footage_match.py`, `skills/yt-conti/footage-match.md`(Claude에게 줄 규칙, 한국어) · Test `tests/test_footage_match.py`

**Interfaces:**
- Consumes: Task 1 결과, `conti._load_conti`/컷 id, `llm.ask_validated`.
- Produces:
  - 저장 `workspace/footage_match/<conti stem>.json`: `{"folders": [abs paths], "cuts": {cut_id: {"kind", "choice": {"fid","start","end"}|null, "candidates": [{"fid","start","end","score","why","text"}], "reason"?, "user": bool}}, "suggestions": [{"fid","start","end","text","line","guide","size","hidden": bool}], "images_used": int}`.
  - `load_state(conti) -> dict`, `save_state(conti, state)`(원자적 쓰기).
  - `text_candidates(cut, transcripts, k=5) -> list`(글자 유사도, 내 판단: 문자 2-gram 자카드 + 시간 근접 없음).
  - `match(conti, files_results, now=None) -> dict` — 사용자 판정(`user: true`) 컷은 건너뜀. 컷을 묶어 Claude 호출(대사 컷: 글자만, 대사 없는 컷: 모음 이미지, 호출당 8장, 처리당 120장). 응답 스키마: 컷별 `[{fid, start, end, confidence: high|medium|low, why}]` 최대 3개 + `suggestions`. high → `auto` 선택, 없으면 `review`, 후보 0 → `missing`. 상한으로 못 본 컷 → `review` + reason "사진 상한으로 AI가 보지 못함".
  - 사용자 조작: `choose(conti, cut_id, fid, start, end)` → `pinned`; `mark_not_filmed(conti, cut_id)`; `reset(conti, cut_id)`(user 해제); `hide_suggestion(conti, idx)`.
  - 지운 컷 id의 판정은 남기고 `visible_cuts(conti, state)`가 지금 콘티 컷만 돌려준다.
- [ ] 테스트(llm 가짜): 판정 세 가지, 사용자 판정 보존(Review Focus 2), 이미지 상한(Review Focus 5 — 120장 넘는 경우), 컷 삭제 후 숨김·복원 시 되살아남, 새 컷 후보·숨기기, 저장 원자성.
- [ ] 구현 → `pytest tests/test_footage_match.py -q` → 커밋 `feat(footage): match shot footage to conti cuts by meaning with grouped Claude calls`

### Task 3: API, 작업, 미디어 서빙

stack: python · agent: efl:python-developer

**Files:** Create `scripts/ui/api_footage.py` · Modify `scripts/ui/jobs.py`(SINGLE_KINDS에 footage·footage_clips), `scripts/ui/api_add.py`(collect 시작 시 footage 활성이면 busy), `scripts/ui/server.py`(모듈 등록) · Test `tests/test_ui_footage.py`

**Interfaces (Produces):**
- `GET /api/footage?path=<conti>` → `{folders:[{path, files, duration_sec, processed, unreadable, missing}], summary:{filmed, review, missing}, cuts:[{id, n, kind, choice, candidates, reason, user}], suggestions:[...], stt_missing: bool, outputs:{remaining_html, csv, clips_dir}}`(지금 콘티 컷만).
- `POST /api/footage/folders {path, folder}` 더하기(폴더 존재·디렉터리 확인) / `POST /api/footage/folders/remove {path, folder}`.
- `POST /api/footage/process {path}` → 작업 kind `footage`(새 파일 처리 → 맞추기). collect 활성이면 `busy` "레퍼런스 분석이 끝난 뒤 시작해 주세요"; 반대로 footage 활성 중 collect 시작은 "촬영 영상 처리가 끝난 뒤 시작해 주세요". 진행 항목은 파일 단위(+ 마지막 '맞추기' 항목).
- `POST /api/footage/choose {path, cut, fid, start, end}`, `/not-filmed {path, cut}`, `/reset {path, cut}`, `/suggestions/hide {path, index}`, `/suggestions/insert {path, index, after_cut, hash}` → `conti.edit_cuts` insert(제안 대사·가이드·샷 크기) 후 그 컷을 해당 구간으로 pinned. 응답에 새 콘티 hash.
- `GET /api/footage/media/<fid>` — 연결 폴더 안 파일만(실제 경로 재확인, 폴더 빼면 404), Range 지원(206), Content-Type 확장자별. `GET /api/footage/frame/<fid>/<rel>` 사진.
- [ ] 테스트: 라우트 왕복(가짜 처리/llm), busy 양방향, 미디어 Range·밖 경로 거부·빼기 후 404(Review Focus 3), 제안 넣기 → 콘티 컷 증가 + 이력 auto + pinned.
- [ ] 구현 → `pytest tests/test_ui_footage.py tests/test_ui_add.py tests/test_ui_server.py -q` → 커밋 `feat(footage): footage API, background job and safe media serving`

### Task 4: 결과물 — 폰용 남은 컷, CSV, 컷 파일

stack: python · agent: efl:python-developer

**Files:** Create `scripts/footage_out.py` · Modify `scripts/ui/api_footage.py` · Test `tests/test_footage_out.py`

**Interfaces (Produces):**
- `remaining_html(conti) -> rel path` → `drafts/<기획안>-remaining.html`: 외부 자원 없는 한 파일(이미지 base64), 맨 위 만든 시각·"찍음 N/M", 남은 컷·확인 필요 컷만(번호·섹션·샷 크기 도식(conti.sketch)·참고 프레임·대사·가이드). 폰 폭에서 읽기 좋게.
- `csv_export(conti) -> rel path` → `drafts/<기획안>-footage.csv`(UTF-8 BOM, 열: 컷, 섹션, 판정, 파일, 시작, 끝, 원본 경로).
- `export_clips(conti, progress=None) -> dict` → `drafts/<기획안>-cuts/NN_<원본 이름>.mp4`; 키프레임 재포장(영상+소리), 실패 시 그 구간만 PyAV H.264 재압축(`reencoded: true`), 원본 없음은 건너뜀 + 이유. 작업 kind `footage_clips`.
- 라우트: `POST /api/footage/remaining`, `POST /api/footage/csv`, `POST /api/footage/clips`(작업).
- [ ] 테스트: HTML에 외부 주소 없음·남은 컷만, CSV 열/BOM, 컷 파일 길이·재포장 경로(sample_video), 재포장 실패 시 재압축 경로(monkeypatch), 원본 삭제 후 건너뜀(Review Focus 4).
- [ ] 구현 → 테스트 → 커밋 `feat(footage): phone-ready remaining cuts, location CSV and per-cut clip files`

### Task 5: 콘티 화면의 촬영 영상 영역

stack: frontend · agent: efl:frontend-developer

**Files:** Create `scripts/ui/static/footage-area.js`, `scripts/ui/static/footage-utils.js` · Modify `scripts/ui/static/pages/conti.js`, `scripts/ui/static/app.css` · Test `tests/test_footage_utils_js.py`, `tests/test_ui_static.py`

**Interfaces:**
- Consumes: Task 3·4 라우트.
- Produces: 영역(접기, 기본 접힘 — 연결 폴더가 있으면 요약 줄만 보임): 폴더 목록(+ 경로 입력 `폴더 더하기`, `빼기`), `새 파일 처리하고 맞추기`(진행 카드: 레퍼런스 추가와 같은 원형·남은 시간, 멈추기), stt 없음 안내, 요약 `찍음 N · 확인 필요 N · 남은 컷 N` + 필터(전체/남은 컷/확인 필요), 컷 줄(번호·대사 요약·판정 배지(글자 포함)·선택 구간), 확인 필요 카드(후보마다 사진·파일·시간·받아쓴 말·`이걸로`·`재생`(인라인 `<video>`, 지문 id + `#t=start,end`)·`못 찍음으로`), 새 컷 후보 카드(`이 컷 뒤에 넣기`(컷 고르기)·`숨기기`), 결과 버튼(`폰용 남은 컷 만들기`, `CSV 받기`, `컷 파일 만들기`)과 만든 파일 링크. 콘티 편집기의 컷 목록 줄에 작은 판정 점(찍음/확인/남음)도 표시.
- `footage-utils.js`: `KIND_TEXT`, `summaryLine(state)`, `filterCuts(cuts, f)`, `fmtRange(start, end)`.
- [ ] 테스트(node·정적) → 구현 → 테스트 서버에서 실제 sample 영상 폴더로 흐름(가짜 llm 불가하면 처리까지만) 캡처 1280/380 라이트·다크 → 커밋 `feat(ui): footage area in the conti editor — folders, progress, remaining cuts, review and outputs`

### Task 6: 문서

stack: docs

**Files:** `docs/help/conti.md`(촬영 영상 절), `yt-coach-plugin-design.md`(촬영본 이미지 상한과 레퍼런스 규칙 구분, 새 모듈·저장 위치), `README.md`(CLI가 표에 있으면), `requirements.txt` 주석(faster-whisper 선택 설치가 촬영본 받아쓰기에도 쓰임).
- [ ] 고치기 → 전체 테스트 → 커밋 `docs(footage): footage matching help and design notes`
