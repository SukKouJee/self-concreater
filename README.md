# yt-coach

직접 모은 유튜브 레퍼런스를 분석해 **무엇을 만들지, 어떤 흐름·촬영으로 갈지에 대한 모티브**를 주고,
기획·진단·일일 미션으로 이어 주는 개인 크리에이터용 Claude Code 플러그인입니다.

- 저장소: https://github.com/SukKouJee/self-concreater (비공개)
- 설계: [`yt-coach-plugin-design.md`](yt-coach-plugin-design.md) (v2.2)
- 로컬 UI 설계: [`docs/superpowers/specs/2026-10-23-local-ui-design.md`](docs/superpowers/specs/2026-10-23-local-ui-design.md)
- 개발 계획: [`PLAN.md`](PLAN.md)
- 서버판 설계: [`docs/superpowers/specs/2026-11-01-server-edition-design.md`](docs/superpowers/specs/2026-11-01-server-edition-design.md)

## 커맨드

플러그인으로 설치하면 `/yt-coach:<커맨드>` 형태로도 부를 수 있습니다.

| 커맨드 | 하는 일 | 상태 |
|---|---|---|
| `/yt-init [category]` | 작업 폴더·템플릿 생성, 인터뷰로 `profile.md` 작성, 첫 카테고리 생성 | ✅ |
| `/yt-add <category> [ref\|mine] <links...> [--force] [--shots]` | 링크별 자막·컷 통계 수집 → 분석 → 확인 후 영상별 md 생성, 기법 카탈로그 갱신. `--shots-only`로 기존 영상 샷 분류 보강 | ✅ |
| `/yt-typify <category>` | ref 영상으로 축별 유형화 판정·아키타입·이상치 → index.md | ✅ |
| `/yt-inspire <category> [주제]` | 모티브 카드(흐름·기법·각도·조합) 생성·누적, `star <id>`로 ★ 표시, 기법 카탈로그 중복 병합, 지인 기법을 재료로 고르기(`--library`) | ✅ |
| `/yt-draft <category> <motif-id\|archetype> "<주제>"` | 섹션별 대본 + 샷 리스트 → `drafts/`, 아키타입 비율 비교, 지인 기획안 틀(`library:<id>`)로 시작 | ✅ |
| `/yt-polish <draft\|latest>` | 저장된 기획안의 훅 5개·제목 10개·썸네일 지시서를 만들고, 한국어 기준 점수·지적과 내 레퍼런스 근거를 붙여 보여 준다. 고른 것만 기획안에 반영(제목·훅 대본·훅 유형 교체, `## 패키징`에 썸네일 지시서와 이전 값 이력) | ✅ |
| `/yt-conti <draft\|latest\|-conti.md>` | 기획안 → 전체 대사 + 컷별 대사·자막·효과음·가이드 콘티(`-conti.md`), 샷 도식 + 레퍼런스 참고 프레임 HTML 스토리보드. `-conti.md`를 고치면 다시 그리기, 반복 컷은 장면으로 합치기(`ytc conti merge`). 화면에서는 장면 그림(Claude가 그린 일러스트, 설명을 고치면 자동으로 다시 그림) | ✅ |
| `/yt-diagnose <category> <link\|draft\|대본>` | 내 콘텐츠를 아키타입과 축별 비교(근접도·갭·다음 행동) → `mine/`, `intentional`로 의도한 차이 제외 | ✅ |
| `/yt-mission [today\|submit <file>\|skip\|swap]` | 하루 15~30분 미션, 제출 피드백·난이도 기록, 난이도에 따라 크기 조정 | ✅ |
| `/yt-review [YYYY-Www]` | 주간 회고 → `reviews/`, 카테고리 HTML 리포트 링크, 승인 시 profile 갱신 | ✅ |
| `/yt-ui [stop]` | 작업 폴더를 브라우저 화면(로컬 전용)으로 연다. Claude Code를 닫아도 화면은 남고, `stop`으로 끈다 | ✅ |
| `/yt-library [connect <주소>\|pull\|search <검색어>\|share technique <cat> <종류> <이름>\|share draft <파일>]` | 지인과 쓰는 비공개 git 저장소(`workspace/library/`)에서 훅 기법·기획안 틀을 받고 찾고, 확인 뒤에만 올린다(대본 원문·내 메모·준비물 제외, force push 없음) | ✅ |

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
- 상세 화면(레퍼런스·기획안)의 md 본문은 마크다운(제목·목록·표·인용·코드)으로 보입니다. 파일은 그대로이고 화면 표시만 바뀝니다.
- 가이드: 숏폼·롱폼 만들기 흐름을 단계별로 보여주고(`#/guide/shorts`, `#/guide/long`), 단계 링크로 해당 화면에 이동하며 완료한 단계에 배지가 붙습니다.
- 도움말 패널: 화면 오른쪽 패널의 [이 화면] 탭은 화면 설명, [챗봇] 탭은 내 상황 기준으로 답합니다. 챗봇은 개수와 상태만 보고 대본 본문·`## 내 메모`·작업 폴더 경로는 보지 않으며, 다른 화면과 같은 LLM 엔진 설정을 씁니다.
- 끄기: 화면 위쪽 "서버 끄기" 또는 `ytc ui --stop`. 진행 중인 작업이 있으면 경고합니다.

