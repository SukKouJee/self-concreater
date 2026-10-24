# 움직이는 자막 영상(Remotion) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콘티 자막(계획 시간 또는 편집본 컷에 맞춘 시간)을 Remotion으로 "초록 배경 + 움직이는 자막" MP4로 렌더해, CapCut 위 트랙에 얹고 크로마키로 초록을 빼서 쓰게 한다.

**Architecture:** 플러그인 안에 작은 Remotion 프로젝트 `remotion/`(JSX, 빌드 설정 최소)를 두고, Python `scripts/captions.py`가 콘티에서 자막 목록(props JSON)을 만들어 `npx remotion render`를 하위 프로세스로 실행한다. 자막 블록은 SRT와 같은 계산(`conti._srt_blocks`, 계획/편집본 맞춤)을 공유한다. UI는 콘티 상세에 버튼 + 작업(kind `caption_video`).

**Spec:** 사용자가 질문 없이 "기본안으로 바로 진행"(2026-10-24). 아래 "기본안"이 이번 기준(내 결정 — 사용자에게 보고함). 이전 합의: CapCut PC 사용, 투명 영상은 CapCut이 못 읽으므로 초록 배경 MP4 + 크로마키, Remotion은 개인·직원 3명 이하 무료.

### 기본안 (내 결정)
- 입력: 콘티 화면 자막(`caption`) — 기본은 콘티 계획 시간, 편집본 경로를 주면 실제 컷에 맞춘 시간(기존 SRT 맞춤과 같은 계산).
- 출력: `drafts/<기획안>-captions.mp4`, H.264, 30fps, 전체 길이 1개 파일. 크기: 쇼츠(콘티 `format` shorts) 1080×1920, 롱폼 1920×1080; 편집본을 주면 그 영상의 가로·세로 비율을 따른다(짧은 변 1080 기준).
- 배경: 크로마 그린 `#00FF00` 전체.
- 모양: 화면 아래쪽(하단에서 18%) 가운데, 최대 2줄, 굵은 고딕(글꼴 순서 `"Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif` — 네트워크 글꼴 받기 없음), 흰 글자 + 검은 외곽선(굵게) + 그림자 없음(크로마키 번짐 방지), 숫자가 들어간 낱말은 노란색(#FFD400) 강조.
- 움직임: 나타날 때 0.27초(8프레임) 동안 살짝 커지며(0.85→1.0, spring) 나타나고, 사라질 때 0.13초(4프레임) 페이드아웃. 자막이 0.4초보다 짧으면 움직임 없이 바로 표시.
- 설치: Node 18+ 필요. 처음 한 번 `ytc captions setup`(= `remotion/`에서 `npm ci`)을 사용자가 실행 — 자동 설치하지 않음. Remotion이 첫 렌더 때 Chrome Headless Shell을 받는다는 점을 README에 적는다.
- 라이선스: README에 "Remotion은 개인·직원 3명 이하·비영리 무료, 그보다 큰 회사는 회사 라이선스 필요" 한 줄.
- 빼는 것: 스타일 고르기·편집 UI, 인트로·지도 애니메이션, 투명 배경 출력.

## Global Constraints (각 Task 지시에 그대로 포함)
- 사용자 영상은 읽기만(기존 규칙). 렌더 결과만 `drafts/`에 새로 쓰고(같은 이름이면 덮어씀 — 파생 파일), 콘티·기획안 md는 바꾸지 않는다. 렌더 중간 파일은 `workspace/tmp/captions_*`에 두고 끝나면(성공·실패) 지운다.
- 렌더는 하위 프로세스(`npx remotion render`), 작업 폴더가 아닌 `remotion/`을 cwd로, 시간 제한(기본 15분), 취소 시 그 프로세스만 종료(`proc.terminate()` → 대기 → `kill()`). **프로세스를 이미지 이름으로 끄지 않는다**(`taskkill /IM node.exe`/`python.exe`, `Stop-Process -Name`, `pkill` 금지 — CLAUDE.md).
- `remotion/node_modules`, `remotion/out` 등은 gitignore. `package.json`은 `remotion`·`@remotion/cli`·`react`·`react-dom` 버전 고정(정확한 버전, `^` 없음 — 4.0.538 기준, react 19 계열은 Remotion이 지원하는 버전으로), `package-lock.json` 커밋.
- 설치 안 됨(`node` 없음, `remotion/node_modules` 없음) → `ScriptError("remotion_missing", "움직이는 자막을 만들려면 처음 한 번 설치가 필요합니다: ytc captions setup (Node 18 이상)")`. 설치는 사용자 명령으로만.
- 화면 문구 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음. 병렬 작업 시 커밋은 명시한 경로만. 새 커맨드를 더하지 않고 `commands/yt-conti.md`에 절을 더한다(README 커맨드 표 변화 없음 — 스크립트 표에 `captions` 행 추가).
- 설계서(`yt-coach-plugin-design.md`)에 "자막 영상 렌더(선택 설치, Remotion)" 한 단락을 더하고 버전 표기를 올린다. CLAUDE.md "로컬 전용 UI 허용" 줄에 "자막 영상 렌더(Remotion, 사용자가 설치할 때만)도 허용"을 더한다.

## Review Focus
1. Remotion 미설치·Node 없음 → 친절한 `remotion_missing`, 작업을 만들지 않음.
2. 자막이 하나도 없는 콘티 → 렌더하지 않고 "내보낼 자막이 없습니다".
3. 렌더 실패(하위 프로세스 비정상 종료) → 마지막 출력 몇 줄을 한국어 메시지와 함께, 임시 파일 정리, 기존 mp4는 덮어쓰지 않음(임시 이름으로 렌더 후 교체).
4. 아주 긴 자막·특수문자·이모지 → 2줄로 줄바꿈, 넘치면 글자 크기 자동 축소(최소 60%), 깨지지 않음.
5. 렌더 중 화면 이동·취소 → 하위 프로세스 종료, 임시 정리.

---

### Task 1: Remotion 프로젝트

stack: frontend
agent: efl:frontend-developer

**범위:** 기본안의 모양·움직임·크기·배경 + Global Constraints.

**Files:** Create `remotion/package.json`, `remotion/package-lock.json`(npm install로 생성), `remotion/remotion.config.mjs`(필요 시), `remotion/src/index.jsx`(registerRoot), `remotion/src/Root.jsx`(Composition `Captions`: `calculateMetadata`로 props의 fps·width·height·durationInFrames 사용), `remotion/src/Captions.jsx`, `remotion/src/timing.mjs`(순수 함수), `remotion/README.md`(짧게), `remotion/sample-props.json`; Modify `.gitignore`(`remotion/node_modules/`, `remotion/out/`); Test `tests/test_remotion_timing.py`(node로 `timing.mjs` 실행 — node 없으면 skip).

**Interfaces:**
- props JSON(Task 2가 만듦): `{"fps": 30, "width": 1080, "height": 1920, "durationInFrames": int, "background": "#00FF00", "captions": [{"start": 초, "end": 초, "text": str}]}`.
- `timing.mjs`: `export function toFrames(sec, fps)`(반올림), `export function captionFrames(captions, fps)` → `[{from, duration, text}]`(겹치면 앞 자막을 다음 시작에서 자름, 길이 0 이하 제외), `export function emphasisTokens(text)` → `[{text, emphasis: bool}]`(공백 기준 낱말, 숫자 포함 낱말 강조), `export function animate(frame, duration, fps)` → `{scale, opacity}`(기본안 값; 0.4초 미만은 `{1,1}`).
- `Captions.jsx`: 배경 색 전체 + `Sequence`별 자막(하단 18%, 가운데, 최대 2줄, `-webkit-text-stroke` 대신 여러 겹 `text-shadow` 외곽선 또는 `paint-order: stroke` — 크로마 번짐 없는 단색, 노란 강조, 글자 크기는 화면 짧은 변의 7% 기본, 2줄 넘치면 줄임(최소 60%)).

- [ ] **Step 1:** `tests/test_remotion_timing.py`(node subprocess로 `timing.mjs` 함수들 검증: 반올림, 겹침 자르기, 0길이 제외, 강조 토큰, 짧은 자막 무애니메이션). RED.
- [ ] **Step 2:** 파일 작성, `npm install`(remotion 폴더에서; 정확한 버전 고정), `npx remotion compositions src/index.jsx`로 `Captions`가 보이는지 확인.
- [ ] **Step 3:** 샘플 렌더: `npx remotion render src/index.jsx Captions out/sample.mp4 --props=sample-props.json`(3초짜리) 성공 확인, 길이·해상도 확인(`ffprobe`가 없으면 OpenCV로 프레임 수·크기 확인), 결과 파일은 커밋하지 않음.
- [ ] **Step 4:** `pytest tests -q`. **Step 5: 커밋** — `feat(captions): add remotion caption composition`(package-lock 포함, node_modules 제외)

---

### Task 2: 렌더 스크립트 + API + 콘티 화면 버튼

stack: python
agent: efl:python-developer

**범위:** 기본안의 입력·출력·설치 흐름 + Global Constraints. (콘티 화면 JS 버튼도 이 Task에서 — 작은 변경)

**Files:** Modify `scripts/conti.py`(`caption_blocks(conti, track="caption", video=None, progress=None) -> dict`로 export_srt의 블록·retime 계산을 뽑아 공유; export_srt 동작 불변), Create `scripts/captions.py`, Modify `scripts/ui/api_conti.py`(라우트 2개), `scripts/ui/jobs.py`(`SINGLE_KINDS["caption_video"] = "이미 자막 영상을 만드는 중입니다"`), `scripts/ui/static/pages/conti.js`(버튼·진행·결과 링크), `commands/yt-conti.md`(6절), `README.md`(스크립트 표 `captions` 행, 설치·라이선스·CapCut 크로마키 사용법 짧게), `yt-coach-plugin-design.md`, `CLAUDE.md`, `PLAN.md`(새 항목); Test `tests/test_captions.py`, `tests/test_ui_conti.py`, `tests/test_ui_static.py`.

**Interfaces:**
- `captions.check_setup() -> dict` — `shutil.which("node")`·`npx`, `remotion/node_modules/@remotion/cli` 존재 확인; 없으면 `remotion_missing`.
- `captions.setup() -> dict` — `remotion/`에서 `npm ci`(없으면 `npm install`) 실행(사용자 명령), 결과 요약.
- `captions.build_props(conti, video=None, track="caption") -> dict` — `conti.caption_blocks` → 초 단위 `captions`, 크기(기본안 규칙; 편집본이면 cv2로 가로·세로 → 짧은 변 1080 비율), `durationInFrames = ceil(total_sec * 30)`.
- `captions.render(conti, video=None, track="caption", progress=None, cancel=None, timeout=900) -> dict` — `check_setup` → props를 `workspace/tmp/captions_<uuid>.json` → `npx remotion render src/index.jsx Captions <임시 mp4 in workspace/tmp> --props=<json>`(cwd=`remotion/`, Windows는 `npx.cmd`) → 성공 시 `drafts/<기획안 stem>-captions.mp4`로 교체(`os.replace`, 실패 시 기존 파일 유지) → 임시 정리 → `{"path","count","duration_sec","width","height","retime"?}`. 진행률은 Remotion 출력의 `Rendered N/M` 줄을 파싱해 `progress(n, m)`. 취소 콜백이 참이면 `proc.terminate()`(대기 5초 후 `kill()`). 실패 → `ScriptError("render_failed", "자막 영상을 만들지 못했습니다: <마지막 출력 5줄>")`. 자막 0개 → `ScriptError("no_captions", "내보낼 자막이 없습니다")`.
- CLI: `python -m scripts.captions setup`, `check`, `render <conti> [--video <편집본>] [--track caption|line]`.
- `GET /api/captions/check` → `{"ready": bool, "message"?}`.
- `POST /api/conti/caption-video` `{"path", "video"?, "track"?}` → 검증(콘티 경로, 영상 경로는 `conti._clean_video_path`, `check_setup`, 자막 0개)을 작업 전에 → 작업 kind `"caption_video"`(항목 id = 콘티 경로, step `"render"`, 진행률 메시지 "N/M 프레임") → 끝나면 항목 `state="done"`, `preview` = render 결과 + `url`. 작업 취소 시 render의 cancel이 참.
- `conti.js`: SRT 영역 아래 "움직이는 자막 영상" 블록: 설치 확인(`/api/captions/check`)이 false면 안내 + `ytc captions setup` 명령 복사; true면 버튼 "자막 영상 만들기(초록 배경)"(편집본 경로 입력칸이 채워져 있으면 그 시간으로) → 진행률 → 완료 시 "받기" 링크(a download) + 안내 "CapCut: 이 영상을 맨 위 트랙에 얹고 크로마키로 초록을 빼세요". 재진입: 같은 콘티의 caption_video 작업 진행 중이면 진행 표시 복원.

- [ ] **Step 1: 실패 테스트** — caption_blocks가 export_srt와 같은 블록(계획·편집본) / build_props 크기·길이·시간 / check_setup 없음 → remotion_missing / render: 하위 프로세스를 가짜로 바꿔 명령 줄(cwd, npx.cmd, props 경로), 진행률 파싱, 성공 시 교체·임시 정리, 실패 시 기존 mp4 유지·메시지, 취소 시 terminate, 자막 0개 → no_captions / API: check, 작업 사전 검증(미설치·자막 없음·잘못된 영상 → 400, 작업 없음), 작업 완료 preview.url, 중복 실행 / 정적: conti.js 토큰("/api/conti/caption-video","/api/captions/check","kind=caption_video","자막 영상 만들기","크로마키","ytc captions setup"). 실제 Remotion 렌더 통합 테스트 1개는 `remotion/node_modules`가 있을 때만 실행(없으면 skip): 3초 콘티 → mp4가 생기고 프레임 크기·수가 맞음.
- [ ] **Step 2–4:** 구현, `pytest tests -q`, `node --check`. **Step 5: 커밋** — `feat(captions): render caption videos for capcut`
