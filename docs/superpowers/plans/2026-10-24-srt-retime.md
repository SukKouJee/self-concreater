# SRT를 실제 컷에 맞추기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CapCut에서 컷 편집을 마치고 자막 없이 내보낸 영상의 실제 컷 경계를 찾아, 콘티 자막 SRT의 시간을 그 경계에 맞춘다.

**Architecture:** 컷 경계 찾기는 `scripts/cuts.py`에 영상 파일만 읽는 가벼운 `detect_cuts(path)`(PySceneDetect, 프레임·카메라 분석 없음)를 더한다. 시간 맞추기는 `scripts/conti.py`의 `retime_boundaries()`(순수 함수)와 `export_srt(..., video=...)`. UI는 콘티 상세의 SRT 영역에 "편집한 영상 경로" 입력 + 작업(kind `srtfit`)으로 실행.

**Spec:** 채팅에서 승인(2026-10-24) — 아래 "승인된 범위".

### 승인된 범위
- 흐름: CapCut에서 컷 편집을 마친 영상을 자막 없이 한 번 내보낸다 → 그 파일의 컷 경계를 찾는다 → 콘티 계획 시간을 실제 길이에 맞게 늘이거나 줄인 뒤, 각 자막 시작·끝을 가장 가까운 실제 컷 경계에 붙인다(컷 수가 조금 달라도 대체로 맞게; 콘티 순서대로 편집했다는 전제) → 맞춘 SRT를 CapCut에 불러와 자막을 얹는다.
- 파일은 화면에 영상 경로를 붙여 넣어 지정한다. 영상을 복사·업로드하지 않고 그 자리에서 읽기만 하며, 사용자 영상은 지우거나 바꾸지 않는다.

## Global Constraints
- 사용자 영상 파일은 읽기만(삭제·이동·수정·복사 금지). 프레임 이미지도 쓰지 않는다. CLAUDE.md의 "받은 영상 삭제" 규칙은 다운로드한 영상에 대한 것이며 여기 해당하지 않는다(사용자 파일이므로 절대 지우지 않는다).
- 경로 검증: 문자열, 존재하는 파일, 확장자 `.mp4 .mov .m4v .mkv .webm .avi`(대소문자 무시), 크기 > 0. 아니면 `ScriptError("bad_video", 한국어 메시지)`. 경로는 작업 폴더 밖이어도 된다(사용자 PC의 로컬 파일 — 로컬 전용 UI·토큰 보호 전제).
- 출력은 기존과 같은 `drafts/<기획안>-captions.srt`(대사 `-lines.srt`)를 덮어쓴다(파생 파일). 콘티 md·html·기획안 불변.
- 시간 맞추기 기준값은 `templates/thresholds.json`의 `srt`: `{"snap_min_sec": 0.4, "snap_ratio": 0.35, "scale_warn": [0.5, 2.0]}`.
- 새 의존성 금지(PySceneDetect·OpenCV는 이미 있음). 사용자 대면 한국어, 식별자 영문. 커밋 Conventional Commit, Claude 서명 없음.
- **프로세스를 이미지 이름으로 끄지 않는다**(`taskkill /IM python.exe` 등 금지 — CLAUDE.md). 테스트로 띄운 프로세스는 그 PID만.

## Review Focus
1. 실제 컷 수가 콘티보다 많거나(한 컷을 둘로 자름) 적을 때(두 컷을 합침) → 경계가 단조 증가하고, 대응 못 한 경계는 비례 시간 그대로.
2. 컷이 하나도 안 잡히는 영상(한 장면) → 비례 늘이기만, 결과 요약에 "실제 컷을 찾지 못해 길이 비율로만 맞춤".
3. 실제 길이가 콘티의 절반 미만·두 배 초과 → 결과에 경고("다른 영상이 아닌지 확인").
4. 경로에 따옴표가 붙어 붙여 넣어짐(`"C:\...\a.mp4"`) → 앞뒤 따옴표·공백 제거 후 처리.
5. 같은 실제 컷을 두 경계가 차지하지 않음(각 컷은 한 번만).

---

### Task 1: 컷 찾기 + 시간 맞추기 + CLI + API

stack: python
agent: efl:python-developer

**범위:** 위 승인된 범위의 서버·스크립트 부분.

**Files:** Modify `scripts/cuts.py`, `scripts/conti.py`, `scripts/ui/api_conti.py`, `scripts/ui/jobs.py`(`SINGLE_KINDS["srtfit"] = "이미 자막 시간을 맞추는 중입니다"`), `templates/thresholds.json`, `commands/yt-conti.md`(5절에 `--video` 흐름), `README.md`(스크립트 표·UI 표 한 줄). Test `tests/test_cuts.py`(있으면), `tests/test_conti.py`, `tests/test_ui_conti.py`.

