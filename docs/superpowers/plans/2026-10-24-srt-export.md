# 자막 SRT 내보내기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티의 컷별 화면 자막(`caption`)과 대사(`line`)를 콘티 시간에 맞춘 `.srt`로 내보내 CapCut PC에서 불러올 수 있게 한다(글자 입력 수고 제거).

**Architecture:** 계산·저장은 `scripts/conti.py`의 `export_srt()`(+ CLI `conti srt`), UI는 `scripts/ui/api_conti.py`의 `POST /api/conti/srt`와 콘티 화면 버튼. 파일은 `/files/`(drafts 허용)로 내려받는다.

**Spec:** 채팅에서 승인(2026-10-24) — 아래 "승인된 범위".

### 승인된 범위
- 사용자: CapCut PC로 편집·자막 입력, 가장 오래 걸리는 일은 글자를 하나하나 치는 것. Remotion 움직이는 자막은 미룬다.
- 콘티 컷마다의 화면 자막(`caption`)을 콘티 시간에 맞춘 `.srt`로. 대사(`line`)는 별도 `.srt`로 고를 수 있게(기본은 화면 자막만 — 말은 CapCut 자동 자막으로 넣을 수 있으므로).
- UTF-8. 기획안 옆 `drafts/<기획안 이름>-captions.srt`(대사는 `-lines.srt`).
- 콘티 화면에 "CapCut용 자막(SRT) 받기" 버튼, 터미널로도.
- CapCut: 캡션 → 자막 불러오기 → 타임라인에 고칠 수 있는 자막 칸. 콘티 시간은 계획 시간이라 실제 편집과 어긋나면 CapCut에서 맞춘다(실제 컷에 다시 맞추기는 다음 단계).

## Global Constraints
- 기존 콘티 md·html·기획안은 바꾸지 않는다. SRT 파일만 새로 쓰거나(같은 이름이 있으면) 덮어쓴다 — SRT는 매번 콘티에서 다시 만드는 파생 파일이라 덮어써도 된다(파일 첫 줄에 주석을 넣을 수 없는 형식이므로 README·커맨드 문서에 "다시 받으면 덮어씀"을 적는다).
- 새 의존성 금지. 사용자 대면 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음.
- **프로세스를 이미지 이름으로 끄지 않는다**(`taskkill /IM python.exe` 등 금지 — CLAUDE.md).

## Review Focus
1. 연속된 컷에 같은 자막이 이어지면 하나의 자막 칸으로 합친다(자막이 컷마다 깜빡이지 않게).
2. 빈 자막 컷은 건너뛰되 시간은 누적한다.
3. 자막 글에 줄바꿈·`|`·`-->`·앞뒤 공백 → SRT 구조가 깨지지 않음(줄바꿈은 최대 2줄 유지, 빈 줄 제거, `-->`는 `→`로).
4. 컷 길이 합이 0이거나 `sec`가 이상한 콘티 → `ScriptError`, 파일 안 씀.
5. 타임코드 `HH:MM:SS,mmm` 반올림 누적 오차 없음(초를 ms 정수로 누적).

---

### Task 1: SRT 내보내기 (스크립트 + API + 콘티 화면)

stack: python
agent: efl:python-developer

**범위:** 위 "승인된 범위" 전부.

**Files:**
- Modify: `scripts/conti.py`(`export_srt`, CLI `srt`), `scripts/ui/api_conti.py`(`POST /api/conti/srt`), `scripts/ui/static/pages/conti.js`(버튼), `commands/yt-conti.md`(5절 "자막 SRT 내보내기"), `README.md`(콘티 설명·UI 표에 SRT 한 줄), `PLAN.md`(새 항목 체크)
- Test: `tests/test_conti.py`, `tests/test_ui_conti.py`, `tests/test_ui_static.py`

**Interfaces:**
- `conti.export_srt(conti: str | Path, track: str = "caption") -> dict` — `track ∈ {"caption","line"}`(그 밖은 `ScriptError("bad_request")`). 콘티 파일은 `drafts/*-conti.md`(`safe_draft_path(..., conti_file=True)`). 컷을 순서대로, 시작 ms = 앞 컷들 `sec`의 ms 정수 합(`round(sec*1000)`), 끝 = 시작 + 이 컷 ms. 텍스트 정리(앞뒤 공백, 빈 줄 제거, 최대 2줄 — 넘으면 앞 2줄 + 나머지는 둘째 줄 끝에 공백으로 이어 붙임, `-->`→`→`). 빈 텍스트 컷은 건너뜀. 같은 텍스트가 바로 앞 블록과 이어지면(사이에 빈 컷 없음) 끝 시간만 늘림. 출력 경로 `drafts/<기획안 stem>-captions.srt` / `-lines.srt`(콘티 stem에서 `-conti` 제거). 줄바꿈 CRLF, UTF-8(BOM 없음). 반환 `{"path": 상대경로, "track", "count": 블록 수, "duration_sec"}`. 블록이 0개면 파일을 쓰지 않고 `{"path": None, "count": 0, ...}`.
- CLI: `python -m scripts.conti srt <conti> [--track caption|line]`.
- `POST /api/conti/srt` `{"path": 콘티 경로, "track"?: "caption"|"line"}` → `export_srt` 결과 + `"url": "/files/<path>"`(path가 있을 때).
- 콘티 화면(콘티 상세): 버튼 "CapCut용 자막(SRT) 받기"와 작은 선택 "대사도 따로 받기"(대사 SRT 버튼). 누르면 API 호출 → 성공 시 `a[href=filesUrl(path)][download]`를 만들어 클릭(사용자 클릭 핸들러 안에서) + 안내 "CapCut PC: 캡션 → 자막 불러오기에서 이 파일을 고르세요. 콘티 시간 기준이라 실제 편집과 어긋나면 자막 칸을 끌어 맞추세요". count 0이면 "내보낼 자막이 없습니다". 실패는 기존 alert 방식.
- `/files/`가 `.srt`를 내려줄 때 Content-Type이 텍스트(UTF-8)인지 확인하고, 아니면 서버의 확장자 매핑에 `.srt: application/x-subrip; charset=utf-8`(또는 `text/plain; charset=utf-8`)을 더한다.

- [ ] **Step 1: 실패 테스트** — export_srt: 기본 형식(번호, 타임코드, CRLF, 빈 줄 구분); 같은 자막 연속 합치기; 빈 컷 건너뛰기+시간 누적; 0.1초 단위 누적 오차 없음(예: 0.1 × 30컷 → 끝 00:00:03,000); 텍스트 정리(줄바꿈 3줄→2줄, `-->` 치환); line 트랙 파일 이름; 블록 0개 → 파일 없음; 잘못된 track; 콘티 아닌 경로 → bad_path; 콘티 md·html 바이트 불변. API: 200 + url, 다운로드 GET 200 + 내용 일치 + content-type. 정적: conti.js 토큰 "CapCut용 자막(SRT) 받기", "/api/conti/srt", "download".
- [ ] **Step 2–4:** 구현, `pytest tests -q`, `node --check`.
- [ ] **Step 5: 커밋** — `feat(conti): export captions as srt for capcut`
