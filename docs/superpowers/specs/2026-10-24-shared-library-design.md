# 공용 라이브러리 설계 — 지인들과 훅 기법·기획안 틀 나누기

작성: 2026-10-24 · 대상: yt-coach 플러그인 · 상태: 사용자 승인(채팅) 후 builder check 대기

## 1. 왜 만드나

yt-coach는 지금 개인용이다. 사용자는 **같이 영상을 만드는 지인 몇 명**과 서로 찾은 것을 가져다 쓰고 싶다. 구체적으로 가져다 쓰고 싶었던 것은 두 가지다.

1. **지인이 찾은 훅 기법** — 각자의 기법 카탈로그(`refs/ref_<cat>/techniques.md`)에 쌓이는 것.
2. **잘 먹힌 기획안 틀** — 지인이 실제로 찍어 반응이 좋았던 기획안의 뼈대(섹션 구성·비율, 훅 방식, 샷 리듬).

지인들은 **모두 Claude Code에 yt-coach를 설치해 쓰고**, 이미 **GitHub를 함께 쓴다**. 처음 요청은 "사용자별 템플릿·기법을 중앙화해서 MCP 연동으로 처리"였다.

## 2. 목표 · 비목표 · 반목표

**목표**
- 지인들이 함께 쓰는 GitHub 비공개 저장소 하나를 "공용 라이브러리"로 두고, 각자 yt-coach가 거기서 **받고(pull)·올린다(push)**.
- 올리는 것은 사용자가 고른 **기법 1개** 또는 **기획안 1개를 틀로** — 한 줄 메모(왜 좋았는지)와 "잘 먹혔다" 표시를 붙일 수 있다.
- 받은 지인 기법·틀을 **모티브 뽑기·기획안 만들기의 재료**로 고를 수 있다.
- yt-coach 안의 작은 **MCP 서버**로 Claude가 대화 중에 라이브러리를 찾고(검색·보기), 사용자가 확인하면 공유한다.
- 로컬 UI에 "공용 라이브러리" 화면(받기·목록·공유하기).

**비목표(이번에 안 함)**
- 웹 서비스 호스팅, 가입·로그인·권한 관리 기능(권한은 GitHub 저장소 초대로).
- 성과(조회수) 자동 수집 — Analytics 연동은 보류 그대로. "잘 먹혔다"는 올리는 사람이 표시.
- 레퍼런스 영상 분석 원본·프레임·내 메모·진단·미션 공유.
- 지인 것을 내 기법 카탈로그(`techniques.md`)나 유형화 기준에 **섞기**.

**반목표(이렇게 되면 실패)**
- 사용자가 고르지 않은 것이 자동으로 올라감.
- 내 메모·대본 원문·개인 경로가 저장소에 들어감.
- 여럿이 동시에 올릴 때 서로의 것을 덮어씀(병합 충돌·force push).
- yt-coach가 GitHub 토큰·비밀번호를 저장하거나 다룸.
- 남의 기법이 내 레퍼런스 분석 결과처럼 보임(출처 흐림).

## 3. 제약

- CLAUDE.md 보류 항목 "호스팅·다중 사용자 웹 서비스"는 **그대로 둔다** — 이 설계는 서버를 띄우지 않고 git 저장소를 공유할 뿐이다. CLAUDE.md에 "공용 라이브러리(지인 GitHub 저장소 공유)는 허용"을 한 줄 더한다.
- git 인증은 각자의 기존 git 설정(자격 증명 관리자·SSH·`gh auth`)을 그대로 쓴다. yt-coach는 git 명령을 하위 프로세스로 실행만 한다.
- 올리기(commit+push)는 **사용자 확인 뒤에만**. MCP 도구도 같은 규칙(미리보기 → 사용자 확인 → 올리기).
- 계산·저장은 `scripts/`, 해석(어떤 기법이 맞는지 고르기 등)은 Claude — 기존 원칙 그대로.
- 기존 규칙: md 프런트매터가 계산 입력, `## 내 메모` 불변, AI 출력 확인 후 저장, 프로세스를 이미지 이름으로 끄지 않기.
- Python 3.11+, 새 의존성 최소. MCP 서버는 **Python 표준 입출력 MCP**로(공식 `mcp` Python SDK를 의존성으로 더할지, stdlib로 JSON-RPC를 직접 구현할지는 builder 판단 — 아래 7절).