| 묶음 | 메뉴 | 상태 |
|---|---|---|
| 홈 | 카테고리별 진행 단계, 다음 할 일, 최근 결과물 | 1차 ✅ |
| 모으기 | 레퍼런스 (`/yt-add` 전체를 한 화면에서: 링크 목록에 줄마다 상태(새 영상·이미 있음 등), 분석은 확인을 기다리지 않고 다음 영상으로 넘어가며 끝난 영상은 "확인 대기" 카드로 쌓여 "모두 저장"/개별 저장, 섹션 라벨 수정, 다시 분석. 영상마다 단계별 진행률·남은 시간·끝날 시각을 보여 주며, 이 PC에서 걸린 시간(`workspace/timings.json`, 길이·횟수만 기록)이 쌓일수록 예상이 정확해짐) | 1차 ✅ |
| 모으기 | 레퍼런스 목록(같은 화면 아래쪽: 영상 카드, 섹션 흐름 띠, 상세에서 프레임·본문 확인, 섹션 라벨·훅/서사 유형 고치기. 고친 영상은 "직접 고침" 표시, 본문 표는 분석 당시 기록 그대로) | 2차(보기·라벨 고치기) ✅ |
| 모으기 | 공용 라이브러리 (`#/library`. 연결 전에는 저장소 주소·표시 이름으로 연결, 연결 뒤에는 "받기"(마지막 받은 시각)와 탭 "기법"/"기획안 틀"(카테고리·작성자·잘 먹힘 거르기, 작성자·메모·"내가 올림" 표시)을 봄. 기법 카드를 체크해 "모티브 재료로 쓰기"(모티브 화면의 "지인 기법" 칸으로 전달), 틀 카드의 "이 틀로 기획안 만들기"(기획안 화면 "지인 틀"). "공유하기" 탭: 내 기법/기획안 고르기 → 미리보기(기획안은 "섹션 요약 받기"로 Claude 1회, 고치기) → 메모·잘 먹힘 → "올리기"와 인라인 확인 뒤에만 올림. 이미 올린 항목은 "그래도 새로 올리기"로 따로 확인. 지인이 쓴 글은 마크다운 표시일 뿐 실행되지 않음) | 2차 ✅ |
| 분석 | 유형화 (ref 수·단계, 축별 판정, 아키타입 카드, 이상치. 실행 → 미리보기에서 근거·이름 고치기 → 저장) | 2차 ✅ |
| 분석 | 리포트 (탭 2개. "카테고리 리포트": "리포트 만들기"(Claude 없음)로 `reports/<카테고리>-<날짜>.html`을 만들고 목록에서 골라 화면 안(스크립트 실행 차단 프레임)이나 새 탭에서 보기. "주간 회고": 주를 고르면 미션 완료율·종류·난이도·건너뛴 사유·새 모티브 카드·진단 갭이 바로 보이고(Claude 없음), "회고 쓰기"로 요약·반복된 약점·다음 주 방향·프로필 변경 제안을 받아 고쳐 "저장" → `reviews/<주>.md`(`## 내 메모` 유지). 저장한 회고에서 제안마다 체크해 "프로필에 반영"(지금/바꿀 값 나란히, 체크한 섹션만)) | 2차 ✅ |
| 만들기 | 콘티 (컷별 레퍼런스 장면·대사 대안을 클릭으로 고르기, CapCut용 자막 SRT 받기, 컷 추가·복제·삭제·순서 바꾸기와 버전 이력·되돌리기, 반복 컷을 장면으로 합치기(미리보기 → 저장, "합치기 전" 이력으로 되돌리기: `/api/conti/merge/*`), 장면에 맞는 레퍼런스 사진 고르기(`POST /api/conti/refs/pick`)와 레퍼런스 프레임 한 줄 설명 만들기(`POST /api/refs/describe`), 편집본 영상의 컷에 자막 시간 맞추기: `POST /api/conti/srt-fit`, 움직이는 자막 영상(초록 배경 MP4) 만들기: `POST /api/conti/caption-video`, 촬영 영상 연결·컷 맞추기: 날짜별 촬영 폴더를 이어 컷마다 찍었는지 판정하고 남은 컷(폰용 HTML)·위치 CSV·컷 클립 만들기: `/api/footage/*`) | 1차 ✅ |
| 만들기 | 모티브 (새 카드 뽑기 → 미리보기에서 고쳐 고른 카드만 저장, 저장한 카드 거르기·★·"기획안 만들기"로 그 카드를 출처로 기획안 화면 열기. 기법 카탈로그 병합은 터미널 `/yt-coach:yt-inspire`) | 2차 ✅ |
| 만들기 | 기획안 (저장한 기획안 목록·상세 읽기 전용, 출처 고르기 → 만들기 → 미리보기에서 제목·대본·샷 표·준비물 고쳐 저장, 콘티 화면으로 연결, 상세에서 "훅·제목·썸네일 다듬기": 훅 5·제목 10·썸네일 후보를 점수·지적·근거와 보고 고쳐 저장) | 2차 ✅ |
| 성장 | 진단 (내 영상·기획안 목록 → "비교하기"로 가장 가까운 아키타입·축별 근접도·갭을 바로 보기(Claude 없음) → "설명 받기"로 요약·갭 설명·다음 행동을 받아 고쳐 저장 → `mine/<이름>.md`(`## 내 메모` 유지). 저장한 진단에서 갭마다 "일부러 다르게 한 거예요" 표시. 대본 붙여넣기·링크 바로 진단은 터미널 `/yt-coach:yt-diagnose`) | 2차 ✅ |
| 성장 | 미션 (오늘 미션 받기 → 미리보기에서 "이걸로 할게요"로 저장·"다른 걸로" 다시 뽑기. 저장된 미션은 "완료 제출"(한 일·난이도·선택 파일 경로 → "피드백 받기" → 고쳐 저장), "건너뛰기"(사유 선택), "바꾸기"(교체 미리보기). 지난 14일 기록·이번 주 완료율. 제출 파일은 작업 폴더에 잠깐 복사해 읽고 바로 지움) | 2차 ✅ |

