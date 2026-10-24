# 미션 화면 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로컬 UI "성장 › 미션"(지금 2차 안내)을 실제 화면으로: 오늘 미션 받기(Claude 1회) → 저장, 완료 제출(메모·난이도·선택 파일 → Claude 피드백 1회 → 확인 후 저장), 건너뛰기, 바꾸기, 최근 14일 기록.

**Architecture:** 저장·계산은 기존 `scripts/mission.py`(`context`, `save`, `submit`, `skip`). 미션 검증을 `validate_mission(data)`로 뽑아 Claude 응답 검증과 `save`가 같이 쓴다. `submit`에 `note`(무엇을 했는지)를 더한다. UI 서버 `scripts/ui/api_mission.py`, 프런트 `pages/mission.js`.

**Spec:** 채팅에서 승인(2026-10-24) — 아래 "승인된 범위". 규칙: `skills/yt-mission/SKILL.md`, 터미널: `commands/yt-mission.md`.

### 승인된 범위
- 미션은 카테고리별이 아니라 전체에 하루 하나(기존 그대로). 재료: ★카드 → 최근 기획안 → 진단 갭(의도한 차이 제외) → 이력. 최근 3개 평가·건너뛰기로 시간 자동 조정.
- 오늘의 미션 카드:
  - 없으면 "오늘 미션 받기"(Claude 1회) → 미리보기(제목·종류·시간·왜·단계·결과물; 지난 평가 반영 시 "지난 평가 반영해 5분 줄였어요" 식 안내) → "이걸로 할게요"(저장) / "다른 걸로"(다시 뽑기).
  - 있으면 내용·상태(진행 중/완료/건너뜀) + "완료 제출"(무엇을 했는지 한두 줄, 난이도 쉬움/적당/어려움, 선택: 사진·스크린샷·텍스트 파일 경로 → "피드백 받기" → Claude 짧은 피드백(잘된 점 1 → 다음에 바꿀 점 1) → 확인 후 저장), "건너뛰기"(사유: 시간 부족/재료 없음/하기 싫음/기타), "바꾸기"(다른 재료로 다시 뽑아 교체, Claude 1회).
  - 프로필이 없으면 "프로필을 만들면 미션이 맞춤으로 나옵니다" 안내.
- 최근 기록: 지난 14일 날짜별 완료/건너뜀·난이도·제목, 이번 주 완료율.
- 제출 파일은 이미지·텍스트만, Claude가 읽도록 작업 폴더 임시 위치에 잠깐 복사했다가 피드백 직후 지운다(원본은 건드리지 않음). 영상은 적어 준 설명으로 판단(내 결정).
- 빼는 것: 주간 회고·프로필 단계 제안(리포트 화면에서).

## Global Constraints (각 Task 지시에 그대로 포함)
- Claude는 "오늘 미션 받기"·"다른 걸로"·"바꾸기"·"피드백 받기" 클릭 때만. 미션 저장은 "이걸로 할게요", 제출 저장은 피드백 확인 후 "저장", 건너뛰기는 사유 선택 후 확인 버튼 — 모두 사용자 클릭.
- 사용자 원본 파일은 읽기·복사만(수정·삭제·이동 금지). 임시 복사본은 `workspace/tmp/mission_submit_*`에, 피드백 작업이 끝나면(성공·실패 모두) finally로 삭제. 허용 확장자: 이미지 `.png .jpg .jpeg .webp .gif`, 텍스트 `.txt .md`; 크기 ≤ 10MB; 아니면 `bad_file`.
- 기록 파일 `missions/<날짜>.md` 쓰기는 `refstore.write_atomic`(기존 `save`/`submit`의 직접 쓰기도 바꾼다). 동시 요청은 모듈 lock으로 직렬화.
- 날짜는 서버 로컬 날짜(`mission._today`). 테스트는 날짜 인자로 고정.
- 새 의존성 금지. 프런트: innerHTML 금지, `h()`/`fill()`, 정적 파일 `http(s)://` 금지, 네이티브 dialog 금지(인라인 확인), 페이지 계약·`alive`/`isCurrent()`·`ctx.api`, 작업 재진입·handled·`job_id`·aborted 처리는 `pages/motif.js` 방식. md 본문 표시는 `static/md.js` `renderMarkdown`.
- 사용자 대면 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음. 병렬 작업 시 커밋은 명시한 경로만.
- **프로세스를 이미지 이름으로 끄지 않는다**(`taskkill /IM python.exe`/`node.exe`, `Stop-Process -Name`, `pkill` 금지 — CLAUDE.md). 직접 띄운 PID만.

