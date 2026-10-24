# yt-coach

직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 주고,
기획·진단·일일 미션으로 이어 주는 개인 크리에이터용 Claude Code 플러그인입니다.

- 저장소: https://github.com/SukKouJee/self-concreater (비공개)
- 설계: [`yt-coach-plugin-design.md`](yt-coach-plugin-design.md) (v2.2)
- 로컬 UI 설계: [`docs/superpowers/specs/2026-10-23-local-ui-design.md`](docs/superpowers/specs/2026-10-23-local-ui-design.md)
- 개발 계획: [`PLAN.md`](PLAN.md)

## 커맨드

플러그인으로 설치하면 `/yt-coach:<커맨드>` 형태로도 부를 수 있습니다.

| 커맨드 | 하는 일 | 상태 |
|---|---|---|
| `/yt-init [category]` | 작업 폴더·템플릿 생성, 인터뷰로 `profile.md` 작성, 첫 카테고리 생성 | ✅ |
| `/yt-add <category> [ref\|mine] <links...> [--force] [--shots]` | 링크별 자막·컷 통계 수집 → 분석 → 확인 후 영상별 md 생성, 기법 카탈로그 갱신. `--shots-only`로 기존 영상 샷 분류 보강 | ✅ |
| `/yt-typify <category>` | ref 영상으로 축별 유형화 판정·아키타입·이상치 → index.md | ✅ |
| `/yt-inspire <category> [주제]` | 모티브 카드(흐름·기법·각도·조합) 생성·누적, `star <id>`로 ★ 표시, 기법 카탈로그 중복 병합 | ✅ |
| `/yt-draft <category> <motif-id\|archetype> "<주제>"` | 섹션별 대본 + 샷 리스트 → `drafts/`, 아키타입 비율 비교 | ✅ |
| `/yt-polish <draft\|latest>` | 저장된 기획안의 훅 5개·제목 10개·썸네일 지시서를 만들고, 한국어 기준 점수·지적과 내 레퍼런스 근거를 붙여 보여 준다. 고른 것만 기획안에 반영(제목·훅 대본·훅 유형 교체, `## 패키징`에 썸네일 지시서와 이전 값 이력) | ✅ |
| `/yt-conti <draft\|latest\|-conti.md>` | 기획안 → 전체 대사 + 컷별 대사·자막·효과음·가이드 콘티(`-conti.md`), 샷 도식 + 레퍼런스 참고 프레임 HTML 스토리보드. `-conti.md`를 고치면 다시 그리기 | ✅ |
| `/yt-diagnose <category> <link\|draft\|대본>` | 내 콘텐츠를 아키타입과 축별 비교(근접도·갭·다음 행동) → `mine/`, `intentional`로 의도한 차이 제외 | ✅ |
| `/yt-mission [today\|submit <file>\|skip\|swap]` | 하루 15~30분 미션, 제출 피드백·난이도 기록, 난이도에 따라 크기 조정 | ✅ |
| `/yt-review [YYYY-Www]` | 주간 회고 → `reviews/`, 카테고리 HTML 리포트 링크, 승인 시 profile 갱신 | ✅ |
| `/yt-ui [stop]` | 작업 폴더를 브라우저 화면(로컬 전용)으로 연다. Claude Code를 닫아도 화면은 남고, `stop`으로 끈다 | ✅ |

## 로컬 UI

커맨드를 외우지 않고 브라우저 화면에서 같은 일을 합니다. 내 PC(127.0.0.1)에서만 열리고, 주소에 든 key가 있어야 접근됩니다.
화면에서 Claude가 필요한 일은 설치된 `claude` CLI(내 구독)로 실행하며, 별도 API 키는 필요 없습니다.
md 프런트매터가 여전히 유일한 원본이고, 화면은 그것을 읽고 쓰는 창입니다. AI 결과는 저장/바꾸기 버튼을 눌러야만 파일에 기록됩니다.

```bash
ytc ui                   # 작업 폴더에서 실행. 빈 포트로 띄우고 브라우저를 엽니다
bin\ytc.cmd ui           # Git Bash 없는 Windows
/yt-coach:yt-ui          # Claude Code 안에서. 세션과 분리돼 Claude Code를 닫아도 남습니다
```

