# yt-coach — 유튜브 콘텐츠 분석·발상 Claude Code 플러그인 설계서

버전: v2.5 (2026-10-27, 콘티 이력·컷 편집(schema 3)·레퍼런스 화면 하나; v2.4 2026-10-24, 자막 영상 렌더 추가; v2.3 2026-10-24 다듬기 단계 추가; v2.2 2026-10-23 로컬 UI 추가; v2.1 2026-10-20 콘티 추가) / 상태: 개발 착수용

## 1. 목적과 범위

개인 크리에이터(초보) 1인이 로컬 Claude Code에서 사용하는 플러그인.
사용자가 직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 얻는 것이 1차 목적이다.
유형(아키타입) 분석은 모티브를 뽑기 위한 기반이고, 내 콘텐츠 진단과 일일 미션은 그 위에서 돌아가는 보조 루프다.

### 1.1 사용 흐름
`/yt-init` → `/yt-add`(ref) → `/yt-typify` → `/yt-inspire` → `/yt-draft` → `/yt-polish` → `/yt-conti` → (제작) → `/yt-diagnose`(mine) → `/yt-mission`, `/yt-review`

### 1.2 범위 안
- 첫 실행 인터뷰와 프로필 생성
- 링크 직접 입력, 메타데이터·자막 수집, 컷 통계
- 영상별 md: 시놉시스, 섹션, 기법, 촬영 요약
- 카테고리별 유형화 판정(축별), 아키타입
- 모티브 카드(흐름·기법·주제 각도·조합), 기법 카탈로그
- 대본 골격 + 섹션별 샷 리스트
- 콘티: 전체 대사 + 컷별 대사·자막·효과음·가이드, 도식 스케치와 레퍼런스 참고 프레임을 붙인 HTML 스토리보드
- 내 콘텐츠 진단(선택), 일일 미션, 주간 회고
- Vision 샷 분류 (후반 단계)

### 1.3 보류
- 채널 자동 수집, 신규 영상 감지
- YouTube Analytics(OAuth) 리텐션 연동
- 호스팅·다중 사용자 웹 서비스 (로컬 전용 UI `ytc ui`는 허용, 1.5절. 공용 라이브러리 = 지인들이 함께 쓰는 GitHub 비공개 저장소를 clone해 공유하는 것은 허용, 6.4절)

### 1.4 전제
- **로컬 Claude Code** 실행 (YouTube 접근 필요)
- 의미 해석은 Claude, 수집·계산은 Python 스크립트. 기본은 별도 키 없음(claude-cli); anthropic-api 엔진은 선택
- Python 3.11+, `yt-dlp`, `youtube-transcript-api`, `scenedetect`, `opencv-python-headless`, `scikit-learn`, `pyyaml`, (선택) `faster-whisper`, (선택) ffmpeg — 현재 미사용(컷: OpenCV, STT 디코딩: PyAV)