## 4. 사용자가 정한 것 (채팅에서 승인)

- 대상: 같이 영상을 만드는 지인들(소수, 서로 아는 사이).
- 나누는 것: 훅 기법, 잘 먹힌 기획안 틀.
- 저장 위치: 지인들이 쓰는 GitHub 비공개 저장소(이미 GitHub 사용 중).
- 지인 모두 Claude Code + yt-coach 사용.
- 아래 5절의 구성 전체(저장소 1개 clone, 고른 것만 올림, 기획안은 뼈대만, 항목마다 파일, 받기는 pull, 지인 것은 따로 표시, MCP는 로컬 stdio, 공용 라이브러리 화면, 호스팅·가입·성과 수집 제외).

## 5. 구성

### 5.1 연결
- 설정 1회: 저장소 주소(예: `git@github.com:team/yt-library.git` 또는 https)를 받아 작업 폴더의 `workspace/library/`에 clone(작업 폴더 자체는 플러그인 저장소 밖의 사용자 데이터이고, `workspace/`는 원본·내려받은 데이터 자리라는 기존 규칙과 맞음). 설정은 작업 폴더 `library.json`에 `{"remote": "...", "author": "<표시 이름>"}`.
- 표시 이름(author)은 처음에 사용자에게 묻는다(기본값 `git config user.name`).
- 연결 해제·주소 변경은 설정 파일 수정 + 안내.

### 5.2 저장소 구조 (공용 라이브러리 저장소 안)
```
README.md                     # 라이브러리 설명(처음 연결 시 없으면 만들어 줌)
techniques/<category>/<id>.md # 기법 1개 = 파일 1개
templates/<category>/<id>.md  # 기획안 틀 1개 = 파일 1개
```
- `<id>` = `<author-slug>-<YYYYMMDD>-<짧은 해시>` → 여럿이 동시에 올려도 파일 이름이 겹치지 않음.
- 카테고리 이름은 각자 작업 폴더의 카테고리 이름을 그대로 쓴다(서로 다르면 다른 폴더 — 매핑은 비목표).

**기법 파일** (프런트매터 + 짧은 본문)
```yaml
kind: technique
id: ...
author: 표시 이름
category: mukbang
technique_kind: 훅          # 훅 | 전환 | 엔딩 | 촬영 (기존 TECHNIQUE_KINDS)
name: 결과 선공개
worked: true                # "잘 먹혔다" 표시(선택)
note: "첫 1초에 완성품 — 내 쇼츠 3편 다 이걸로 시작"   # 한 줄 메모(선택)
shared_at: 2026-10-24
schema_version: 1
```
본문: 기법 설명(카탈로그의 설명 그대로) + "출처: 레퍼런스 N개"(영상 id·링크는 넣지 않음 — 원본 공유 아님; 개수만).

**기획안 틀 파일**
```yaml
kind: template
id: ...
author: 표시 이름
category: mukbang
title: 편의점 라면 5종 몰아먹기형          # 기획안 제목(공유용으로 고칠 수 있음)
target_sec: 60
hook_type: result_first
narrative_type: list
sections: [{label: hook, ratio: 0.07}, {label: body, sub: story, ratio: 0.82}, ...]   # 비율만
pacing: {avg_cut_sec: 1.33}
shot_mix: {클로즈업: 0.4, 탑뷰: 0.3, ...}   # 샷 크기 비율
worked: true
note: "조회수 평소 2배"
shared_at: 2026-10-24
schema_version: 1
```
본문: 섹션별 "무엇을 하는 구간인지" 한 줄씩(대본 원문 아님 — 올리기 전 Claude가 요약을 제안하고 사용자가 고침), 섹션별 샷 리듬(예: "클로즈업 1.5초 × 24"). 내 메모·대본·준비물·개인 경로 없음.