### LLM 엔진 (선택)

화면(`ytc ui`)이 Claude를 부르는 모든 곳은 엔진 계층 하나를 지납니다. 기본은 `claude-cli`(설치한 `claude`, 지금과 같은 동작)이고, 작업 폴더 루트의 `yt-coach.json`으로 Anthropic API로 바꿀 수 있습니다.

```json
{ "llm": { "engine": "anthropic-api", "model": "claude-sonnet-5-5", "max_tokens": 8000 } }
```

- `engine`: `claude-cli`(기본) 또는 `anthropic-api`. 환경 변수 `YTC_LLM_ENGINE`이 있으면 그것이 우선합니다. `model`·`max_tokens`는 생략 가능합니다.
- `anthropic-api`는 환경 변수 `ANTHROPIC_API_KEY`가 필요합니다. 키는 파일·로그에 남기지 않습니다.
- 오류 코드: `llm_not_configured`(설정·키 없음), `llm_auth`(키 거부), `llm_failed`(호출 실패). `claude-cli`는 기존 `claude_missing`·`claude_login`을 그대로 씁니다.
- 터미널의 `/yt-coach:*` 커맨드는 Claude Code가 직접 해석하므로 이 설정과 무관합니다.

## 두 가지 판

같은 코어를 세 가지 방법으로 쓸 수 있습니다. 작업 폴더 형식은 모두 같습니다.

