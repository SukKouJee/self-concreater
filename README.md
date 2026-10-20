# yt-coach

직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 주고,
기획·진단·일일 미션으로 이어 주는 개인 크리에이터용 Claude Code 플러그인입니다.

- 설계: [`yt-coach-plugin-design.md`](yt-coach-plugin-design.md) (v2)
- 개발 계획: [`PLAN.md`](PLAN.md)

## 커맨드

플러그인으로 설치하면 `/yt-coach:<커맨드>` 형태로도 부를 수 있습니다.

| 커맨드 | 하는 일 | 상태 |
|---|---|---|
| `/yt-init [category]` | 작업 폴더·템플릿 생성, 인터뷰로 `profile.md` 작성, 첫 카테고리 생성 | ✅ |
| `/yt-add <category> [ref\|mine] <links...> [--force]` | 링크별 자막·컷 통계 수집 → 분석 → 확인 후 영상별 md 생성, 기법 카탈로그 갱신 | ✅ |
| `/yt-typify <category>` | ref 영상으로 축별 유형화 판정·아키타입·이상치 → index.md | ✅ |
| `/yt-inspire <category> [주제]` | 모티브 카드(흐름·기법·각도·조합) 생성·누적, `star <id>`로 ★ 표시, 기법 카탈로그 중복 병합 | ✅ |
| `/yt-draft <category> <motif-id\|archetype> "<주제>"` | 섹션별 대본 + 샷 리스트 → `drafts/`, 아키타입 비율 비교 | ✅ |
| `/yt-diagnose <category> <link\|draft\|대본>` | 내 콘텐츠를 아키타입과 축별 비교(근접도·갭·다음 행동) → `mine/`, `intentional`로 의도한 차이 제외 | ✅ |
| `/yt-mission [today\|submit <file>\|skip\|swap]` | 하루 15~30분 미션, 제출 피드백·난이도 기록, 난이도에 따라 크기 조정 | ✅ |
| `/yt-review [YYYY-Www]` | 주간 회고 → `reviews/`, 승인 시 profile 갱신 | ✅ |

## 설치

### 1. 준비물

- Python 3.11+
- ffmpeg (PATH에 있어야 함)
- Claude Code (로컬 실행. YouTube 접근이 필요합니다)

### 2. Python 의존성

```bash
cd <이 저장소>
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

설치 후 Claude Code를 재시작하고 `/help` 또는 `/`를 입력해 `yt-` 커맨드 8개가 보이는지 확인합니다.

## 스킬

| 스킬 | 하는 일 |
|---|---|
| `yt-analyze` | 영상 한 편의 섹션·훅·서사·기법 분석 규칙 (`/yt-add`가 사용) |
| `yt-diagnose` | 근접도 읽는 법, 갭 설명, 다음 행동 작성 규칙 (`/yt-diagnose`가 사용) |
| `yt-draft` | 대본(말·자막)과 샷 리스트(크기·앵글·길이) 작성 규칙 (`/yt-draft`가 사용) |
| `yt-inspire` | 모티브 카드 4종 작성 규칙, 기법 병합 기준 (`/yt-inspire`가 사용) |
| `yt-mission` | 성장 단계, 미션 재료 우선순위·크기, 피드백, 주간 회고 규칙 (`/yt-mission`, `/yt-review`가 사용) |
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
  (`bin/ytc`가 플러그인 `.venv`의 Python을 고르고, 현재 폴더를 `YT_COACH_HOME`으로 넘깁니다)
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
| `similarity` | `compare <cat> <target>`, `save <cat> <target> --notes <json>` (→ `mine/`), `intentional <cat> <name> <key> [--off]` |
| `mission` | `context`, `save --mission <json> [--replace]`, `submit <file> --difficulty --feedback`, `skip --reason`, `week [YYYY-Www]`, `review-save`, `profile-update` (모두 `--date`로 날짜 지정 가능) |
| `stt` | `<video_id> [--force]` — 자막 없는 영상 음성 인식 (선택: `pip install faster-whisper`, 모델 `YT_STT_MODEL`, 기본 small) |

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
cmd /c "cd /d C:\path	o\작업폴더 && claude -p \"/yt-coach:yt-mission today (자동 실행: 확인 질문 없이 저장)\" --allowedTools Bash,Read,Write"
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