### 5.3 올리기(공유하기)
1. 사용자가 고른다: 내 기법 카탈로그의 기법 1개, 또는 내 기획안 1개.
2. 미리보기를 만든다(스크립트): 위 파일 내용. 기획안 틀의 섹션 요약은 Claude가 제안(1회), 사용자가 고침.
3. 사용자가 메모·"잘 먹혔다"를 넣고 **확인**.
4. 스크립트: `git pull --rebase`(최신화) → 새 파일 추가 → `git commit`(메시지 `share(<category>): <name> by <author>`) → `git push`. push가 거부되면 pull --rebase 후 1회 재시도, 그래도 실패면 원인 메시지(인증 실패·권한 없음·네트워크)와 함께 중단 — **force push 절대 금지**.
5. 이미 같은 내용을 올렸으면(같은 author+name 또는 같은 기획안 출처) 경고하고 새 파일로 올릴지/취소할지 묻는다.

### 5.4 받기와 보기
- "받기" = `git pull --ff-only`(실패하면 원인 안내; 로컬 변경은 yt-coach가 만든 적 없으므로 있으면 경고만).
- 목록·검색(스크립트): 카테고리·종류·작성자·"잘 먹혔다" 필터, 이름·설명 검색. 내 것은 "내가 올림" 표시.
- 지인 것은 **내 `techniques.md`에 섞지 않는다**. 화면과 Claude 입력에서 "지인 라이브러리"로 따로, 작성자와 함께.

### 5.5 재료로 쓰기
- 모티브 뽑기(`motif.context`): 선택 입력 `library_techniques`(사용자가 고른 지인 기법들)를 payload에 더한다. 카드 `basis`/`evidence` 규칙: 지인 기법을 쓴 카드는 `basis`에 "지인 <author>의 <기법>"을 적고, `evidence`는 여전히 **내 ref 영상**이 근거(기존 검증 유지 — 지인 기법만으로 근거 없는 카드는 만들지 않음). → yt-inspire 규칙에 한 절 추가.
- 기획안 만들기(`draft.context`): 출처 종류에 "지인 기획안 틀"(`library:<id>`)을 더한다. 틀의 섹션 비율·훅·서사·샷 리듬을 뼈대로 쓰고, 기획안 프런트매터 `source: library:<id>`, 개요에 "틀: <author>의 <title>". → yt-draft 규칙에 한 절 추가.

### 5.6 MCP 서버
- yt-coach 플러그인이 MCP 서버를 선언(플러그인 `.mcp.json` 또는 `plugin.json`의 `mcpServers`, stdio, 명령은 `bin/ytc` 경유 `python -m scripts.library_mcp`). 작업 폴더는 기존처럼 `YT_COACH_HOME`/현재 폴더.
- 도구(읽기): `library_status`(연결·최근 받은 시각), `library_pull`, `library_search(query?, category?, kind?, author?, worked?)`, `library_get(id)`.
- 도구(쓰기): `library_share_preview(kind, source)` → 미리보기 반환(저장 안 함), `library_share_confirm(preview_id, note?, worked?, edits?)` → 사용자가 대화에서 확인한 뒤에만 Claude가 호출(도구 설명에 "사용자의 명시적 확인 후에만" 명시). 미리보기 id는 일정 시간 뒤 만료.
- MCP 도구가 실패하면 기존 스크립트 오류 형식(`{"error","message"}`)을 그대로.

### 5.7 화면 — "공용 라이브러리"
- 왼쪽 메뉴 새 묶음 또는 "모으기" 아래 항목(builder 판단, 메뉴 표 갱신).
- 연결 안 됨: 저장소 주소·표시 이름 입력 → 연결(clone). 실패 원인 안내(인증·권한·주소).
- 연결됨: "받기" 버튼 + 마지막 받은 시각, 탭 "기법"/"기획안 틀", 필터(카테고리·작성자·잘 먹힘), 카드(작성자·메모·잘 먹힘 배지·날짜), 틀 카드에 "이 틀로 기획안 만들기"(`#/draft/new/library:<id>`), 기법 카드에 "모티브 재료로 고르기"(모티브 화면으로 선택 상태 전달).
- "공유하기": 내 기법 목록 / 내 기획안 목록에서 고르기 → 미리보기(기획안이면 Claude 섹션 요약 1회) → 메모·잘 먹힘 → "올리기"(확인) → 결과(성공·push 실패 원인).
- 기존 화면 연결: 레퍼런스 상세의 기법·기획안 상세에 "공유하기" 버튼(선택).