| 판 | 대상 | 실행 |
|---|---|---|
| 플러그인(기본) | 내 PC의 Claude Code | 아래 "설치". 이 문서의 나머지는 이 판 기준이고 바뀐 것이 없습니다 |
| 개인 컨테이너 | 한 사람, 내 PC에서 Docker로 | `deploy/local` |
| 서버 | 지인 몇 명이 각자 로그인해서 쓰는 한 대의 서버 | `deploy/server` |

컨테이너 두 판은 Claude를 `ANTHROPIC_API_KEY`로 부르고(구독 로그인이 아닙니다), Docker와 Compose가 필요합니다.

### 개인 컨테이너 (`deploy/local`)

```bash
cd deploy/local
cp .env.example .env        # ANTHROPIC_API_KEY 채우기
docker compose up -d --build
docker compose logs ytc     # 나온 http://127.0.0.1:8765/?key=... 주소를 브라우저로 연다
```

- 비워 두면 `deploy/local/work/`에 새 작업 폴더를 만듭니다. 쓰던 폴더를 쓰려면 `.env`에 `YTC_WORK_DIR=C:/내/작업폴더`(슬래시)를 적습니다.
- 주의: 그 폴더는 **읽기/쓰기**로 붙습니다(컨테이너가 `workspace/`와 `ui.lock`을 씁니다). 같은 폴더를 내 PC의 `ytc ui`와 동시에 열지 말고, 먼저 `ytc ui --stop` 하세요.
- 꺼져 있는 기능(화면에서 숨김, API는 403): 공용 라이브러리, 자막 영상 만들기. 촬영 영상 처리는 compose의 `/footage:ro` 줄을 풀어 쓸 수 있습니다.
- 화면은 `127.0.0.1`에만 열립니다. 끄기: `docker compose down`.

### 서버 (`deploy/server`)

PostgreSQL + MinIO + 앱 + 마이그레이션이 한 compose로 뜹니다. 초대한 사용자만 들어옵니다. 로그인은 **이메일+비밀번호**(관리자가 정함, `YTC_PASSWORD_LOGIN=1`, `.env.example` 기본값)와 **Google 로그인**(선택) 중 하나 이상을 씁니다. Google OAuth 없이도 됩니다.

