# yt-coach 개발 계획서

설계 기준: `yt-coach-plugin-design.md` v2
작업 방식: 단계(Phase)별로 진행하고, 각 단계의 **완료 조건**을 실제 링크로 확인한 뒤 다음 단계로 간다.
테스트용 링크: 같은 카테고리의 쇼츠 10~12개 + 롱폼 3개를 미리 준비한다 (`tests/fixtures/links.txt`).

---

## Phase 0. 뼈대 (0.5일)

작업
- [x] `.claude-plugin/plugin.json` (name: yt-coach, version 0.1.0)
- [x] `commands/` 8개 파일: 프런트매터(description, argument-hint)만, 본문은 "미구현" 안내
- [x] `templates/`: video.md, index.md, techniques.md, profile.md, mission.md, thresholds.json
- [x] `scripts/` 패키지 구조, `requirements.txt`, `scripts/common.py`(경로·JSON 출력·에러 규약)
- [x] `README.md` 커맨드 표, 설치 방법 (로컬 마켓플레이스 등록)
- [x] `tests/` pytest 설정

완료 조건
- 로컬에 플러그인 설치 후 8개 커맨드가 목록에 보인다
- `python -m scripts.common --selftest` 가 `{"ok": true}` 출력

---

## Phase 1. 초기화와 레퍼런스 저장소 (1일)

작업
- [x] `refstore.py`: md 프런트매터 읽기/쓰기(본문 보존), 카테고리 목록, ref 수 집계, `index.md` 단계 섹션 갱신
- [x] `/yt-init`: 폴더·템플릿 생성, git init 제안, 인터뷰 → `profile.md` → 첫 카테고리 생성
- [x] 단위 테스트: 프런트매터 수정 시 본문·사용자 메모가 손실되지 않음

완료 조건
- 빈 폴더에서 `/yt-init` 실행 → `profile.md`, `refs/ref_<cat>/index.md`, `techniques.md` 생성
- `refstore.py count <cat>` 이 `{"ref": 0, "mine": 0, "stage": 1}` 출력

---

## Phase 2. 수집 (1~1.5일)

작업
- [x] `fetch.py <url>`: video id 정규화(watch/shorts/youtu.be), yt-dlp 메타데이터, youtube-transcript-api 자막(원어 → ko → en, 각각 수동 → 자동 순), `workspace/raw/{id}.json`
- [x] 말이 거의 없는 영상 표시(`sparse`), 요청 간격(`YT_FETCH_INTERVAL`)
- [x] 실패 유형 구분: `no_transcript`, `blocked`, `private`, `invalid_url`
- [x] `YT_PROXY` 지원
- [x] `stt.py` (선택 의존성, 없으면 친절한 에러)
- [x] 중복 검사: 이미 같은 카테고리에 있음 / ref·mine 충돌

완료 조건
- 테스트 링크 15개 중 자막 있는 영상은 모두 raw JSON 생성
- 차단 시 `blocked` 에러와 프록시 안내가 나온다
- **이 단계에서 차단 여부를 확인하고 결과를 README에 기록**

---

## Phase 3. 컷 통계 (1일)

작업
- [x] `cuts.py <id>`: yt-dlp 360p 이하 다운로드 → PySceneDetect(ContentDetector) → 컷 타임라인
- [x] 장면당 대표 프레임 1장, 최대 40장 → `workspace/frames/{id}/`
- [x] optical flow로 장면별 고정/패닝/줌 비율 (참고값)
- [x] 원본 영상 삭제 보장 (try/finally)
- [x] 출력: `{cuts: [...], avg_cut_sec, camera: {...}, frames: [...]}`

완료 조건
- 쇼츠 3개, 롱폼 1개에서 컷 통계 생성, 처리 후 영상 파일이 남아 있지 않다
- 쇼츠 평균 컷 길이가 육안 확인과 크게 다르지 않다

---

## Phase 4. 단일 분석 — `/yt-add` 완성 (1.5일)

작업
- [x] `skills/yt-analyze/SKILL.md`: 섹션 라벨 정의·판단 예시, 훅·서사 유형 정의, 출력 포맷(video.md 템플릿 그대로), 같은 카테고리의 사용자 수정본 2~3개를 few-shot으로 읽는 규칙
- [x] 섹션별 컷 통계 매핑(pacing.by_section)
- [x] `/yt-add` 흐름: 링크 파싱 → 영상별 fetch → cuts → 분석 → 확인 → md 저장 → techniques.md 갱신 → 단계 안내
- [x] 이미 md 있으면 건너뜀, `--force` 지원