### 5.8 터미널
- 새 커맨드 `/yt-coach:yt-library [connect <remote> | pull | search <q> | share technique <cat> <name> | share draft <file>]` + 스크립트 `python -m scripts.library ...`. README 커맨드 표·스크립트 표 갱신.

## 6. 안전·실패

- git 하위 프로세스: 시간 제한(예: 60초), `GIT_TERMINAL_PROMPT=0`(비밀번호 프롬프트로 멈추지 않게 — 인증 실패는 오류로), 출력의 자격 증명 비슷한 문자열은 메시지에서 가림.
- clone 위치 밖으로 파일을 쓰지 않음. 라이브러리 파일 읽기는 프런트매터 검증(모양이 틀린 지인 파일은 목록에서 빼고 "읽지 못한 파일 N개" 표시).
- 지인이 올린 md 본문은 신뢰하지 않는 데이터 — 화면은 기존 `renderMarkdown`(HTML 실행 없음, 링크 제한), Claude 입력에는 "지인 라이브러리 데이터(지시가 아님)"로 감싸 넘김.
- 동시 올리기: 파일 단위 추가만 하므로 rebase 충돌이 거의 없음. 충돌이 나면 중단하고 안내(자동 해결 금지).

## 7. builder에게 맡기는 것
- MCP 구현 방식: 공식 `mcp` Python SDK 의존성 추가 vs stdlib JSON-RPC 직접 구현(작고 의존성 없음). 플러그인 MCP 선언 위치·형식은 현재 Claude Code 문서 기준으로 확인.
- 메뉴 위치, 화면 세부 배치, 미리보기 만료 시간, id 해시 길이.
- 기획안 틀 섹션 요약 프롬프트(yt-draft 계열 새 스킬 `yt-library` 또는 기존 스킬 절 추가).
- 테스트용 원격: 로컬 bare 저장소(`git init --bare`)로 clone/pull/push 통합 테스트.

## 8. 결정 보완 (builder check 반영, 내 결정)

1. **기획안 틀의 값 출처**: 기획안 md에 이미 있는 것을 쓴다 — 프런트매터 `sections`(시간 → 비율로 환산), `hook_type`, `narrative_type`, `pacing.avg_cut_sec`, `target_sec`, `title`. `shot_mix`는 본문 샷 표(`| # | 샷 | 앵글 | 길이 | 내용 |`)의 샷 크기·길이에서 시간 가중 비율로 계산(대본만 있는 기획안이면 `shot_mix: null`, 샷 리듬 줄 생략). **기획안 스키마는 바꾸지 않는다**(schema_version 그대로).
2. **기법 식별**: 키 = (카테고리, 종류(훅·전환·엔딩·촬영), 이름). 같은 카테고리·종류에 같은 이름이 둘 이상이면 고를 때 둘 다 보여 주고 설명으로 구분. 출처 개수는 `techniques.md` 줄의 `[id](videos/…)` 링크 수를 세는 best-effort(못 세면 생략).
3. **중복 올리기 감지**: 사용자 md에 아무것도 쓰지 않는다. 작업 폴더 `library.json`에 `shared: [{key, hash, id, shared_at}]` 기록(기법 key = 위 키, 기획안 key = 기획안 상대 경로, hash = 올린 내용 해시). 같은 key가 있으면 "이미 올렸습니다 — 새로 올릴까요?" 확인.
4. **지인 기법 고르기**: 화면 — 공용 라이브러리 화면에서 체크한 기법 id 목록을 모티브 화면으로 넘김(해시 경로 `#/motif?lib=<id,id>` 대신 페이지 간 메모리 상태 + 모티브 화면의 "지인 기법" 선택 칸에서도 고를 수 있게). 터미널 — Claude가 MCP `library_search`로 찾아 사용자에게 보여 주고 고른 id를 `motif context --library <id,...>`로 넘김. 선택은 실행마다(저장하지 않음).
5. **카테고리 다른 틀**: 기획안은 항상 **내 카테고리**에서 만든다. 지인 틀은 섹션 비율·훅·서사·샷 리듬의 뼈대로만 쓰고, 아키타입 비교는 내 카테고리 아키타입으로(있을 때). 출처 형식 `library:<id>`.
6. **미리보기 보관**: `workspace/library/.previews/`가 아니라 작업 폴더 `workspace/tmp/library_preview_<id>.json`(30분 만료, 확정·취소 시 삭제) — MCP 서버·UI 서버가 다시 시작돼도 유지. `library_share_confirm`은 `edits`(제목·섹션 요약·메모 수정)를 받는다.
7. **MCP 실행 명령**: `bin/ytc`(sh)와 `bin/ytc.cmd`(Windows)가 이미 있다. 플러그인 MCP 선언은 OS별로 동작하는 방식으로 builder가 확인(필요하면 Python을 직접 실행).