## Review Focus
1. 미리보기를 띄운 사이 다른 탭·터미널이 오늘 미션을 저장 → "이걸로 할게요"가 `mission_exists`로 거부되고 화면은 저장된 미션을 다시 불러오라고 안내(덮어쓰지 않음; 바꾸기는 명시적 교체만).
2. 자정을 넘겨 화면을 열어 둠 → 오늘 날짜 기준으로 다시 불러오면 새 날의 "미션 없음"(이전 날 미션에 제출하지 않음: 제출·건너뛰기 요청에 화면이 본 날짜를 보내고 서버는 그 날짜 미션에 적용).
3. 제출 파일 경로가 없음/폴더/허용 밖 확장자/10MB 초과 → `bad_file`, 임시 파일 안 남음.
4. 건너뛴 미션·완료된 미션에 다시 제출·건너뛰기 → 상태 규칙(완료된 미션은 다시 제출 가능 — 덮어쓰기 전 인라인 확인, 건너뛴 미션도 나중에 제출 가능).
5. 14일 기록이 비어 있음 / 프로필 없음 → 안내가 깨지지 않음.

---

### Task 1: 미션 API + 검증 분리 + note

stack: python
agent: efl:python-developer

**범위:** 승인된 범위의 서버 부분 + Global Constraints 전부.

**Files:** Modify `scripts/mission.py`(`validate_mission`, `submit(..., note=None)`, 쓰기 원자화), `scripts/ui/jobs.py`(`SINGLE_KINDS["mission"] = "이미 미션을 만드는 중입니다"`, `SINGLE_KINDS["mission_feedback"] = "이미 피드백을 받는 중입니다"`); Create `scripts/ui/api_mission.py`; Modify `scripts/ui/server.py`(`ROUTE_MODULES`), `commands/yt-mission.md`(submit에 `--note` 한 줄); Test `tests/test_mission.py`, `tests/test_ui_mission.py`.

**Interfaces (Task 2가 씀):**
- `mission.validate_mission(data) -> dict` — 기존 `save` 검증(같은 코드 `invalid_mission`·메시지) + 모양 오류(dict 아님, steps list 아님) → `invalid_mission`; 한 줄 필드 공백 정리; 정리된 mission 반환. `save`가 이것을 쓴다.
- `mission.submit(file, difficulty, feedback, date=None, note=None)` — `## 제출` 섹션: `- 한 일: {note}`(있을 때), `- 파일: {파일 이름만 또는 file}`(file이 빈 문자열이면 줄 생략), `- 난이도: …`. CLI `submit`에 `--note` 추가(기존 인자 호환).
- `GET /api/mission?date=` (date 생략 시 오늘) → `{"date","today": {record + "body": [{heading,text}]}|None,"recommended","stage","has_profile","history": 지난 14일 [{date,title,kind,status,difficulty,skip_reason,minutes}] (오래된→최근, 미션 없는 날도 {date, status: None}),"week": {"done","skipped","total","rate"}}` — 이번 주(월~일, 오늘까지) 완료율 = submitted / (미션 있는 날).
- `POST /api/mission/draft` `{"date"?, "replace"?: bool}` → `{"job"}`: `find_claude()` 먼저; replace=false인데 오늘 미션이 있으면 `mission_exists`(작업 전). 작업 kind `"mission"`, 항목 1개(id=date), step `"context"` → `"claude"` → `awaiting/preview/ask="save"`, preview `{"mission": 검증된 mission, "recommended", "date", "replace"}`, 작업 `done`. Claude: `ask_validated([SKILLS_DIR/"yt-mission"/"SKILL.md"], payload=mission.context(date) (+ replace면 "swap_from": 오늘 미션 제목·재료 — 다른 재료·종류로), task=MISSION_TASK, validate=mission.validate_mission, schema=MISSION_SCHEMA)`; `MISSION_TASK = "yt-mission 규칙 1~4절대로 입력에 맞는 오늘의 미션 JSON 하나를 만드세요."`(swap이면 "오늘 미션과 다른 재료·종류로" 덧붙임).
- `POST /api/mission/save` `{"date","mission","replace"?: bool,"job_id"?}` → mkstemp(`workspace/tmp/mission_*.json`, finally 삭제) → `mission.save(path, date, replace)` → 결과 + 새 `today`. job 항목 awaiting이면 `saved`.
- `POST /api/mission/feedback` `{"date","note","difficulty","file"?}` → `{"job"}`: 오늘 미션 없으면 `no_mission`, difficulty 검증, file 있으면 Global Constraints 규칙으로 검증(작업 전 `bad_file`). 작업 kind `"mission_feedback"`, 항목 id=date, step `"copy"`(file 있을 때) → `"claude"` → awaiting preview `{"feedback": str, "note","difficulty","file_name"}`. 임시 복사본은 작업 안 finally로 삭제. Claude: `ask([SKILL], payload={"mission": 오늘 미션 레코드+본문, "note", "difficulty", "file": 임시 경로 상대(있으면)}, task=FEEDBACK_TASK, schema={feedback: str}, tools=["Read"] if file else None)`; `FEEDBACK_TASK = "yt-mission 규칙 5절대로 제출물에 2~4문장 피드백(잘된 점 1개 → 다음에 바꿀 점 1개)을 쓰세요. 파일이 있으면 Read로 보세요."`; 빈 피드백은 1회 재시도(`ask_validated`).
- `POST /api/mission/submit` `{"date","note","difficulty","feedback","file_name"?,"job_id"?}` → `mission.submit(file_name or "", difficulty, feedback, date, note)` → 새 `today`. 완료된 미션 재제출 허용.
- `POST /api/mission/skip` `{"date","reason"}` → `mission.skip`; reason은 비어 있지 않은 1~100자.
- 날짜 인자는 `YYYY-MM-DD` 검증(`bad_request`).

