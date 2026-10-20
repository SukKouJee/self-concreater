# yt-coach — 유튜브 콘텐츠 분석·발상 Claude Code 플러그인 설계서

버전: v2 (2026-10-20) / 상태: 개발 착수용

## 1. 목적과 범위

개인 크리에이터(초보) 1인이 로컬 Claude Code에서 사용하는 플러그인.
사용자가 직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 얻는 것이 1차 목적이다.
유형(아키타입) 분석은 모티브를 뽑기 위한 기반이고, 내 콘텐츠 진단과 일일 미션은 그 위에서 돌아가는 보조 루프다.

### 1.1 사용 흐름
`/yt-init` → `/yt-add`(ref) → `/yt-typify` → `/yt-inspire` → `/yt-draft` → (제작) → `/yt-diagnose`(mine) → `/yt-mission`, `/yt-review`

### 1.2 범위 안
- 첫 실행 인터뷰와 프로필 생성
- 링크 직접 입력, 메타데이터·자막 수집, 컷 통계
- 영상별 md: 시놉시스, 섹션, 기법, 촬영 요약
- 카테고리별 유형화 판정(축별), 아키타입
- 모티브 카드(흐름·기법·주제 각도·조합), 기법 카탈로그
- 대본 골격 + 섹션별 샷 리스트
- 내 콘텐츠 진단(선택), 일일 미션, 주간 회고
- Vision 샷 분류 (후반 단계)

### 1.3 보류
- 채널 자동 수집, 신규 영상 감지
- YouTube Analytics(OAuth) 리텐션 연동
- 웹 대시보드 (HTML 리포트로 대체)

### 1.4 전제
- **로컬 Claude Code** 실행 (YouTube 접근 필요)
- 의미 해석은 Claude, 수집·계산은 Python 스크립트. 별도 LLM API 키 없음
- Python 3.11+, ffmpeg, `yt-dlp`, `youtube-transcript-api`, `scenedetect`, `opencv-python-headless`, `scikit-learn`, `pyyaml`, (선택) `faster-whisper`

## 2. 저장소 구조

```
yt-coach/
├── .claude-plugin/plugin.json
├── commands/        yt-init, yt-add, yt-typify, yt-inspire, yt-draft,
│                    yt-diagnose, yt-mission, yt-review (.md)
├── skills/
│   ├── yt-analyze/   시놉시스·섹션·기법·촬영 요약 규칙
│   ├── yt-shots/     프레임 기반 샷 분류 규칙 (후반)
│   ├── yt-typify/    축 정의·판정 기준·아키타입 포맷
│   ├── yt-inspire/   모티브 카드 생성 규칙
│   ├── yt-draft/     대본 골격 + 샷 리스트 규칙
│   ├── yt-diagnose/  근접도·갭 서술 규칙
│   └── yt-mission/   성장 단계·미션·회고 규칙
├── bin/ytc          스크립트 실행기 (.venv Python 선택, 작업 폴더 전달)
├── scripts/
│   ├── fetch.py      메타데이터·자막 → workspace/raw/{id}.json
│   ├── stt.py        자막 없을 때 Whisper (선택)
│   ├── cuts.py       저화질 다운로드 → 장면 감지 → 컷 통계·대표 프레임 → 원본 삭제
│   ├── refstore.py   md 프런트매터 읽기/쓰기, 인덱스 재생성
│   ├── features.py   프런트매터 → 수치 피처
│   ├── cluster.py    축별 군집·판정 점수
│   ├── similarity.py mine vs 아키타입 근접도
│   └── report.py     HTML 리포트
├── templates/        video.md, index.md, techniques.md, profile.md, mission.md, thresholds.json
├── CLAUDE.md
└── README.md

<사용자 작업 폴더>/        # 플러그인을 쓰는 프로젝트 루트
├── profile.md
├── refs/
│   └── ref_<category>/
│       ├── index.md        개요·판정·아키타입·모티브 카드
│       ├── techniques.md   기법 카탈로그 (훅·전환·엔딩·촬영)
│       └── videos/{video_id}.md
├── mine/{id}.md            내 콘텐츠·대본·진단 결과
├── drafts/{date}-{slug}.md 대본 골격 + 샷 리스트
├── missions/{YYYY-MM-DD}.md
├── reviews/{YYYY-Www}.md
└── workspace/              gitignore: raw 자막 JSON, 프레임, 캐시
```