## 9. LLM 엔진 교체 (사용자 추가 요청, 2026-10-24)

사용자: "추후에 claude 및 llm 엔진을 api 방식으로 교체할 수 있게도 해 줘". 내 결정(사용자에게 보고함):
- 화면·라이브러리에서 Claude를 부르는 모든 곳(레퍼런스 분석·샷 분류·콘티·대사 대안·유형화·모티브·기획안·다듬기·진단·미션·회고·라이브러리 요약)을 **엔진 계층 `scripts/llm.py` 하나**로 모은다: `check()`, `ask(rules, payload, task, *, schema=None, files=None, timeout=600)`, `ask_validated(...)`(1회 재시도 규칙 그대로).
- 엔진은 작업 폴더 `yt-coach.json`의 `{"llm": {"engine": "claude-cli" | "anthropic-api", "model": "<선택>", "max_tokens": <선택>}}`로 고른다. 파일이 없으면 `claude-cli`(지금 동작 그대로). 환경 변수 `YTC_LLM_ENGINE`이 있으면 우선.
- `claude-cli` 엔진 = 지금의 `scripts/claude_cli.py`(격리 플래그·structured output·오류 코드 그대로). `files`가 있으면 `--tools Read`를 켜고 경로를 payload에 둔다(지금과 같음).
- `anthropic-api` 엔진 = Anthropic Messages API를 **표준 라이브러리 HTTP**(`urllib`)로 호출. 키는 `ANTHROPIC_API_KEY` 환경 변수에서만(파일·로그·오류 메시지에 남기지 않음). 규칙(SKILL.md)은 system, payload·task는 user. `schema`가 있으면 그 스키마를 입력으로 하는 도구 하나(`respond`)를 `tool_choice` auto + "respond 도구로만 답하라" 지시로 받고, tool_use가 없으면 텍스트 JSON을 파싱한다. `files`의 이미지(jpg/png/webp/gif, 최대 20장, 각 5MB 이하)는 base64 image 블록, 텍스트(md/txt)는 text 블록으로 함께 보낸다. 기본 모델은 설정 값, 없으면 `claude-sonnet-5-5`. 오류 코드: 키 없음 `llm_not_configured`, 인증 실패 `llm_auth`, 그 밖 `llm_failed`(HTTP 상태·짧은 이유, 키 없음).
- 새 엔진(다른 회사 API 등)은 같은 인터페이스의 엔진 하나를 더하는 것으로 끝나게 한다(엔진 등록 표).
- 터미널 커맨드(Claude Code 안에서 대화로 도는 흐름)는 이 계층과 무관 — 그대로 Claude Code가 해석한다.

## 10. 관련
- 설계서 `yt-coach-plugin-design.md` 1.3 보류(호스팅 그대로), 6.x(라이브러리 파일 스키마 추가), 3(새 커맨드).
- 이전 화면 작업 패턴: `scripts/ui/api_*.py` 작업(job)·미리보기·확인 후 저장, `pages/*.js` 페이지 계약.