완료 조건
- 링크 3개 입력 → `videos/*.md` 3개, 프런트매터가 스키마대로 채워짐
- 사용자가 한 영상의 섹션을 고친 뒤 새 영상을 넣으면, 새 분석이 그 교정 경향을 따른다(수동 확인)
- 같은 링크를 다시 넣으면 재분석하지 않는다

---

## Phase 5. 유형화 — `/yt-typify` (2일)

작업
- [x] `features.py`: 프런트매터 → 축별 벡터 (structure: 라벨 시퀀스 + 비율, hook/narrative: 범주형, title: 패턴 원핫, pacing: 컷 수치)
- [x] `cluster.py`: **ref만 로드**, 축별 군집(HDBSCAN 또는 계층 군집), 실루엣·점유율 계산, thresholds 적용, 이상치 목록
- [x] 단계 분기: <5 중단, 5~9 예비 관찰, 10+ 정식 판정
- [x] `skills/yt-typify/SKILL.md`: 스크립트 결과 해석, 판정 근거 문장, 아키타입 카드(골격·평균 비율·대표 영상·촬영 경향), 이름 제안
- [x] `index.md` 판정·아키타입·이상치 섹션 갱신
- [x] 테스트: mine 영상이 섞여 있어도 결과가 동일함

완료 조건
- 테스트 쇼츠 10~12개로 축별 판정이 나오고, 근거가 납득 가능하다
- 섹션 라벨을 md에서 고친 뒤 재실행하면 결과에 반영된다

---

## Phase 6. 모티브 — `/yt-inspire` (1.5일)

작업
- [x] `skills/yt-inspire/SKILL.md`: 카드 4종(흐름·기법·각도·조합) 정의, 근거 영상 링크 필수, profile 관심 주제 반영, 이미 나온 카드와 중복 방지
- [x] 단계별 동작: 0~4개 기법 카드만, 5~9개 기법·각도, 10+ 4종 모두
- [x] 카드 ID(`M-<cat>-001`) 부여, `index.md` 모티브 섹션 누적, ★ 선택 처리
- [x] techniques.md 정리 규칙(중복 기법 병합)

완료 조건
- 레퍼런스 3개 상태에서 기법 카드가 나온다
- 10개 이상 상태에서 4종 카드가 나오고, 각 카드가 실제 영상을 근거로 든다

---

## Phase 7. 기획 — `/yt-draft` (1일)

작업
- [x] `skills/yt-draft/SKILL.md`: 모티브 카드 또는 아키타입을 입력으로 섹션별 대본 + 샷 리스트(샷 크기·앵글·길이·컷 수), pacing 축의 컷 경향 반영, profile 피하고 싶은 스타일 반영
- [x] `drafts/{date}-{slug}.md` 저장 (출처 카드 ID 기록)

완료 조건
- ★카드 하나로 쇼츠 대본 + 샷 리스트가 나오고, 섹션 비율이 아키타입과 대체로 맞는다

---

## Phase 8. 진단 — `/yt-diagnose` (1일)

작업
- [x] mine 입력 3종: 링크(전체 파이프라인), draft 파일, 대본 텍스트(촬영·pacing 축 제외)
- [x] `similarity.py`: 축별 근접도, 가장 가까운 아키타입, 수치 갭
- [x] `skills/yt-diagnose/SKILL.md`: 갭 서술, intentional 처리, 다음 행동 제안 1~3개
- [x] `mine/{id}.md` 저장, 아키타입 불변 테스트
- [x] ref 10개 미만이면 "예비 진단"으로 표시 (설계서 4절)

완료 조건
- Phase 7의 draft를 진단하면 높은 근접도, 일부러 구조를 바꾼 대본은 낮은 근접도가 나온다
- intentional 표시한 갭이 재진단에서 빠진다

---

## Phase 9. 미션·회고 (1.5일)

