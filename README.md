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
| `/yt-add <category> [ref\|mine] <links...>` | 링크별 자막·컷 통계 수집 → 영상별 md 생성 | 미구현 |
| `/yt-typify <category>` | ref 영상으로 축별 유형화 판정·아키타입 | 미구현 |
| `/yt-inspire <category> [주제]` | 모티브 카드(흐름·기법·각도·조합) 생성 | 미구현 |
| `/yt-draft <category> <motif-id\|archetype> "<주제>"` | 섹션별 대본 + 샷 리스트 | 미구현 |
| `/yt-diagnose <category> <link\|draft\|대본>` | 내 콘텐츠를 아키타입과 축별 비교 | 미구현 |
| `/yt-mission [today\|submit <file>\|skip\|swap]` | 하루 15~30분 미션 | 미구현 |
| `/yt-review` | 주간 회고 → profile 갱신 제안 | 미구현 |

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
| `stt` | `<video_id> [--force]` — 자막 없는 영상 음성 인식 (선택: `pip install faster-whisper`, 모델 `YT_STT_MODEL`, 기본 small) |

`fetch` 실패 코드: `invalid_url`, `private`, `unavailable`, `age_restricted`, `blocked`, `proxy_failed`, `no_transcript`, `role_conflict`, `category_not_found`, `fetch_failed`, `transcript_failed`

## 테스트

```bash
pytest tests/
```

실제 링크 테스트용 목록은 `tests/fixtures/links.txt`에 둡니다.

## 운영 메모

- 자막 수집이 차단되면 `YT_PROXY` 환경변수로 프록시를 지정합니다 (예: `http://user:pass@host:port`).
- 차단 확인 기록:
  - 2026-10-20, 개발 PC(Windows, 가정용 회선): 프록시 없이 메타데이터·자막 수집 정상. 차단 없음.
  - 같은 날 15:40경: 약 10분 사이 자막 요청 30여 회 후 **자막 다운로드만 IP 차단**(`IpBlocked`, timedtext HTTP 429).
    메타데이터·자막 목록 조회는 계속 정상. `blocked` 에러와 프록시 안내가 실제로 나오는 것 확인.
    → 요청 사이 최소 간격(`YT_FETCH_INTERVAL`, 기본 3초)을 넣었습니다. 차단되면 시간을 두고 다시 시도하거나 `YT_PROXY`를 씁니다.
  - 클라우드·데이터센터 IP에서는 자막 요청이 처음부터 막히는 경우가 많습니다.
- 영상 원본은 360p 이하로 받아 프레임 추출 직후 삭제합니다. 개인 분석 용도로만 사용합니다.