**Interfaces:**
- `cuts.detect_cuts(path: Path) -> dict` → `{"duration_sec": float, "fps": float, "cuts": [내부 컷 시각(초, 오름차순, 0과 끝 제외)]}` — `scenedetect.detect` + `ContentDetector(threshold=SCENE_THRESHOLD, min_scene_len=MIN_SCENE_FRAMES)`, 길이는 `cv2` 프레임 수/fps. 실패(열 수 없음 등) → `ScriptError("bad_video", ...)`.
- `conti.retime_boundaries(planned: list[float], actual_duration: float, cuts: list[float], snap_min: float, snap_ratio: float) -> dict` — `planned`는 0으로 시작해 계획 총길이로 끝나는 경계 목록(컷 수+1). 알고리즘(고정): `s = actual_duration / planned[-1]`; 내부 경계 i마다 `t = planned[i] * s`, 허용 거리 `tol = max(snap_min, snap_ratio * min(앞 컷 길이, 뒤 컷 길이) * s)`; 아직 안 쓴 실제 컷 중 `prev_mapped < c` 이고 `|c - t| <= tol`인 가장 가까운 것을 고름(동점이면 앞쪽), 없으면 `t`(단 `prev_mapped`보다 커야 하므로 `max(t, prev_mapped + 0.01)`); 끝 경계 = `actual_duration`. 반환 `{"boundaries": [...], "scale": s, "snapped": 맞춘 내부 경계 수, "inner": 내부 경계 수, "detected": len(cuts)}`.
- `conti.export_srt(conti, track="caption", video: str | Path | None = None) -> dict` — `video`가 있으면 `_clean_video_path`(앞뒤 공백·따옴표 제거, 검증) → `detect_cuts` → `retime_boundaries`로 컷별 시작·끝(ms 정수, 경계 반올림)을 바꿔 기존 블록 생성(빈 컷 건너뛰기·연속 같은 자막 합치기) 그대로. 결과에 `"retime": {"video": 파일 이름만, "detected", "snapped", "inner", "scale", "warnings": [...]}` 추가. warnings: 컷 0개("실제 컷을 찾지 못해 길이 비율로만 맞췄습니다"), scale 범위 밖("콘티 길이와 많이 다릅니다(실제 X초 / 계획 Y초) — 다른 영상이 아닌지 확인하세요").
- CLI: `python -m scripts.conti srt <conti> [--track caption|line] [--video <path>]`.
- `POST /api/conti/srt` 기존 그대로(빠름). 새 `POST /api/conti/srt-fit` `{"path", "track"?, "video"}` → 경로 검증(콘티 경로·영상 경로)을 작업 전에 하고 작업 kind `"srtfit"` 시작(항목 1개, id=콘티 경로, step `"detect"` → `"write"`), 끝나면 항목 `state="done"`(확인이 필요 없는 결과라 awaiting 아님) + 항목 `preview`에 export 결과(+`url`), 작업 `done`. 실패는 작업 failed + 메시지.

- [ ] **Step 1: 실패 테스트** — `retime_boundaries`: 컷 수 같음(전부 snap), 많음(여분 무시), 적음(일부 비례), 컷 없음, 단조성, 같은 컷 중복 사용 없음, tol 경계값. `detect_cuts`: 짧은 합성 영상(OpenCV로 색이 바뀌는 3구간 mp4를 tmp에 생성)에서 컷 2개 근처 검출, 읽을 수 없는 파일 → bad_video. `export_srt(video=…)`: 합성 영상으로 블록 시간이 실제 컷에 맞음, warnings, 영상 파일 바이트·mtime 불변, 따옴표 붙은 경로. API: srt-fit 작업 → done + preview.url, 잘못된 영상 경로 → 400 bad_video(작업 없음), 중복 실행 job_running.
- [ ] **Step 2–4.** **Step 5: 커밋** — `feat(conti): fit srt timing to cuts of the edited video`

---

### Task 2: 콘티 화면 입력

stack: frontend
agent: efl:frontend-developer

**범위:** 콘티 상세의 SRT 영역에 "편집한 영상으로 시간 맞추기": 안내 한 줄("CapCut에서 컷 편집을 마친 영상을 자막 없이 내보낸 뒤, 그 파일 경로를 붙여 넣으세요 — 파일은 읽기만 합니다"), 경로 `input`(라벨 "편집한 영상 파일 경로", placeholder `예: C:\Users\…\Videos\편집본.mp4`), 버튼 "실제 컷에 맞춰 SRT 받기"(화면 자막) / "대사도 맞춰 받기". 실행 중 진행 표시("컷을 찾는 중…"), 완료 시 기존 다운로드 방식으로 파일을 내려받고 요약("실제 컷 N개 · 자막 경계 k/m개를 컷에 맞춤 · 길이 비율 x.xx") + warnings 표시. 재진입 시 같은 콘티의 srtfit 작업이 진행 중이면 진행 표시를 되살림(결과 다운로드는 자동으로 하지 않고 "받기" 링크로). 경로 입력은 이 화면에서만 기억(localStorage 금지 — 개인 경로 저장 안 함).

**Files:** Modify `scripts/ui/static/pages/conti.js`, `scripts/ui/static/app.css`, `tests/test_ui_static.py`.

- [ ] 정적 테스트 토큰: "/api/conti/srt-fit", "kind=srtfit", "편집한 영상 파일 경로", "실제 컷에 맞춰 SRT 받기"; 금지 토큰(innerHTML, confirm(, alert(, prompt(, localStorage) 없음.
- [ ] 구현, `pytest tests -q`, `node --check`. **커밋** — `feat(ui): fit srt to the edited video from the conti screen`