1. `cd deploy/server && cp .env.example .env` 후 필수 값을 채웁니다: `YTC_PUBLIC_URL`(`https://도메인`), `YTC_SESSION_SECRET`, DB 비밀번호 둘, MinIO 키 둘, `ANTHROPIC_API_KEY`. 비밀번호는 `python -c "import secrets;print(secrets.token_urlsafe(48))"`로 만들고 `$`는 넣지 않습니다.
2. (선택) Google 로그인도 쓰려면 Google Cloud Console에서 OAuth 클라이언트를 만듭니다. 비밀번호 로그인만 쓰면 건너뜁니다.
   - 동의 화면: 사용자 유형 **외부**, 범위는 `openid email profile`만. 게시 상태가 "테스트"면 테스트 사용자로 넣은 계정만 로그인됩니다.
   - 사용자 인증 정보 > OAuth 클라이언트 ID > 유형 **웹 애플리케이션**.
   - 승인된 리디렉션 URI: `https://<도메인>/auth/google/callback` (`YTC_PUBLIC_URL`과 글자 그대로 같고 끝에 `/` 없음).
   - 클라이언트 ID·보안 비밀을 `.env`의 `YTC_GOOGLE_CLIENT_ID`, `YTC_GOOGLE_CLIENT_SECRET`에 넣습니다.
3. `docker compose up -d --build` — DB 마이그레이션(`migrate`)과 버킷 생성이 먼저 돌고 앱이 뜹니다.
4. HTTPS: 도메인이 이 서버를 가리키면 `docker compose --profile tls up -d`(caddy, 자동 인증서, `YTC_DOMAIN` 필요). 앱은 `127.0.0.1:8080`에만 열리므로 Tailscale serve 같은 사설망 프록시를 앞에 둬도 됩니다. production은 https 주소가 아니면 시작하지 않습니다.
5. 사용자 초대(초대한 이메일만 로그인됩니다). `--password`를 주면 비밀번호를 터미널에서 두 번 묻습니다(화면에 보이지 않음, 10자 이상):

```bash
docker compose --profile admin run --rm admin add-user you@example.com --admin --password
docker compose --profile admin run --rm admin add-user friend@example.com --daily-limit 0.5 --password
# 스크립트에서는 표준 입력으로(셸 기록에 남지 않게 파일·비밀 관리 도구에서 읽어 넘깁니다)
docker compose --profile admin run --rm -T admin set-password friend@example.com --password-stdin < friend-password.txt
```

| 관리 명령 (`python -m server.admin`) | 하는 일 |
|---|---|
| `add-user <이메일> [--name N] [--daily-limit USD] [--admin] [--password \| --password-stdin]` | 초대(이미 있으면 `user_exists` 오류). `--password`면 비밀번호를 묻고, `--password-stdin`이면 표준 입력 한 줄에서 읽음 |
| `set-password <이메일> [--password-stdin]` | 비밀번호를 정하거나 바꿈(잊어버렸을 때 재설정). 그 사용자의 모든 기기 세션도 끊김 |
| `clear-password <이메일>` | 비밀번호를 지워 비밀번호 로그인을 막음(Google 로그인은 그대로). 세션도 끊김 |
| `disable-user <이메일>` | 비활성. 로그인 중이던 세션도 끊김 |
| `logout-all <이메일>` | 계정은 그대로 두고 모든 기기의 세션을 끊음(기기 분실 등). 다시 로그인하면 됨 |
| `make-admin <이메일>` / `revoke-admin <이메일>` | 관리자 권한을 주거나 걷음(첫 관리자 만들기, 관리 화면이 잠겼을 때). 감사 기록에 "운영자(명령줄)"로 남음 |
| `import <폴더> --user <이메일> [--dry-run] [--overwrite] [--overwrite-memo]` | 내 PC 작업 폴더를 그 사용자 계정으로 올림. 다시 실행해도 안전하고, 서버에서 고친 `## 내 메모`는 `--overwrite-memo`가 없으면 지키며 올린 뒤 목록을 비교해 확인 |
| `usage [--days N]` | 사용자별 LLM 사용량 집계 |

```bash
docker compose --profile admin run --rm -v C:/내/작업폴더:/import:ro admin import /import --user you@gmail.com --dry-run
```