원칙
- **md가 지식층(사람·Claude가 읽고 고침), workspace가 원본층(기계용).**
- 스크립트는 md 프런트매터만 계산 입력으로 쓴다. 사용자가 md를 고치면 그것이 곧 교정이다.
- 교정 이력은 git diff로 본다. 작업 폴더를 git으로 관리하도록 `/yt-init`에서 권한다.

## 3. 커맨드

### `/yt-init`
- 폴더·템플릿 생성, git init 제안
- 인터뷰 3~5문항: 관심 주제, 포맷(쇼츠/롱폼), 롤모델 채널, 하루 투자 시간, 피하고 싶은 스타일
- `profile.md` 초안 → 사용자 확인 → 첫 카테고리 생성 → "링크 3개부터" 안내

### `/yt-add <category> [ref|mine] <links...>`
- 기본 role은 `ref`. 같은 영상을 ref와 mine에 동시 등록 불가
- 영상별 순차 처리: `fetch.py` → (자막 없으면 STT 여부 질문) → `cuts.py` → `yt-analyze` → `videos/{id}.md` 생성
- 이미 md가 있으면 재분석하지 않음 (`--force`로만)
- `--shots-only <category>`: 기존 영상에 Vision 샷 분류만 보강 (후반, 5.4)
- 처리 후 `techniques.md` 갱신, 카테고리 단계 안내(4절)

### `/yt-typify <category>`
- **ref만 사용**, mine은 스크립트에서 필터 (끌 수 없음)
- 단계별 동작: 5개 미만 중단 / 5~9개 예비 관찰 / 10개 이상 정식 판정
- 축별 `typable | weak | untypable` + 근거 문장 + 아키타입(이름은 제안 후 사용자 확정)
- 결과는 `index.md`의 판정 섹션에 기록

### `/yt-inspire <category> [주제]`
- 입력: index.md, techniques.md, profile.md(관심 주제), 선택 주제
- 출력: 모티브 카드 3~6장, `index.md`의 모티브 섹션에 누적
  - 흐름 모티브: 아키타입 흐름을 내 주제에 적용한 예
  - 기법 모티브: untypable 영상 포함, 떼어 쓸 수 있는 훅·전환·엔딩·촬영 기법
  - 각도 모티브: 레퍼런스가 덜 다룬 주제 각도
  - 조합 모티브: 유형 구조 + 영상 X 훅 + 영상 Y 엔딩
- 사용자가 고른 카드는 `★`로 표시
- 단계별 카드 종류: 0~4개 기법 / 5~9개 기법·각도 / 10개 이상 4종 모두

### `/yt-draft <category> <motif-id | archetype> "<주제>"`
- 섹션별 대본 + 샷 리스트(샷 크기·앵글·길이·컷 수)
- profile의 피하고 싶은 스타일 반영
- `drafts/`에 저장

### `/yt-diagnose <category> <link | draft파일 | 대본파일>`
- 아키타입 대비 축별 근접도 + 갭. 단일 종합 점수 없음
- 갭별 `intentional` 표시 시 이후 점수·미션에서 제외
- 결과는 아키타입을 바꾸지 않음. `mine/`에 저장
- ref 10개 미만이면 "예비 진단"으로 표시하고 근접도 대신 관찰 위주로 서술

### `/yt-mission [today | submit <file> | skip | swap]`
- 하루 1~2개, 15~30분. 성장 단계: 관찰 → 모방 → 제작 → 변형
- 재료: ★모티브, 최근 draft, 진단 갭, 미션 이력
- 촬영 미션 포함 (예: 탑뷰 손 클로즈업 15초)
- 제출 시 짧은 피드백 + 난이도 평가 기록

### `/yt-review`
- 주간: 쌓인 레퍼런스·모티브, 미션 완료율, 반복 약점, 다음 주 방향 제안 → 승인 시 profile 반영

## 4. 점진적 단계 (레퍼런스 수 기준)