작업
- [x] `skills/yt-mission/SKILL.md`: 성장 단계 판단, 재료 우선순위(★카드 → draft → 갭), 15~30분 크기, 촬영 미션 포함, 난이도 적응 규칙
- [x] `/yt-mission today|submit|skip|swap`, `missions/{date}.md` 기록
- [x] `/yt-review`: 주간 집계 → 제안 → 승인 시 profile 갱신
- [x] (선택) cron 예시: `claude -p "/yt-mission today"` 를 README에

완료 조건
- 3일 연속 미션 생성·제출 시뮬레이션에서 난이도 평가가 다음 미션에 반영된다
- 주간 회고 승인 후 profile.md 성장 단계·약점이 갱신된다

---

## Phase 10. Vision 샷 분류 (1.5일, 후반)

작업
- [x] `skills/yt-shots/SKILL.md`: 샷 크기·앵글·얼굴출연·손만·B롤·텍스트오버레이 분류 기준과 예시
- [x] Claude가 `workspace/frames/{id}/`를 Read로 보고 장면별 분류 → 섹션 매핑 → `shots` 프런트매터
- [x] 기존 영상 일괄 보강 옵션(`/yt-add --shots-only <cat>`)
- [x] features·cluster에 shots 축 추가, draft 샷 리스트에 반영

완료 조건
- 쇼츠 5개에서 샷 분류가 육안과 대체로 일치하고, typify에 shots 축 판정이 나온다

---

## Phase 11. 리포트 (1일, 선택)

- [x] `report.py <cat>`: 축별 판정, 아키타입 비교, pacing 분포, mine 근접도 추이를 단일 HTML로
- [x] `/yt-review`에서 링크 제공

---

## Phase 12. 콘티 — `/yt-conti` (반나절, 추가 요건)

- [x] 설계서 v2.1 반영 (커맨드·스키마·저장 위치)
- [x] `shots.py apply`가 프레임별 라벨을 `workspace/shots/{id}.json`에 남김
- [x] `conti.py context`: draft md(프런트매터 섹션 + 본문 샷 표) 파싱, 레퍼런스 말 속도 중앙값, 포맷
- [x] `conti.py save`: 검증(섹션별 컷 길이 합, 샷 크기 어휘), 경고(대사 길이 초과, 전체 대사와 컷 대사 불일치), `-conti.md` 저장(내 메모 보존)
- [x] HTML 스토리보드: 컷 카드(도식 SVG + 참고 프레임 base64 + 대사·자막·효과음·가이드), 전체 대사, 표 보기, 라이트/다크, 인쇄
- [x] 참고 프레임 매칭: 프레임별 라벨(샷 크기+섹션) → 섹션별 대표 샷 크기 근사, 같은 프레임 반복 최소화
- [x] `conti.py render <conti.md>`: 고친 프런트매터로 HTML 다시 그림
- [x] `skills/yt-conti/SKILL.md`, `commands/yt-conti.md`, README 표
- [x] `conti.py srt <conti> [--track caption|line]`: 컷별 자막(또는 대사)을 CapCut용 `.srt`로 내보내기(`drafts/<기획안>-captions.srt`·`-lines.srt`), 콘티 화면 버튼

완료 조건
- 실제 draft 1개로 콘티를 만들고, HTML에서 컷마다 도식·참고 프레임·대사·가이드가 보이며 대사 길이 경고가 맞게 나온다
- 확인(2026-10-20): 컵라면 4단계 draft(36컷) → 전체 대사·컷 가이드 생성, 경고 0. 처음엔 섹션 대표 샷 근사로 참고 프레임이
  틀렸음(인서트에 도로 장면) → 프레임별 라벨 저장 + `--shots-only` 재분류(12개) 후 36컷 모두 exact 매칭, 육안 일치

---

## Phase 13. 로컬 UI 1차 (추가 요건)

설계: `docs/superpowers/specs/2026-10-23-local-ui-design.md`, 계획: `docs/superpowers/plans/2026-10-23-local-ui.md`