- LLM 비용: 운영자 키 하나를 같이 씁니다. 사용자당 하루(한국 시간) 한도 `YTC_DAILY_USD`(기본 1.00), 넘으면 안내 문구와 함께 막힙니다. 서버 전체 동시 작업은 영상 `YTC_MEDIA_SLOTS`(2)·Claude `YTC_LLM_SLOTS`(4)입니다.
- 서버 v1에서 꺼진 기능: 공용 라이브러리, 자막 영상 만들기, 촬영 영상 처리, 자막 시간 맞추기(srtfit). 촬영 영상은 내 PC 판에서 처리한 뒤 `import`로 옮깁니다.
- 비밀번호 로그인은 이메일별 10분에 5번, 접속 IP별 10분에 20번 틀리면 잠시 막힙니다(429, 몇 분 뒤 다시). 맞히면 그 이메일의 횟수는 초기화됩니다. 사용자가 스스로 비밀번호를 바꾸는 화면은 없고 관리자가 `set-password`로 바꿉니다.
- 화면의 로그아웃은 이 브라우저의 쿠키만 지웁니다. 다른 기기까지 모두 로그아웃하려면(기기 분실 등) 관리자가 관리 화면이나 `logout-all <이메일>`로 끊습니다.
- **관리 화면**(`#/admin`, 관리자에게만 머리글의 "관리"와 도움말 메뉴에 보임): 사용자 목록(상태, 로그인 방법, 오늘 사용액/한도), 초대(첫 비밀번호는 선택 — 비우면 Google 로그인만), 사람별 비밀번호 재설정·지우기, 모든 기기 로그아웃, 비활성/다시 쓰기, 하루 한도, 관리자 지정·해제, 최근 7일 사용량, 최근 관리 기록. 첫 관리자는 `add-user --admin` 또는 `make-admin`으로 만들고, 그다음부터는 화면에서 합니다. 자기 계정은 비활성·강등할 수 없고 마지막 관리자도 뺄 수 없습니다. 앱은 owner DB 자격 없이 DB의 관리 함수(관리자인지 DB가 다시 확인)로만 사용자를 바꿉니다.
- 앱이 다시 시작되면 진행 중이던 작업은 멈춥니다(저장된 문서는 그대로).
- 유튜브가 서버 IP를 막으면 `.env`의 `YT_PROXY`를 채웁니다.
- 참고: MinIO 공식 이미지가 중단돼 Chainguard 빌드(`cgr.dev/chainguard/minio`)를 다이제스트로 고정해 씁니다. 갱신 방법은 서버 설계 §9.3.
- 아직 없는 것: 백업·정합성 점검·휴지통 만료(S10). 운영 전에 `pgdata`, `minio` 볼륨을 직접 백업하세요.
- 설계: [`docs/superpowers/specs/2026-11-01-server-edition-design.md`](docs/superpowers/specs/2026-11-01-server-edition-design.md)

## 설치

### 1. 준비물

- Python 3.11+
- (선택) ffmpeg — 지금은 필요 없습니다. 컷 분석은 OpenCV, STT·촬영 영상 디코딩은 PyAV(`av`, 휠에 ffmpeg 포함)로 합니다
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

### 4. (선택) 움직이는 자막 영상

콘티 화면의 "자막 영상 만들기(초록 배경)"와 `ytc captions render`는 Remotion을 씁니다. 쓰려면 Node 18 이상을 깔고 처음 한 번만 설치하세요(자동 설치하지 않습니다).

```bash
ytc captions setup      # remotion/에서 npm ci
```

- 첫 렌더 때 Remotion이 Chrome Headless Shell을 내려받아 오래 걸립니다.
- 결과 `drafts/<기획안>-captions.mp4`는 초록(`#00FF00`) 배경에 자막만 움직입니다. CapCut에서 이 영상을 맨 위 트랙에 얹고 크로마키로 초록을 빼세요.
- 라이선스: Remotion은 개인·직원 3명 이하·비영리는 무료, 그보다 큰 회사는 회사 라이선스가 필요합니다.