### 1.5 로컬 UI (v2.2)
기능을 내 PC에서만 열리는 웹 화면으로도 쓴다. 서버 중앙화·개인화·호스팅은 아니다. 전체 설계: [`docs/superpowers/specs/2026-10-23-local-ui-design.md`](docs/superpowers/specs/2026-10-23-local-ui-design.md)
- 실행: `ytc ui [--port N] [--no-open] [--detach] [--stop]`, Windows는 `bin/ytc.cmd`, Claude Code 안은 `/yt-ui [stop]`. `127.0.0.1`에만 바인딩하고 실행마다 만든 key를 주소에 넣어 key 없는 요청은 거부. 작업 폴더당 서버 하나(`workspace/ui.lock`).
- 구조: 서버(Python)가 `scripts.*`를 함수로 직접 호출하고, 해석이 필요한 일은 `claude -p`(사용자 구독, `skills/*/SKILL.md` 규칙 재사용)로 실행. 정적 HTML/CSS/JS, 빌드 단계 없음.
- 메뉴: 홈 / 모으기(레퍼런스 1차: 넣기·확인·목록·라벨 고치기를 한 화면, 공용 라이브러리) / 분석(유형화·리포트 2차) / 만들기(콘티 1차, 모티브·기획안 2차) / 성장(진단·미션 2차).
- 메뉴 모양(2026-10-26, shadcn/ui 사이드바 모양): 왼쪽 메뉴는 두 색 모드 모두 zinc-950(거의 검정). 홈·가이드는 작은 선 아이콘 + 흰 글씨, 그룹 이름(모으기·분석·만들기·성장)은 작은 회색 글씨, 하위 메뉴는 흰 글씨, 현재 화면은 zinc-800 칸. 그룹은 눌러 접고 펴며 접은 그룹은 브라우저에 기억한다(`ytc.menu.collapsed`). 다른 그룹에서 접힌 그룹 안 화면으로 들어가면 그 그룹을 펼친다(지금 화면의 그룹을 접은 것은 새로 고침해도 유지).
- 화면 모양(2026-10-26): shadcn/ui(new-york, zinc) 생김새를 React 없이 `app.css` 토큰과 규칙으로 옮겼다. 본문 바탕 흰색(다크 zinc-950), 카드는 흰 바탕(다크 zinc-900)·얇은 회색 테두리·모서리 12px·아주 옅은 그림자(`--elev-1`). 주요 버튼은 거의 검정(다크는 거의 흰색), 나머지 버튼·입력창은 흰 바탕 + 회색 테두리, 배지·꼬리표·필터는 모서리 6~8px 사각형. 포커스는 바탕색 2px + 회색 2px 링. 글자는 본문 14px, 제목 24px 600.
- 카드·유형 표시(2026-10-25): 필터·선택 버튼의 선택 상태는 주요 버튼 색(`--pick-bg`). 훅·서사·형식·섹션 라벨·본문 세부 유형 코드는 화면에서만 한국어로 보인다(`scripts/ui/static/labels.js`, 파일 값은 영어 코드 그대로). 레퍼런스 목록 카드: 썸네일 위 형식·길이·직접 고침(전체 보기에서는 역할도), 제목·채널, 훅·서사 꼬리표, 평균 컷·가장 많은 카메라 비율 칸, 섹션 띠와 훅·본문·마무리 길이. 목록 API는 `avg_cut_sec`(0보다 큰 수만)와 `camera`(static·pan·zoom 중 0~1 값만)를 더 낸다.
- 원칙: md 프런트매터가 유일한 원본이고 화면은 읽고 쓰는 창. 저장·바꾸기 클릭이 사용자 확인이며, 클릭 없이 AI 출력이 파일에 쓰이지 않는다(`--shots-only`, `script_mismatch` 자동 맞춤 제외). 화면 밖 수정은 내용 해시로 감지해 저장을 거부한다.
- LLM 엔진: 화면의 모든 Claude 호출은 `scripts/llm.py` 하나를 지난다. 기본 `claude-cli`(위 `claude -p`), 작업 폴더 `yt-coach.json`의 `{"llm": {"engine": "anthropic-api"}}` 또는 `YTC_LLM_ENGINE`으로 Anthropic Messages API(표준 라이브러리 HTTP, 키는 `ANTHROPIC_API_KEY` 환경 변수만)로 바꾼다. 새 엔진은 `check()`·`ask()`를 가진 모듈을 `ENGINES`에 등록하면 된다. 터미널 커맨드(Claude Code 대화)는 무관.
- 오래 걸리는 수집은 job으로 돌리고(동시에 하나), 화면을 닫아도 계속된다. 서버를 끄면 멈춘다.
- 가이드·도움말(v2.4): `#/guide/shorts`·`#/guide/long`은 숏폼·롱폼 흐름과 단계 링크·완료 배지를 보여주고, 모든 화면 오른쪽 패널([이 화면]/[챗봇])이 `docs/help/*.md`를 `GET /api/help/<page>`(허용 목록만)로 읽어 보여준다. 챗봇은 `POST /api/help/chat`(job kind `help_chat`, 120초 제한)이고 화면 설명·가이드·메뉴·상황 요약·단계 규칙만 근거로 답한다. 입력에서 제외하는 것: md 본문, 대본·자막, `## 내 메모`, 프레임, 작업 폴더 절대 경로, 파일 경로, 공용 라이브러리 원격 URL·작성자. 숫자와 상태 요약(`situation`)만 넣는다. 스펙: `docs/superpowers/specs/2026-10-25-help-chatbot-design.md`
- 레퍼런스 추가 진행률(v2.5): 수집 job은 영상마다 단계(자막·컷·분석·샷)별 상태(대기/진행/완료/실패/건너뜀)와 진행률, 예상 시간, 남은 시간, 끝날 시각을 스냅샷의 `timing`으로 낸다(`scripts/ui/progress.py`). 실측하는 것: 받기 바이트, 장면 감지·카메라 분석 프레임 수, STT 처리 위치, 단계별 걸린 시간. 추정하는 것: 컷은 영상 길이 기준(≈ 0.10×길이+7초, 실측 기반), 분석·샷·자막·STT는 가정값에서 시작해 `workspace/timings.json`(단계별 최근 20건, 분석·샷은 LLM 엔진별)이 쌓이면 그 중앙값으로 바뀐다. 단계 처리 방식이 바뀌면 기록에 방식 버전(`method`)을 올려 예전 기록을 예상에서 뺀다(컷 2: 카메라 판정을 한 번 순차 읽기로 바꿈. 찾아가 읽기보다 장면 감지와 같은 프레임 번호를 정확히 읽는다). 사용자 확인 대기와 차단 방지 대기는 경과에서 제외한다. 프라이버시: timings.json에는 단계 이름, 걸린 시간, 영상 길이·횟수 같은 숫자만 남기고 제목·링크·본문은 기록하지 않는다. 스펙: `docs/superpowers/specs/2026-10-25-add-progress-design.md`
- 레퍼런스 화면 하나(2026-10-27): 메뉴 "레퍼런스 추가"는 없어지고 `#/refs` 하나가 넣기·확인·목록을 맡는다(`#/add`는 넣기 영역을 펼친 `#/refs`). 링크는 줄마다 상태(새 영상/이미 있음/다른 구분으로 있음/중복/영상 링크 아님/다시 분석)를 보이고, 수집 job은 확인을 기다리지 않고 다음 영상으로 넘어간다. 끝난 영상은 "확인 대기" 줄(카드)로 쌓이며 저장·모두 저장·건너뛰기·고칠 점 적고 다시 분석을 한다. 저장하기 전 결과는 서버를 끄면 사라진다. 스펙: `docs/superpowers/specs/2026-10-27-refs-unified-design.md`
- 촬영함 체크: `POST /api/drafts/filmed` `{category, path, filmed, hash}` → `{path, filmed_at, hash}`. 기획안 내용 해시가 다르면 `draft_changed`, 프런트매터 `filmed_at`만 바꾼다.
- 새 스크립트: `status`(홈 데이터), `claude_cli`(Claude 호출기), `ui`(서버). `conti`에 `update`·`scenes`·`cut-context` 추가.