- 옵션: `--port N`, `--no-open`(브라우저 안 열기), `--detach`(분리 실행, 로그 `workspace/ui.log`), `--stop`(끄기. `/yt-coach:yt-ui stop`도 같음)
- 작업 폴더당 서버는 하나입니다. 이미 떠 있으면 새로 띄우지 않고 그 주소를 돌려줍니다.
- 끄기: 화면 위쪽 "서버 끄기" 또는 `ytc ui --stop`. 진행 중인 작업이 있으면 경고합니다.

| 묶음 | 메뉴 | 상태 |
|---|---|---|
| 홈 | 카테고리별 진행 단계, 다음 할 일, 최근 결과물 | 1차 ✅ |
| 모으기 | 레퍼런스 추가 (`/yt-add` 전체: 진행, 확인 카드, 섹션 라벨 수정, 다시 분석) | 1차 ✅ |
| 모으기 | 레퍼런스 목록 (영상 카드, 섹션 흐름 띠, 상세에서 프레임·본문 확인, 섹션 라벨·훅/서사 유형 고치기. 고친 영상은 "직접 고침" 표시, 본문 표는 분석 당시 기록 그대로) | 2차(보기·라벨 고치기) ✅ |
| 분석 | 유형화 (ref 수·단계, 축별 판정, 아키타입 카드, 이상치. 실행 → 미리보기에서 근거·이름 고치기 → 저장) | 2차 ✅ |
| 분석 | 리포트 | 2차 (터미널: `/yt-coach:yt-review`) |
| 만들기 | 콘티 (컷별 레퍼런스 장면·대사 대안을 클릭으로 고르기) | 1차 ✅ |
| 만들기 | 모티브 (새 카드 뽑기 → 미리보기에서 고쳐 고른 카드만 저장, 저장한 카드 거르기·★·"기획안 만들기"로 그 카드를 출처로 기획안 화면 열기. 기법 카탈로그 병합은 터미널 `/yt-coach:yt-inspire`) | 2차 ✅ |
| 만들기 | 기획안 (저장한 기획안 목록·상세 읽기 전용, 출처 고르기 → 만들기 → 미리보기에서 제목·대본·샷 표·준비물 고쳐 저장, 콘티 화면으로 연결) | 2차 ✅ |
| 성장 | 진단, 미션 | 2차 (터미널: `/yt-coach:yt-diagnose`, `/yt-coach:yt-mission`) |

2차 메뉴는 화면에 보이되 "준비 중"과 같은 일을 하는 터미널 커맨드만 안내합니다.

## 설치

### 1. 준비물

- Python 3.11+
- (선택) ffmpeg — 지금은 필요 없습니다. 컷 분석은 OpenCV, STT 디코딩은 PyAV로 합니다
- Claude Code (로컬 실행. YouTube 접근이 필요합니다)

### 2. Python 의존성