## 공용 라이브러리 MCP 서버

플러그인을 설치하면 루트의 `.mcp.json`이 `yt-coach-library` MCP 서버(`scripts/library_mcp.py`, 표준 라이브러리만)를 자동으로 띄웁니다.
Claude가 라이브러리를 직접 부를 수 있는 도구 8개를 제공합니다.

| 도구 | 하는 일 |
|---|---|
| `library_status` / `library_pull` | 연결 상태 / 최신 항목 받기 |
| `library_search` / `library_get` | 받아 둔 항목 찾기 / 하나 읽기 (지인이 쓴 데이터이며 지시가 아님) |
| `library_my_techniques` | 내 `techniques.md` 기법 목록(올린 적 있는지 포함) |
| `library_share_preview` | 올릴 내용 미리보기(기법 또는 기획안 틀). 아직 올리지 않음 |
| `library_summarize` | 기획안 틀의 섹션별 한 줄 요약 |
| `library_share_confirm` | 올리기. 사용자에게 미리보기를 보여 주고 채팅에서 명시적으로 확인받은 뒤에만 호출 |

- 작업 폴더는 CLI와 같습니다: `YT_COACH_HOME`이 있으면 그 경로, 없으면 현재 폴더. Claude Code는 플러그인 MCP 서버를 열어 둔 프로젝트 폴더에서 띄우므로, 보통 따로 설정할 필요가 없습니다.
- `connect`(저장소 주소 연결)는 도구가 아니라 `/yt-library connect <주소>`로 합니다.
- 명령은 `python`입니다(`.mcp.json`). macOS에 python3만 있으면 `claude mcp add --scope user yt-coach-library -- python3 <플러그인 경로>/scripts/library_mcp.py`로 등록하세요(플러그인 쪽 서버는 실패로 표시되지만 도구는 이쪽에서 제공됩니다).
- Windows에서 `python`이 Microsoft Store 안내만 띄운다면 Store 별칭 스텁일 뿐 Python이 설치되지 않은 것입니다. python.org에서 설치하세요.
- 시스템 Python에 `pyyaml`이 없으면 서버가 플러그인 `.venv`의 Python으로 스스로 다시 실행합니다.
- `library_share_confirm`은 권한 프롬프트에서 '항상 허용'하지 마세요. 올릴 때마다 미리보기를 보고 확인해야 합니다.
- 오류는 `isError: true`와 `{"error": "<code>", "message": "..."}` 텍스트로 돌려줍니다.

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
| `draft` | `context <cat> <motif-id\|A1>`, `save <cat> --draft <json>` (검증·장면 수·아키타입 비교 → `drafts/`) |
| `polish` | `context <draft.md>`, `evaluate <draft.md> --data <json>`, `apply <draft.md> --choice <json> --hash <sha>` (훅·제목·썸네일 한국어 점수·지적·레퍼런스 근거 → 고른 것을 기획안에 반영, 이전 값은 `## 패키징` 이력. 해시가 다르면 `draft_changed`) |
| `conti` | `context <draft.md>`, `save <draft.md> --conti <json> [--force]`, `render <conti.md>`, `srt <conti.md> [--track caption\|line] [--video <편집본>]`(CapCut용 `.srt`. `--video`면 자막 없는 편집본의 실제 컷에 시간을 맞춤), `update`, `scenes`, `cut-context`, `history`, `history-name`, `history-restore`, `history-diff`, `merge <conti.md> [--cuts <json> --hash <sha>]`(반복 컷을 장면으로 합침: 인자 없이 부르면 계획 미리보기 JSON, `--cuts`·`--hash`를 주면 저장하고 이력에 "합치기 전"을 남김. 장면별 레퍼런스 사진 `ref_auto`는 화면의 "사진 다시 고르기"로만 고르며 CLI 명령은 없음) (검증·대사 길이 경고·참고 프레임 매칭 → `drafts/*-conti.md`·`.html`. update·scenes·cut-context는 화면의 컷 단위 편집·장면 후보·대사 대안 입력) |
| `captions` | `setup`(처음 한 번, `remotion/`에서 `npm ci`), `check`, `render <conti.md> [--video <편집본>] [--track caption\|line]` — 콘티 자막을 초록 배경 움직이는 자막 MP4(`drafts/<기획안>-captions.mp4`)로 렌더(선택 설치, Remotion). 취소·시간 초과 시 그 하위 프로세스만 종료 |
| `footage` | `scan <folder>`, `process <file> [--no-stt]` — 촬영 영상 한 파일을 장면 나누기·사진 모음·받아쓰기(`workspace/footage/<지문>/`). 컷 맞추기는 화면에서(`scripts/footage_match.py`, Claude 호출) |
| `footage_out` | `remaining <conti.md>`, `csv <conti.md>`, `clips <conti.md>` — 남은 컷 HTML(`drafts/<기획안>-remaining.html`), 위치 CSV(`-footage.csv`), 컷 클립(`-cuts/NN_<원본 이름>.mp4`) |
| `similarity` | `compare <cat> <target>`, `save <cat> <target> --notes <json>` (→ `mine/`), `intentional <cat> <name> <key> [--off]` |
| `mission` | `context`, `save --mission <json> [--replace]`, `submit <file> --difficulty --feedback`, `skip --reason`, `week [YYYY-Www]`, `review-save`, `profile-update` (모두 `--date`로 날짜 지정 가능) |
| `shots` | `pending <cat>`, `context <id> --category`, `apply <id> --category --labels <json>` (장면 길이 가중 분포 → md `shots`), `describe-context <id> --category`, `apply-desc <id> --category --labels <json>`, `desc-status <cat>` (저장 프레임마다 한 줄 설명 `desc` → `workspace/shots/<id>.json`, md는 건드리지 않음) |
| `report` | `<cat> [--date]` → `reports/<cat>-<date>.html` (축별 판정·아키타입 비교·컷 길이 분포·진단 추이, 외부 자원 없는 한 파일) |
| `stt` | `<video_id> [--force]` — 자막 없는 영상 음성 인식 (선택: `pip install faster-whisper`, 모델 `YT_STT_MODEL`, 기본 small). CPU에서 1분 쇼츠 약 30초(첫 실행은 모델 다운로드 ~460MB). 말이 없으면 `speech: false`, 빈 자막으로 기록돼 `sparse` 처리 |
| `status` | `[category]` — 카테고리별 진행 단계(레퍼런스 수·유형화·기획안·콘티·진단), 다음 할 일, 최근 결과물, 오늘 미션. 화면 홈과 같은 JSON |
| `library_mcp` | (MCP 서버, `python scripts/library_mcp.py`) stdio JSON-RPC로 `library` 기능을 도구 8개로 노출. 위 "공용 라이브러리 MCP 서버" 참고 |
| `library` | `connect <주소> --author <이름>`, `status`, `pull`, `search [검색어] [--category --kind --author --worked]`, `get <id>`, `techniques <cat>`, `preview technique <cat> <종류> <이름> [--desc <설명>]`, `preview draft <파일>`, `summarize <preview_id>`, `confirm <preview_id> [--note --worked --edits <json> --force-new]` (공용 라이브러리 git 저장소 받기·찾기·올리기. git 프롬프트 없음·60초 제한·URL 자격 증명 가림, 미리보기는 `workspace/tmp/library_preview_<id>.json` 30분. 오류: `library_auth`, `library_remote`, `library_network`, `duplicate`, `rebase_conflict`, `preview_expired` 등) |
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

서버판 테스트(`tests/server`, `tests/contract`)는 Docker가 있을 때만 `YTC_TEST_DOCKER=1 pytest tests/server tests/contract`로 돌립니다(없으면 건너뜁니다).

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