## 2. 저장소 구조

```
yt-coach/
├── .claude-plugin/plugin.json
├── commands/        yt-init, yt-add, yt-typify, yt-inspire, yt-draft, yt-polish, yt-conti,
│                    yt-diagnose, yt-mission, yt-review, yt-ui (.md)
├── skills/
│   ├── yt-analyze/   시놉시스·섹션·기법·촬영 요약 규칙
│   ├── yt-shots/     프레임 기반 샷 분류 규칙 (후반)
│   ├── yt-typify/    축 정의·판정 기준·아키타입 포맷
│   ├── yt-inspire/   모티브 카드 생성 규칙
│   ├── yt-draft/     대본 골격 + 샷 리스트 규칙
│   ├── yt-polish/    훅 5·제목 10·썸네일 지시서 규칙 + hooks.json(공식 21, MIT 출처)
│   ├── yt-conti/     전체 대사 + 컷별 대사·가이드 규칙 (footage-match.md: 촬영 장면 맞추기 규칙)
│   ├── yt-diagnose/  근접도·갭 서술 규칙
│   └── yt-mission/   성장 단계·미션·회고 규칙
├── bin/ytc, ytc.cmd 스크립트 실행기 (.venv Python 선택, 작업 폴더 전달. .cmd는 Git Bash 없는 Windows용)
├── scripts/
│   ├── fetch.py      메타데이터·자막 → workspace/raw/{id}.json
│   ├── stt.py        자막 없을 때 Whisper (선택)
│   ├── cuts.py       저화질 다운로드 → 장면 감지 → 컷 통계·대표 프레임 → 원본 삭제
│   ├── refstore.py   md 프런트매터 읽기/쓰기, 인덱스 재생성
│   ├── features.py   프런트매터 → 수치 피처
│   ├── cluster.py    축별 군집·판정 점수
│   ├── similarity.py mine vs 아키타입 근접도
│   ├── conti.py      draft → 콘티 검증·저장, 도식+참고 프레임 HTML 스토리보드
│   ├── footage.py    내 촬영 영상 한 파일 → 장면·사진 모음·받아쓰기 (PyAV 우선, OpenCV 대체)
│   ├── footage_match.py 콘티 컷 ↔ 촬영 장면 후보·판정(Claude 묶음 호출)·상태 파일
│   ├── footage_out.py 남은 컷 HTML·위치 CSV·컷 클립 mp4
│   ├── report.py     HTML 리포트
│   ├── status.py     카테고리별 진행 단계·다음 할 일 (UI 홈)
│   ├── claude_cli.py claude -p 호출기 (UI, `claude-cli` 엔진)
│   ├── llm.py        LLM 엔진 계층 (설정·점검·ask·ask_validated)
│   ├── llm_anthropic.py Anthropic Messages API 엔진 (`anthropic-api`)
│   └── ui/           로컬 UI 서버 + 정적 화면 (api_footage.py: 촬영 영상 라우트 `/api/footage/*`)
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
│   └── {date}-{slug}-conti.md / .html   콘티(프런트매터가 원본) + 스토리보드
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
- 삭제는 지우지 않고 옮긴다: 영상 md와 분석 데이터(workspace/raw·cuts·frames·shots, 다른 카테고리에 같은 영상이 없을 때만)를 `workspace/trash/<시각>/` 아래 같은 상대 경로로(`videomd trash`, UI "레퍼런스 삭제"). `techniques.md`에서 그 출처를 빼고, 유형화 결과는 다시 돌릴 때 반영된다

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
- 삭제는 지우지 않고 옮긴다: 기획안 md와 그 콘티(md·HTML·SRT·자막 영상)를 `workspace/trash/<시각>/`으로(`draft trash`, UI 기획안 보기의 "기획안 삭제")
- 프런트매터 `filmed_at`(YYYY-MM-DD, 선택 항목): 홈 화면에서 촬영함을 체크하면 오늘 날짜로 들어가고 해제하면 키가 지워진다. 사용자가 직접 고쳐도 된다. 계산 입력이 아니며 `schema_version`은 그대로다

### `/yt-polish <draft파일 | latest>`
- 저장된 draft의 **훅·제목·썸네일**을 다듬는 선택 단계(draft 뒤, 콘티 앞 어디서든). Claude가 훅 5개(공식 id·hook_type·`말:`/`자막:` 대본·이유)·제목 10개·썸네일 지시서(문구 3단어 이하·표정·구도·배경)를 내고, 스크립트가 한국어 기준 점수·지적(제목 폭 모바일 56·데스크톱 100, 숫자 없음, 느낌표 과다, 썸네일과 겹치는 낱말, 훅이 구간에 들어가는지, 인사로 시작)과 내 ref 근거("내 레퍼런스 N개 중 K개")를 붙인다
- 규칙은 `skills/yt-polish/`(훅 공식 21가지 `hooks.json`). Jakeschincariol/youtube-agent-skill(MIT)의 `yt-script`·`yt-package`를 한국어로 옮기고 고친 것이며 출처는 `THIRD_PARTY_NOTICES.md`. 영어 점수 도구·정규식은 가져오지 않는다
- 반영(A안): 사용자가 고른 뒤에만 draft를 고친다 — 프런트매터 `title`·`title_pattern`, 본문 `# ` 줄, 훅 섹션 대본 문단(고른 훅의 `hook_type`이 다르면 프런트매터 `hook_type`과 `## 개요`의 훅 줄도). `## 패키징`(내 메모 앞)에 썸네일 지시서·훅 공식·이전 제목/훅 유형/훅 대본 이력. 샷 표와 `## 내 메모`는 건드리지 않고, 화면을 연 뒤 파일이 바뀌었으면 `draft_changed`로 거부. 콘티가 있으면 다시 만들라는 경고
- 유형화·아키타입 기준은 바뀌지 않는다(`mine`을 섞지 않음, 근거는 ref만)

### `/yt-conti <draft파일 | latest>`
- draft의 샷 리스트를 컷 뼈대로 받아 **전체 대사**(영상 전체를 잇는 원고)를 쓰고, 컷마다 그 구간의 대사·자막·효과음·**가이드**(연기·촬영 지시)를 배정
- 대사 길이 검사: 레퍼런스 자막에서 잰 말 속도(초당 글자, 중앙값)로 컷 길이에 들어가는지 경고
- 시각화: 컷마다 샷 크기 도식(SVG) + 같은 샷 크기·섹션의 레퍼런스 대표 프레임(참고용, 구도만)
- `drafts/{draft}-conti.md`(프런트매터 `script`·`cuts`가 원본) + 같은 이름 `.html`(외부 자원 없는 한 파일, 인쇄·폰 보기)
- 사용자가 md를 고치면 `/yt-conti <conti.md>`로 HTML만 다시 그린다. AI 이미지 생성은 하지 않는다
- 삭제는 지우지 않고 옮긴다: 콘티 md·HTML과 그 콘티의 SRT·자막 영상을 `workspace/trash/<시각>/`으로(`conti trash`, UI "콘티 삭제"). 기획안은 남긴다

### `/yt-ui [stop]`
- 작업 폴더를 로컬 전용 브라우저 화면으로 연다(1.5절). `ytc ui --detach`로 세션과 분리해 띄우고 주소를 알린다. `stop`이면 `ytc ui --stop`

### `/yt-library [connect <주소> | pull | search <검색어> | share technique <cat> <종류> <이름> | share draft <파일>]`
- 지인과 함께 쓰는 비공개 git 저장소를 `workspace/library/`에 받아 두고(`library.json`에 주소·표시 이름), 훅 기법·기획안 틀을 찾거나 올린다. 상세: `docs/superpowers/specs/2026-10-24-shared-library-design.md`
- 올리기는 미리보기 → 사용자 확인 → `confirm`에서만(commit+push, force push 없음). 인증은 각자의 git 설정

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
- `yt-shots`(후반): Claude가 대표 프레임을 Read로 보고 샷 분류, 섹션에 매핑. 프레임별 라벨은 `workspace/shots/{id}.json`에 남겨 콘티 참고 프레임 매칭에 쓴다

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

### 6.1.1 `drafts/{draft}-conti.md`
```markdown
---
draft: drafts/2026-10-20-컵라면-4단계.md
category: mukbang
title: ...
format: shorts          # target_sec ≤ 180이면 shorts(세로 9:16 스케치)
target_sec: 45
speech_cps: 4.4         # 레퍼런스 말 속도 중앙값(없으면 4.5)
script:                 # 전체 대사, draft 섹션마다 하나
  - {label: hook, start: 0, end: 2, text: "4단계 다 먹을 수 있을까?"}
cuts:
  - {id: a1b2c3d4, n: 1, section: 0, start: 0, end: 1, size: 탑뷰, angle: 탑뷰, action: ..., line: ..., caption: ..., sfx: ..., guide: ..., ref_frame: workspace/frames/<id>/<file>.jpg}   # ref_frame은 선택
schema_version: 3       # 2: 컷의 선택 필드 ref_frame 추가. 3: 컷마다 안정적인 8자 id 추가
---
## 개요
## 전체 대사
## 콘티      | # | 시간 | 섹션 | 샷 | 대사 | 자막 | 효과음 | 가이드 |
## 내 메모
```
`render`는 `ref_frame`이 있으면 자동 매칭 대신 그 프레임을 쓰고, 없거나 파일이 사라졌으면 자동 매칭으로 돌아간다. schema_version 1·2 파일도 그대로 읽고, 처음 열 때 id가 없는 컷에 id를 붙여 3으로 올린다(내 메모·줄바꿈은 그대로).

**컷 편집 규칙**
- 컷 `id`는 한 번 붙으면 바뀌지 않는다(순서·번호·섹션이 바뀌어도, 지우고 되돌려도 같은 id). 번호 `n`과 시간은 순서에서 다시 계산한다.
- 컷 순서 바꾸기·섹션 경계 넘기기(섹션이 바뀜)·사이에 넣기·복제·삭제를 지원한다. 새 컷 기본값은 미디엄, 4초, "새 컷", "촬영 지시를 적어 주세요". 길이는 0.5~600초(합친 장면은 몇 분이 될 수 있다), 0.5 단위.
- 섹션의 마지막 컷은 지우거나 다른 섹션으로 옮길 수 없다("섹션에 컷이 하나는 있어야 해요").
- 섹션 시간 = 그 섹션 컷 길이의 합. `target_sec`와 형식(쇼츠/롱폼)은 그대로이고, 화면 머리에 "목표 15:00 → 지금 15:08 (+0:08)"처럼 차이를 보인다.
- 콘티가 SRT·자막 영상보다 나중에 바뀌었으면 그 영역에 "콘티가 바뀌었어요 — 다시 받아 주세요"를 띄운다.

**이력** (`workspace/conti_history/<콘티 파일 이름>/NNNN.json`, 원본 데이터라 gitignore)
- 종류: 처음 만듦 / 자동 / <사용자 이름> / 다시 만들기 전 / md 직접 수정 / 되돌림. 마지막 손댄 지 5분 안의 편집은 한 자동 버전으로 묶는다.
- 되돌리기는 `## 내 메모`를 지금 것 그대로 두고, 섹션 전체 대사는 그 버전에 저장된 그대로 되살린다(컷 대사에서 다시 만들지 않는다). 되돌리기 직전 상태도 이력에 남는다. 메모만 고친 것은 이력에 기록하지 않는다.
- 사용자가 md를 손으로 고친 것은 콘티를 열 때 감지해 "md 직접 수정"으로 남긴다.
- 콘티(또는 기획안)를 지우면 이력 폴더도 같은 휴지통 묶음(`workspace/trash/<시각>/`)으로 옮긴다.
- CLI: `conti history <conti.md>`, `history-name <conti.md> --label <이름> [--expect-hash]`, `history-restore <conti.md> --n <번호> [--expect-hash]`, `history-diff <conti.md> --a <번호> [--b <번호>]`(b 생략 시 지금과 비교).

### 6.2 `index.md` 섹션
`## 개요` / `## 단계` / `## 판정` (축별 표) / `## 아키타입` / `## 모티브 카드` / `## 이상치`

### 6.3 `profile.md`
`## 관심 주제` / `## 포맷` / `## 롤모델` / `## 하루 시간` / `## 피하고 싶은 스타일` / `## 강점` / `## 약점` / `## 성장 단계`

### 6.4 공용 라이브러리 파일 (`workspace/library/`, 지인 공유 저장소)
- `techniques/<category>/<id>.md` (kind: technique — author, category, technique_kind, name, worked?, note?, shared_at, schema_version 1; 본문 = 기법 설명 + `출처: 레퍼런스 N개`)
- `templates/<category>/<id>.md` (kind: template — title, target_sec, hook_type, narrative_type, sections[label,sub?,ratio], pacing.avg_cut_sec, shot_mix, worked?, note?; 본문 = `## 섹션 요약`(섹션별 한 줄) + `## 샷 리듬`)
- id = `<author-slug>-<YYYYMMDD>-<6자 해시>`. 대본 원문·내 메모·준비물·영상 id·개인 경로는 들어가지 않는다. 지인 파일은 신뢰하지 않는 데이터로 읽는다(모양이 틀리면 목록에서 뺌)

## 7. 손발 맞추기

| 장치 | 위치 | 반영 |
|---|---|---|
| md 직접 수정 (섹션·유형·메모) | refs/ | 다음 typify·inspire, 분석 few-shot |
| 모티브 ★ 선택 | index.md | draft·mission 재료 |
| 콘티 프런트매터 수정 | drafts/*-conti.md | `/yt-conti <conti.md>`로 HTML 다시 그림 |
| intentional 갭 | mine/ | 진단·미션 제외 |
| 미션 난이도·skip 사유 | missions/ | 미션 크기·종류 조정 |
| 주간 회고 승인 | reviews/, profile.md | 다음 주 방향 |

AI 출력은 제안. 파일 저장 전 사용자 확인.

### 7.1 자막 영상 렌더 (선택 설치, Remotion)

콘티 자막을 CapCut에서 쓸 "초록 배경 + 움직이는 자막" MP4로 렌더한다(`scripts/captions.py`, `remotion/`). 자막 블록과 시간은 SRT 내보내기와 같은 계산(`conti.caption_blocks`: 계획 시간 또는 편집본 컷에 맞춘 시간)을 공유한다. 출력은 `drafts/<기획안>-captions.mp4`(H.264, 30fps, `#00FF00`, 쇼츠 1080×1920·롱폼 1920×1080·편집본이면 그 비율). 렌더는 `npx remotion render` 하위 프로세스(cwd `remotion/`, 임시 mp4로 렌더한 뒤 교체, 시간 제한은 영상 길이에 비례한 전체 상한(`total_timeout`, 짧은 영상 900초)과 출력이 300초 동안 없을 때의 멈춤 감지, 취소 시 그 프로세스만 종료)이고, Node 18+와 사용자가 직접 실행하는 `ytc captions setup`이 있을 때만 켜진다(없으면 `remotion_missing`). 투명 배경 출력·스타일 편집 UI는 하지 않는다. Remotion은 개인·직원 3명 이하·비영리 무료.

### 7.2 촬영 영상 ↔ 콘티 맞추기 (v2.6)

사용자가 직접 찍은 영상을 콘티 컷에 이어, 컷마다 찍었는지 판정하고 남은 컷을 모은다. 스펙: `docs/superpowers/specs/2026-10-27-footage-matching-design.md`

- 입력: 콘티에 연결한 날짜별 촬영 폴더(여러 개, 나중에 추가, 하위 폴더 안 봄, UNC·네트워크 경로 거부 — PC로 복사). 연결은 콘티 md가 아니라 `workspace/footage_match/<콘티 stem>.json`에 둔다. 새 파일만 처리한다(파일 지문 `fid`가 같으면 건너뜀).
- 처리(`scripts/footage.py`): 파일마다 장면 나누기(60초 넘는 장면은 분할, 최소 1초) → 장면당 사진 → 장면 모음 이미지(montage) → 받아쓰기(`faster-whisper`가 있을 때만, 30분 창 단위). 디코더는 PyAV 우선, 실패하면 OpenCV. 폴더 연결은 UNC뿐 아니라 매핑된 네트워크 드라이브(`DRIVE_REMOTE`)도 거부한다. 결과는 `workspace/footage/<fid>/`(scenes.json, transcript.json, frames/, montage). CPU만 쓰며 2시간 1080p ≈ 25~30분, 4K ≈ 30~45분(합성 영상 실측: 1080p 25~27분, 4K HEVC 31~43분. 실제 촬영본에서 다시 잰 값은 아님).
- 맞추기(`scripts/footage_match.py`, 규칙 `skills/yt-conti/footage-match.md`): 글자 후보(로컬)를 만든 뒤 Claude 묶음 호출로 컷마다 후보 최대 3개(`MAX_CANDIDATES`, 더 적을 수 있음)와 판정을 받는다. 확실하면 자동 "찍음", 사용자는 고르기·"안 찍음" 표시·AI 판정으로 되돌리기. 콘티에 없는 쓸 만한 장면은 제안(넣기·숨기기). 상한에 걸려 보지 못한 장면(말이 많은 장면은 뒤로 밀림)에 기대는 컷은 "사진 상한으로 AI가 보지 못함" 이유와 함께 확인 필요로 남는다. AI에 보낸 장면은 상태 파일 `seen_scenes`(`{<fid>@<시작초>: 마지막으로 보낸 맞추기 번호}`, 예전 목록 형식은 0번으로 읽음, 지금 쓸 수 없는 파일의 장면은 지움)에 남겨, 다시 맞추면 보낸 적 없는 장면, 그다음 가장 오래전에 보낸 장면부터 보낸다(여러 번 맞추면 돌아가며 모두 본다). 판정에는 컷 내용(섹션·대사·가이드·행동·샷 크기)과 맞춘 방식(글자·사진)의 지문 `sig`를 남기고, 사진 컷이 지난번과 같은 `sig`일 때만 이번에 다시 보내지 않은 장면에서 앞서 찾은 AI 후보를 이어받는다(파일을 아직 쓸 수 있을 때. 고친 컷은 다시 판정). 묶음 호출이 실패해 새 후보를 하나도 못 받은 글자 컷은 이전 판정을 두고 이유만 "AI 호출이 실패해 다시 맞춰야 해요"로 바꾸고, 사진 컷은 이어받을 후보만으로 같은 이유를 달아 다시 정한다. 연결 폴더가 잠시 빠졌으면(외장 드라이브 분리) 지난 처리에서 본 그 폴더의 파일 지문(`folder_fids`)으로 캐시 결과를 맞추기에 넘겨, 그 파일로 정한 자동 판정을 지우지 않는다(사용자가 "빼기"로 끊은 폴더는 넘기지 않는다). **촬영본 이미지 상한: Claude 호출 하나에 8장, 한 번 실행에 120장.** 이것은 레퍼런스 규칙(장면당 1장, 영상당 최대 40장, 8절)과 별개다. 레퍼런스는 다운로드 영상을 즉시 삭제하는 분석용이고, 촬영본은 사용자 원본을 읽기만 하며 한 번의 맞추기 호출 비용을 묶는 상한이다.
- 출력(`scripts/footage_out.py`): `drafts/<기획안>-remaining.html`(폰용 남은 컷, 안 찍음 컷 포함, 한 파일), `drafts/<기획안>-footage.csv`(위치, UTF-8 BOM), `drafts/<기획안>-cuts/NN_<원본 이름>.mp4`(고른 구간을 기본은 재압축 없이 잘라 낸 클립(키프레임 때문에 시작이 몇 초 앞당겨질 수 있음). 잘라 붙이기(remux)가 실패하거나 시작이 너무 어긋나면 H.264+AAC로 다시 압축해 시작을 정확히 맞추고(`reencoded: true`, 느림), 멈추기는 컷 사이에서만 듣는다. 만들기 전 그 폴더의 이전 클립만 지움). 콘티 삭제 시 이 출력과 상태 파일도 휴지통으로 함께 옮기며, 되돌릴 때 상태 파일은 `workspace/footage_match/`로 돌려놓는다. 화면 표시: 파일이 사라진 컷은 "원본 없음", 열 수 없는 파일은 폴더 줄의 "읽을 수 없음 N개".
- UI(`scripts/ui/api_footage.py`): `GET /api/footage`, `POST /api/footage/{folders, folders/remove, process, choose, not-filmed, reset, suggestions/hide, suggestions/insert, remaining, csv, clips}`, 미디어 `GET /api/footage/media/<fid>`·`/api/footage/frame/<fid>/<rel>`. 처리·맞추기(`footage`)·클립 만들기(`footage_clips`)는 서로, 그리고 레퍼런스 수집(`collect`)의 무거운 작업과 동시에 돌지 않으며(`JobManager.start`; 사용자 확인만 기다리는 수집 대기는 예외), 콘티·기획안 삭제도 막는다.
- 원칙: 원본 영상은 읽기만 한다. 쓰기는 작업 폴더(`workspace/footage`, `workspace/footage_match`, `drafts/`)에만. 의미 판정은 Claude(제안), 수집·계산·출력은 스크립트. 알려진 한계: HEVC는 Chrome·Edge에서 재생 안 될 수 있어 사진 띠로 대체(변환 안 함, 소리만 나오고 화면이 검은 경우는 감지 못 함), 받아쓰기 30분 경계에 걸친 말은 둘로 나뉠 수 있음.

### 7.3 장면 단위 콘티와 내용 맞춤 사진 (v2.7)
스펙: `docs/superpowers/specs/2026-10-29-scene-conti-design.md`.
- **장면 단위**: 샷(draft)·컷(콘티) 하나는 편집 컷이 아니라 **찍을 장면 하나**다. 같은 섹션 안에서 `action`이 같은 샷·컷은 한 장면이고(연속이 아니어도, 쇼츠도 예외 없음), 길이는 찍어 쓸 분량(롱폼 대개 10~60초, 쇼츠 1~5초)이다. 아키타입 평균 컷 길이는 `guide`의 편집 메모 근거로만 쓴다. `yt-conti`는 예전 기획안의 반복 샷도 같은 규칙으로 묶어 만든다(기획안 md는 고치지 않는다). 컷 길이 상한 `MAX_CUT_SEC`는 600초.
- **기존 콘티 합치기** (`scripts/scene_merge.py`, 규칙 `skills/yt-conti/merge.md`): `conti merge <conti.md>`(또는 `POST /api/conti/merge/preview`)가 반복 컷을 묶는 계획을 미리보기 JSON으로 낸다. 장면 위치는 첫 등장 자리, 길이는 합, 대표 size/angle은 첫 컷, 컷 id는 첫 컷의 것. 대사는 원래 컷 순서로 이은 글을 Claude가 글자를 바꾸지 않고 장면에 다시 나눈다(검증: 장면 `line`을 이으면 그 글). 저장은 `conti merge <conti.md> --cuts <json> --hash <sha>`(`POST /api/conti/merge/apply`)이고, 저장 전 이력에 `before_merge`("합치기 전")를 남긴다. 촬영 판정은 사용자 판정만 합쳐진 장면(첫 사용자 판정)으로 옮기고 AI 판정은 버린다. 사라진 id의 사용자 판정은 상태에 남겨 두어 "합치기 전"으로 되돌리면 다시 보인다. 해시가 미리보기 뒤 달라졌으면 `conti_changed`.
- **프레임 한 줄 설명 `desc`**: 레퍼런스 대표 프레임마다 한 줄 설명(1~60자)을 **프레임당 한 번** 만든다. 저장 위치는 `workspace/shots/<video_id>.json`의 프레임 라벨 `desc`(영상 md는 건드리지 않는다). CLI: `shots describe-context`·`apply-desc`·`desc-status`. 화면은 `POST /api/refs/describe`(job `refs_describe`)와 `GET /api/refs/describe-status`. 샷 분류(`shots apply`)가 라벨을 다시 쓸 때도 기존 `desc`는 남긴다.
- **컷의 `ref_auto`**: 컷의 선택 필드 `{frame, match: "content"}`. Claude가 장면(id·action·guide·size)과 `desc`가 있는 후보 목록을 글로만 맞춰 고른 사진이다(이미지 없음, `skills/yt-conti/pick-refs.md`, `scripts/conti_refs.py`, 장면 40개 묶음). 콘티를 만들 때와 `POST /api/conti/refs/pick`(job `conti_refs`, 화면 "사진 다시 고르기", CLI 없음)에서만 부르고, 렌더는 저장된 값만 쓴다. 표시 순서는 `ref_frame`(사용자 고정, "참고(고정)") → `ref_auto`("참고") → 샷 크기·섹션 맞춤("구도만 참고"). 파일이 사라졌으면 다음 단계로 내려간다. 합칠 때는 합쳐지는 컷 중 첫 컷의 `ref_auto`를 이어받는다. `ref_auto`는 표시용 필드지만 저장하면 이력에 auto 버전으로 남는다.
- **이미지 상한의 구분**: 사진 설명 job은 **Claude 호출 하나에 이미지 8장**(`shots.DESC_BATCH`)이고 약 840장이면 약 105번, 한 번만 한다. 레퍼런스 분석의 "장면당 1장, 영상당 최대 40장"(8절)과 촬영본 맞추기의 "호출당 8장·실행당 120장"(7.2)은 그대로이고 서로 별개다. 사진 고르기·합치기 호출은 이미지를 보내지 않는다.
- 스키마: `ref_auto`는 선택 필드라 `schema_version`은 3 그대로다(예전 파일도 그대로 읽는다).

### 7.4 장면 그림 (v2.8)
스펙: `docs/superpowers/specs/2026-10-30-scene-art-design.md`.
- **컷의 `art`**: 컷의 선택 필드 `{file, basis}`. Claude가 장면의 `action`·`guide`·`size`(+ 화면 비율)로 그린 SVG 일러스트 한 장이다(`skills/yt-conti/draw-scenes.md`, `scripts/conti_art.py`, 호출당 2장면, 한 번에 최대 80장면). `basis`는 그릴 때 입력의 해시이고, 지금 값으로 다시 계산해 다르면 옛 그림(stale)이다. 옛 그림은 새 그림이 올 때까지 계속 보이고, 화면이 고친 지 4초 뒤 옛 그림 장면만 다시 그린다. 반복 컷이 남은 콘티는 `merge_first`로 거절한다. 화면 전용이라 CLI 명령은 없다(`POST /api/conti/art`, job `conti_art`).
- **표시 순서**: 고정 사진(`ref_frame`) → 장면 그림(`art`) → 내용 맞춤 사진(`ref_auto`) → 구도 맞춤 사진 → 도식. 고정된 장면의 `art`는 지우지 않고 숨긴다. 레퍼런스 사진은 장면 상세에서 계속 본다.
- **표시용 캐시 예외**: `art`는 `ref_auto`처럼 md 본문과 계산 입력을 바꾸지 않고 언제든 다시 그릴 수 있어, 저장 전 사용자 확인 없이 저장한다(핵심 원칙 "AI 출력은 확인 후 저장"의 예외). 저장하면 이력에 auto 버전으로 남는다.
- **파일**: `workspace/conti-art/<콘티 이름>/<컷 id>-<basis>.svg`(gitignore). 저장 전 서버가 SVG를 검사해 허용 요소만 남기고, 화면은 `<img>`로만 보여 준다. 옛 파일은 이력 복원을 위해 지우지 않는다.
- 스키마: `art`는 선택 필드라 `schema_version`은 3 그대로다.

## 8. 비용·운영
- 영상은 하나씩 처리, md가 있으면 재분석 안 함
- 프레임은 장면당 1장, 영상당 최대 40장 (레퍼런스 한정. 촬영 영상 맞추기는 7.2의 호출당 8장·실행당 120장)
- 자막 차단 시 `YT_PROXY` 환경변수 안내
- 영상 원본은 프레임 추출 직후 삭제, 개인 분석 용도로만 사용