- [x] Task 1: `status` 스크립트 (홈 데이터)
- [x] Task 2: 콘티 컷 편집 · 참고 프레임 고정 · 장면 후보 (`conti update`·`scenes`, schema_version 2)
- [x] Task 3: Claude CLI 호출기 + 컷 대사 대안 규칙 (`claude_cli`, `cut-context`, SKILL 7절)
- [x] Task 4: UI 서버 뼈대 · 보안 · 실행기 (`ytc ui`, `bin/ytc.cmd`)
- [x] Task 5: 레퍼런스 추가 작업(job) + API
- [x] Task 6: 콘티 API
- [x] Task 7: 화면 뼈대 · 왼쪽 메뉴 · 홈
- [x] Task 8: 레퍼런스 추가 화면
- [x] Task 9: 콘티 화면
- [x] Task 10: `/yt-ui` 커맨드 · 문서
- [x] Task 11: 실제 링크로 완료 확인

확인(2026-10-24): 작업 폴더 복사본에서 `ytc ui --detach` → 두 번째 실행은 같은 주소(`running`). 롱폼 2개(GzAECmhO9b8, 9hNqOiUY4DY)를 API로 추가 → 실제 Claude 분석 확인 카드 표시(스크린샷), 라벨 하나 고쳐 '남은 영상 모두 저장' → md 프런트매터에 고친 라벨 반영, 다운로드 영상 잔여 없음. 36컷 콘티 컷 20: 대사 대안 3개(실제 호출 11초) → 적용, 장면 6장(exact) 중 하나 고정 → md `ref_frame`·HTML '참고(고정)' 반영. 에디터로 md 수정 후 옛 해시 저장 → `conti_changed`. 터미널 `conti render` 후에도 대사·고정·메모 유지. `--stop` 후 lock 삭제.

완료 조건
- `tests/fixtures/links.txt`의 링크 2~3개를 화면에서 추가·저장하고, 기존 콘티에서 컷 하나의 대사 대안 받기·바꾸기·참고 프레임 고정 후 HTML을 육안 확인한다. Chrome 헤드리스 스크린샷으로 레이아웃을 확인한다

---

## Phase 14. 로컬 UI 2차 (추가 요건)

2차 메뉴는 1차에서 "준비 중"으로 표시만 한다. 각 화면은 해당 커맨드와 같은 규칙·검증을 쓰고, 저장은 사용자 클릭 뒤에만 한다.

- [x] 레퍼런스 목록(보기 전용): 영상 카드 목록·상세 (영상별 md 요약, 섹션 흐름, 프레임)
- [x] 레퍼런스 목록 편집 (섹션 라벨 수정, `reviewed` 표시(저장 시 true))
- [x] 유형화 화면 (`cluster` 결과, 축별 판정·아키타입)
- [x] 리포트 화면 (`report` HTML 보기 + 주간 회고: 숫자·회고 쓰기·저장·프로필 반영)
- [x] 모티브 화면 (카드 생성·★)
- [x] 기획안 화면 (`draft`): 목록·상세, 출처 선택 → 만들기 → 미리보기 편집 → 저장, 모티브 카드에서 바로 열기
- [x] 진단 화면 (`similarity`): 대상 목록, 비교하기(Claude 없음), 설명 받기 → 미리보기 편집 → 저장, 일부러 다르게 한 갭 표시
- [x] 미션 화면 (`mission`: 오늘 미션·제출·건너뛰기·교체)

---

## Phase 15. 다듬기 — `/yt-polish` (추가 요건)

계획: `docs/superpowers/plans/2026-10-24-polish.md`. 훅·제목·썸네일을 저장된 기획안에서 다듬고, 고른 것만 반영한다.

- [x] 설계서 v2.3 반영 (커맨드·`## 패키징`)
- [x] `skills/yt-polish/`: 훅 공식 21가지(`hooks.json`, MIT 출처) + SKILL.md, `THIRD_PARTY_NOTICES.md`
- [x] `polish.py`: 한국어 기준 점수·지적(제목 폭·숫자·기호·겹침, 훅 길이·인사), ref 근거, `context`·`evaluate`·`apply`
- [x] `commands/yt-polish.md`, README 표
- [x] 로컬 UI: 기획안 상세의 다듬기 영역 (`api_polish`, `draft.js`)

완료 조건
- 실제 draft로 `/yt-coach:yt-polish`를 돌려 후보·점수·근거가 보이고, 고른 것만 기획안에 반영되며 샷 표·내 메모가 그대로이고 이전 값이 이력에 남는다

---

## Phase 16. 움직이는 자막 영상 — Remotion (추가 요건)