```bash
git clone https://github.com/SukKouJee/self-concreater.git
cd self-concreater
python -m venv .venv
# Windows (Git Bash):  source .venv/Scripts/activate
# macOS/Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 로컬 마켓플레이스로 플러그인 설치

Claude Code 안에서:

```
/plugin marketplace add <이 저장소의 절대 경로>
/plugin install yt-coach@nahon-local
```

설치 후 Claude Code를 재시작하고 `/help` 또는 `/`를 입력해 `yt-` 커맨드 10개가 보이는지 확인합니다.

## 스킬

| 스킬 | 하는 일 |
|---|---|
| `yt-analyze` | 영상 한 편의 섹션·훅·서사·기법 분석 규칙 (`/yt-add`가 사용) |
| `yt-diagnose` | 근접도 읽는 법, 갭 설명, 다음 행동 작성 규칙 (`/yt-diagnose`가 사용) |
| `yt-draft` | 대본(말·자막)과 샷 리스트(크기·앵글·길이) 작성 규칙 (`/yt-draft`가 사용) |
| `yt-polish` | 훅 공식 21가지(`hooks.json`, 출처 MIT)와 제목·썸네일 패키징 규칙. 제목 폭은 한글·한자·전각·이모지 2, 그 밖 1로 센 근사치 (`/yt-polish`가 사용) |
| `yt-conti` | 전체 대사, 컷별 대사 배분·자막·효과음·가이드 작성 규칙 (`/yt-conti`가 사용) |
| `yt-inspire` | 모티브 카드 4종 작성 규칙, 기법 병합 기준 (`/yt-inspire`가 사용) |
| `yt-mission` | 성장 단계, 미션 재료 우선순위·크기, 피드백, 주간 회고 규칙 (`/yt-mission`, `/yt-review`가 사용) |
| `yt-shots` | 프레임 샷 분류 기준(크기·앵글·얼굴·손만·B롤·자막) (`/yt-add --shots`가 사용) |
| `yt-typify` | 유형화 결과 해석: 축별 근거 문장, 아키타입 이름 (`/yt-typify`가 사용) |

## 작업 폴더

플러그인 저장소와 별도로, 레퍼런스가 쌓일 작업 폴더에서 Claude Code를 실행합니다.

```
<작업 폴더>/
├── profile.md
├── refs/ref_<category>/{index.md, techniques.md, videos/}
├── mine/  drafts/  missions/  reviews/
└── workspace/      # 원본 데이터 (git 제외)
```

스크립트는 `YT_COACH_HOME` 환경변수가 있으면 그 경로를, 없으면 현재 폴더를 작업 폴더로 씁니다.

## 스크립트 규약 (개발자용)

- 호출: 작업 폴더에서 `"${CLAUDE_PLUGIN_ROOT}/bin/ytc" <name> [args...]`
  (`bin/ytc`가 플러그인 `.venv`의 Python을 고르고, 현재 폴더를 `YT_COACH_HOME`으로 넘깁니다.
  Git Bash 없는 Windows에서는 `bin\ytc.cmd <name> [args...]`가 같은 일을 합니다)
- 성공: JSON 한 개를 stdout으로 출력하고 종료 코드 0
- 실패: `{"error": "<code>", "message": "..."}`를 stdout으로 출력하고 0이 아닌 종료 코드
- 자가 점검: `python -m scripts.common --selftest` → `{"ok": true}`

| 스크립트 | 하위 명령 |
|---|---|
| `refstore` | `scaffold`, `init <cat>`, `categories`, `count <cat>`, `update-stage <cat>` |
| `fetch` | `<url> [--category <cat>] [--role ref\|mine] [--force]` → `workspace/raw/{id}.json`. 요약에 `chars_per_sec`, `sparse`(말이 거의 없음) 포함 |
| `cuts` | `<url\|video_id> [--force]` → `workspace/cuts/{id}.json`, `workspace/frames/{id}/`. 360p 이하로 받아 장면 감지·카메라 추정 후 영상 즉시 삭제 |
| `videomd` | `context <id> --category <cat>` (분석 입력), `write <id> --category <cat> [--role] --analysis <json> [--force]` (검증·계산·저장) |
| `features` | (라이브러리) md 프런트매터 → 축별 피처 |
| `cluster` | `run <cat>` (ref만 축별 군집·판정·아키타입·이상치 → `workspace/typify/<cat>.json`), `apply <cat> --notes <json>` (index.md 기록) |
| `motif` | `context <cat> [--topic]`, `add <cat> --cards <json>`, `star <cat> <id> [--off]`, `merge-techniques <cat> --plan <json>` |
| `draft` | `context <cat> <motif-id\|A1>`, `save <cat> --draft <json>` (검증·계획 컷·아키타입 비교 → `drafts/`) |
| `polish` | `context <draft.md>`, `evaluate <draft.md> --data <json>`, `apply <draft.md> --choice <json> --hash <sha>` (훅·제목·썸네일 한국어 점수·지적·레퍼런스 근거 → 고른 것을 기획안에 반영, 이전 값은 `## 패키징` 이력. 해시가 다르면 `draft_changed`) |
| `conti` | `context <draft.md>`, `save <draft.md> --conti <json> [--force]`, `render <conti.md>`, `update`, `scenes`, `cut-context` (검증·대사 길이 경고·참고 프레임 매칭 → `drafts/*-conti.md`·`.html`. 뒤 셋은 화면의 컷 단위 편집·장면 후보·대사 대안 입력) |
| `similarity` | `compare <cat> <target>`, `save <cat> <target> --notes <json>` (→ `mine/`), `intentional <cat> <name> <key> [--off]` |
| `mission` | `context`, `save --mission <json> [--replace]`, `submit <file> --difficulty --feedback`, `skip --reason`, `week [YYYY-Www]`, `review-save`, `profile-update` (모두 `--date`로 날짜 지정 가능) |
| `shots` | `pending <cat>`, `context <id> --category`, `apply <id> --category --labels <json>` (장면 길이 가중 분포 → md `shots`) |
| `report` | `<cat> [--date]` → `reports/<cat>-<date>.html` (축별 판정·아키타입 비교·컷 길이 분포·진단 추이, 외부 자원 없는 한 파일) |
| `stt` | `<video_id> [--force]` — 자막 없는 영상 음성 인식 (선택: `pip install faster-whisper`, 모델 `YT_STT_MODEL`, 기본 small). CPU에서 1분 쇼츠 약 30초(첫 실행은 모델 다운로드 ~460MB). 말이 없으면 `speech: false`, 빈 자막으로 기록돼 `sparse` 처리 |
| `status` | `[category]` — 카테고리별 진행 단계(레퍼런스 수·유형화·기획안·콘티·진단), 다음 할 일, 최근 결과물, 오늘 미션. 화면 홈과 같은 JSON |
| `claude_cli` | (라이브러리) 화면이 `claude -p`를 부르는 실행기. 구조화 JSON 출력, 도구 최소 허용(기본 없음, 프레임을 볼 때만 `Read`), 세션 미저장 |
| `ui` | `[--port N] [--no-open] [--detach] [--stop]` — 로컬 전용 화면 서버(127.0.0.1, 주소의 key로만 접근). 작업 폴더당 하나(`workspace/ui.lock`), 이미 떠 있으면 그 주소를 돌려줌. `--detach`는 분리 프로세스(로그 `workspace/ui.log`) |

