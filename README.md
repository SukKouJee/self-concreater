# yt-coach

직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 주고,
기획·진단·일일 미션으로 이어 주는 개인 크리에이터용 Claude Code 플러그인입니다.

- 설계: [`yt-coach-plugin-design.md`](yt-coach-plugin-design.md) (v2)
- 개발 계획: [`PLAN.md`](PLAN.md)

## 커맨드

플러그인으로 설치하면 `/yt-coach:<커맨드>` 형태로도 부를 수 있습니다.

| 커맨드 | 하는 일 | 상태 |
|---|---|---|
| `/yt-init` | 작업 폴더·템플릿 생성, 인터뷰로 `profile.md` 작성, 첫 카테고리 생성 | 미구현 |
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

- 호출: 작업 폴더에서 `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m scripts.<name> ...`
- 성공: JSON 한 개를 stdout으로 출력하고 종료 코드 0
- 실패: `{"error": "<code>", "message": "..."}`를 stdout으로 출력하고 0이 아닌 종료 코드
- 자가 점검: `python -m scripts.common --selftest` → `{"ok": true}`

## 테스트

```bash
pytest tests/
```

실제 링크 테스트용 목록은 `tests/fixtures/links.txt`에 둡니다.

## 운영 메모

- 자막 수집이 차단되면 `YT_PROXY` 환경변수로 프록시를 지정합니다. (Phase 2에서 차단 여부 확인 후 결과 기록 예정)
- 영상 원본은 360p 이하로 받아 프레임 추출 직후 삭제합니다. 개인 분석 용도로만 사용합니다.