- [ ] **Step 1: 실패 테스트** — validate_mission 특성(기존 save 메시지) + 모양 오류; submit note/파일 줄; 쓰기 원자화(파일 교체 방식); GET(오늘 없음/있음, 14일 채움, 주 완료율, 프로필 없음); draft 작업(가짜 `claude_cli.ask`: 성공, 실패 2회 → failed, mission_exists 사전 거부, replace); save(job saved, mission_exists, tmp 정리); feedback(파일 없음/폴더/확장자/10MB 초과 → bad_file·임시 없음, 이미지 파일 → 임시 복사 후 삭제·원본 바이트 불변, Claude tools Read 전달); submit(재제출, note 기록); skip; 날짜 검증; 중복 실행 job_running.
- [ ] **Step 2–4.** **Step 5: 커밋** — `feat(ui): add mission api`

---

### Task 2: 미션 화면

stack: frontend
agent: efl:frontend-developer

**범위:** 승인된 범위의 화면 부분 + Global Constraints 전부.

**Files:** Create `scripts/ui/static/pages/mission.js`; Modify `scripts/ui/static/app.js`(메뉴 "미션" → `route: "#/mission"`, `soon` 제거, `parseHash` `case "mission"`), `scripts/ui/static/pages/home.js`(오늘 미션 카드에 "미션 화면 열기" 링크 `#/mission`), `scripts/ui/static/app.css`, `tests/test_ui_static.py`, `README.md`(UI 메뉴 표), `PLAN.md`(Phase 14 미션 체크).

동작:
- 상단: h1 "미션", 날짜(화면이 불러온 날짜; 날짜가 바뀌었으면 "날짜가 바뀌었습니다 — 새로 불러오기" 버튼), 성장 단계, 프로필 없으면 안내(`/yt-coach:yt-init` 명령 복사).
- 오늘 카드(없을 때): "오늘 미션 받기"(Claude 1회 안내) → 진행 → 미리보기 카드(제목·종류 배지·시간·왜·단계 ol·결과물·재료, `recommended.adjust` easier/harder면 "지난 평가 반영해 5분 줄였어요/늘렸어요") + "이걸로 할게요" / "다른 걸로"(인라인 확인 없이 다시 뽑기) / "닫기". `mission_exists` → "다른 곳에서 오늘 미션이 저장됐습니다 — 불러오기".
- 오늘 카드(있을 때): 본문(`renderMarkdown`, 첫 제목 중복은 dropTitle), 상태 배지(진행 중/완료/건너뜀 + 난이도·사유), 버튼 "완료 제출" / "건너뛰기" / "바꾸기".
  - 완료 제출 폼: "한 일"(textarea, 필수, ≤300자), 난이도 라디오(쉬움 easy/적당 right/어려움 hard, 필수, fieldset/legend), 파일 경로(선택, 라벨 "사진·스크린샷·텍스트 파일 경로(선택)", 안내 "원본은 건드리지 않고 잠깐 복사해 읽은 뒤 지웁니다"), "피드백 받기"(Claude 1회) → 진행 → 피드백 textarea(편집 가능) + "저장"(→ submit) / "다시 받기". 이미 완료된 미션이면 "다시 제출하면 이전 제출을 덮어씁니다" 인라인 확인.
  - 건너뛰기: 사유 라디오 4개(+ 기타 텍스트) → "건너뛰기 확인".
  - 바꾸기: 인라인 확인("오늘 미션을 다른 재료로 바꿉니다") → draft(replace) 흐름 → "이걸로 바꿀게요"(save replace).
- 최근 기록: 14칸(날짜·상태 아이콘+글자: 완료/건너뜀/없음·난이도), 이번 주 완료율 "이번 주 3/5 완료(60%)". 표는 `table`(th scope) 또는 목록, 모바일에서 넘치지 않게.
- 작업 재진입: `kind=mission`(item.id == 오늘 날짜), `kind=mission_feedback` 각각 복원, handled 처리.

- [ ] **Step 1: 정적 테스트** — 메뉴 라이브 라우트(`route: "#/mission"`, `soon: "mission"` 없음, `case "mission"`), home.js `"#/mission"`; 토큰("/api/mission","/api/mission/draft","/api/mission/save","/api/mission/feedback","/api/mission/submit","/api/mission/skip","kind=mission","kind=mission_feedback","job_id","오늘 미션 받기","이걸로 할게요","완료 제출","피드백 받기","건너뛰기","바꾸기","renderMarkdown","fieldset","legend"); 금지 토큰 없음; 서빙 200. COMMANDS 목록 조정 시 이유 기록.
- [ ] **Step 2–4:** 구현, `pytest tests -q`, `node --check`. **Step 5:** 문서. **Step 6: 커밋** — `feat(ui): add mission screen`