`fetch` 실패 코드: `invalid_url`, `private`, `unavailable`, `age_restricted`, `blocked`, `proxy_failed`, `no_transcript`, `role_conflict`, `category_not_found`, `fetch_failed`, `transcript_failed`

## 컷 통계 보정 기록

2026-10-20, 먹방 쇼츠 3개·롱폼 1개 육안 대조:
- ContentDetector 기본값(27)은 같은 구도 점프컷·짧은 디졸브를 절반 넘게 놓침 (쇼츠 60초: 육안 약 30컷 vs 13컷)
- threshold 15, 최소 장면 12프레임으로 조정 → 32컷, 평균 1.87초 (육안 약 2초). 값은 `templates/thresholds.json`의 `cuts`
- 카메라 판정은 장면 안 모든 지점이 같을 때만 pan/zoom (다수결은 고정 카메라를 pan 29%로 오판). 참고값으로만 쓴다

## 테스트

```bash
pytest tests/
```

실제 링크 테스트용 목록은 `tests/fixtures/links.txt`에 둡니다.

## 매일 미션 자동 실행 (선택)

작업 폴더에서 매일 아침 미션을 만들어 두려면 OS 스케줄러로 헤드리스 실행을 걸 수 있습니다.

```bash
# macOS/Linux (crontab -e): 매일 오전 8시
0 8 * * * cd /path/to/작업폴더 && claude -p "/yt-coach:yt-mission today (자동 실행: 확인 질문 없이 저장)" --allowedTools "Bash,Read,Write"

# Windows (작업 스케줄러에 등록할 명령)
cmd /c "cd /d C:\path\to\작업폴더 && claude -p \"/yt-coach:yt-mission today (자동 실행: 확인 질문 없이 저장)\" --allowedTools Bash,Read,Write"
```

## 운영 메모

- 자막 수집이 차단되면 `YT_PROXY` 환경변수로 프록시를 지정합니다 (예: `http://user:pass@host:port`).
- 차단 확인 기록:
  - 2026-10-20, 개발 PC(Windows, 가정용 회선): 프록시 없이 메타데이터·자막 수집 정상. 차단 없음.
  - 같은 날 15:40경: 약 10분 사이 자막 요청 30여 회 후 **자막 다운로드만 IP 차단**(`IpBlocked`, timedtext HTTP 429).
    메타데이터·자막 목록 조회는 계속 정상. `blocked` 에러와 프록시 안내가 실제로 나오는 것 확인.
    → 요청 사이 최소 간격(`YT_FETCH_INTERVAL`, 기본 3초)을 넣었습니다. 차단되면 시간을 두고 다시 시도하거나 `YT_PROXY`를 씁니다.
  - 클라우드·데이터센터 IP에서는 자막 요청이 처음부터 막히는 경우가 많습니다.
- 영상 원본은 360p 이하로 받아 프레임 추출 직후 삭제합니다. 개인 분석 용도로만 사용합니다.