계획: `docs/superpowers/plans/2026-10-24-caption-video.md`. 콘티 자막을 초록 배경 MP4로 렌더해 CapCut 크로마키로 쓴다.

- [x] `remotion/` 프로젝트 (Captions 컴포지션, 버전 고정, lock 커밋)
- [x] `conti.caption_blocks` 추출(SRT와 같은 계산), `captions.py` (`setup`·`check`·`render`, 취소·시간 제한·임시 교체)
- [x] 로컬 UI: `GET /api/captions/check`, `POST /api/conti/caption-video`, 콘티 화면의 "자막 영상 만들기(초록 배경)"
- [x] `commands/yt-conti.md` 6절, README 설치·라이선스·스크립트 표, 설계서 7.1

완료 조건
- 설치 후 콘티 화면에서 버튼으로 mp4를 만들고 받아, CapCut 맨 위 트랙에 얹어 크로마키로 초록이 빠진다(사용자 확인 대기)

---

## Phase 17. 공용 라이브러리 — `/yt-library` (추가 요건)

계획: `docs/superpowers/plans/2026-10-24-shared-library.md`. 지인들과 훅 기법·기획안 틀을 비공개 git 저장소로 나눈다.

- [x] `scripts/library.py` (connect·pull·search·get·미리보기·요약·confirm), `skills/yt-library`, `commands/yt-library.md`
- [ ] 재료로 쓰기 (모티브·기획안), MCP 서버, 로컬 UI 화면

완료 조건
- 두 사람이 거의 동시에 올려도 둘 다 올라가고, 인증 실패는 프롬프트 없이 한국어 원인으로 멈춘다

---

## Phase 18. 가이드·도움말 챗봇 (추가 요건)

계획: `docs/superpowers/specs/2026-10-25-help-chatbot-design.md`. 숏폼·롱폼 가이드 화면과 화면별 도움말 패널, 내 상황 기준으로 답하는 챗봇.

- [x] `docs/help/*.md`, `GET /api/help/<page>`, `POST /api/help/chat`(job `help_chat`), 도움말 패널·가이드 화면
- [x] README UI 절, 설계서 1.5

완료 조건
- [ ] 모티브 화면 도움말 챗봇에 "해보면 어떻게 돼?"를 물으면 첫 줄이 "예측 (내 상황 기준)"이고 실제 ref 수를 인용한다
- [ ] 가이드 숏폼 탭의 단계 링크로 해당 화면에 이동하고 완료 배지가 보인다
- [ ] 창 폭 800px에서 도움말 패널이 열리고 닫히며 Esc로도 닫힌다

---

## 일정 요약

| Phase | 내용 | 예상 |
|---|---|---|
| 0~1 | 뼈대, 초기화, md 저장소 | 1.5일 |
| 2~4 | 수집, 컷 통계, 단일 분석 | 4일 |
| 5~6 | 유형화, 모티브 | 3.5일 |
| 7~8 | 기획, 진단 | 2일 |
| 9 | 미션·회고 | 1.5일 |
| 10~11 | Vision 샷, 리포트 | 2.5일 |
| 12 | 콘티 (추가) | 반나절 |
| 13 | 로컬 UI 1차 (추가) | 계획서 참조 |
| 14 | 로컬 UI 2차 (추가) | 미정 |
| 15 | 다듬기 (추가) | 계획서 참조 |
| 16 | 움직이는 자막 영상 (추가) | 계획서 참조 |
| 17 | 공용 라이브러리 (추가) | 계획서 참조 |
| 18 | 가이드·도움말 챗봇 (추가) | 스펙 참조 |

**첫 마일스톤(Phase 0~4)**: 링크를 넣으면 영상별 md가 쌓이는 상태. 여기서 한 번 실사용하며 섹션 라벨·md 양식을 다듬고 나서 Phase 5로 간다.

## 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 자막 수집 차단 | Phase 2에서 조기 확인, 프록시·STT 경로 |
| 섹션 분할 일관성 | 라벨 고정 + 예시 + 사용자 수정본 few-shot |
| 표본 부족 | 단계별 기능 개방, 추가 수집 안내 |
| 사용량(자막·프레임) | 영상 단위 처리, md 캐시, 프레임 상한 40 |
| 영상 다운로드 | 저화질·개인용·추출 후 즉시 삭제 |