| 단계 | ref 수 | 가능한 것 |
|---|---|---|
| 1 | 0~4 | 영상별 분석, 기법 카탈로그, 기법 모티브 |
| 2 | 5~9 | 예비 관찰("비슷한 구조가 보임"), 각도 모티브, 예비 진단, 추가 수집 안내 |
| 3 | 10+ | 정식 유형화, 흐름·조합 모티브, 진단 |

`/yt-add` 후마다 현재 단계와 "다음 단계까지 무엇을 더 넣으면 좋은지"를 안내한다.

## 5. 분석 체계

### 5.1 섹션 라벨 (고정)
`hook / intro / body / demo / transition / cta / outro`
body 하위: `list_item / story / explain / compare`

### 5.2 유형 축
| 축 | 피처 | 단계 |
|---|---|---|
| structure | 섹션 순서·비율·총 길이 | MVP |
| hook | 훅 방식(질문/결과선공개/충격문장/상황제시)·길이 | MVP |
| narrative | 문제→해결 / 리스트 / 여정 / 비교 / 반응 | MVP |
| title | 숫자·질문·괄호·감탄 패턴, 길이 | MVP |
| pacing | 평균 컷 길이, 섹션별 컷 빈도 | MVP (cuts.py) |
| shots | 샷 크기·앵글·얼굴출연·텍스트오버레이 분포 | 후반 (Vision) |

### 5.3 판정 기준
- 실루엣 점수, 최대 군집 점유율, 표본 수 조합. 임계값은 `thresholds.json`
- 근거 문장 필수

### 5.4 촬영 분석
- `cuts.py`: yt-dlp 360p 이하 다운로드 → PySceneDetect → 컷 타임라인, 섹션별 평균 컷 길이 → 장면당 대표 프레임 1장(최대 40) 저장 → **원본 영상 즉시 삭제**
- 카메라 움직임: optical flow로 고정/패닝/줌 추정, 참고값 표시
- `yt-shots`(후반): Claude가 대표 프레임을 Read로 보고 샷 분류, 섹션에 매핑

## 6. md 스키마

### 6.1 `videos/{id}.md`
```markdown
---
id: abc123
url: https://youtu.be/abc123
role: ref
channel: ...
title: ...
duration_sec: 58
format: shorts
transcript_source: cc
sections:
  - {label: hook, start: 0, end: 3}
  - {label: body, sub: explain, start: 3, end: 48}
  - {label: cta, start: 48, end: 58}
hook_type: result_first
narrative_type: problem_solution
title_pattern: [number, bracket]
pacing: {avg_cut_sec: 1.8, by_section: {hook: 0.9, body: 2.1}}
shots: null          # 후반 단계에서 채움
camera: {static: 0.7, pan: 0.2, zoom: 0.1}
reviewed: false      # 사용자가 고친 뒤 true → 분석 few-shot 예시
schema_version: 1
---
## 한 줄 요약
## 시놉시스
## 섹션
| 구간 | 라벨 | 내용 | 촬영 |
## 눈에 띄는 기법
## 내 메모
```

### 6.2 `index.md` 섹션
`## 개요` / `## 단계` / `## 판정` (축별 표) / `## 아키타입` / `## 모티브 카드` / `## 이상치`

### 6.3 `profile.md`
`## 관심 주제` / `## 포맷` / `## 롤모델` / `## 하루 시간` / `## 피하고 싶은 스타일` / `## 강점` / `## 약점` / `## 성장 단계`

## 7. 손발 맞추기

| 장치 | 위치 | 반영 |
|---|---|---|
| md 직접 수정 (섹션·유형·메모) | refs/ | 다음 typify·inspire, 분석 few-shot |
| 모티브 ★ 선택 | index.md | draft·mission 재료 |
| intentional 갭 | mine/ | 진단·미션 제외 |
| 미션 난이도·skip 사유 | missions/ | 미션 크기·종류 조정 |
| 주간 회고 승인 | reviews/, profile.md | 다음 주 방향 |

AI 출력은 제안. 파일 저장 전 사용자 확인.

## 8. 비용·운영
- 영상은 하나씩 처리, md가 있으면 재분석 안 함
- 프레임은 장면당 1장, 영상당 최대 40장
- 자막 차단 시 `YT_PROXY` 환경변수 안내
- 영상 원본은 프레임 추출 직후 삭제, 개인 분석 용도로만 사용
