# yt-coach 다중 사용자 서버 분리 설계·계획

작성일: 2026-10-31 / 기준 코드: `main` @ f2347d9 (커밋 337개)
상태: 확정(2026-10-31) — 7절 결정 기록. 서버판 구현 완료(2026-11-01, 구현 설계 `2026-11-01-server-edition-design.md`). 2·3.2·4·6절은 구현에 맞게 고쳤고, 다르면 구현 설계가 우선

## 0. 요약

- 지금 버전(로컬 플러그인 + `ytc ui`)은 **v1 로컬판**으로 태그하고 계속 쓸 수 있게 둔다.
- 새 **서버판(v2)** 은 PostgreSQL(사용자별 데이터) + S3 호환 스토리지(MinIO, 리소스 아카이브)로 만든다.
- 두 판은 **같은 코어**(수집·분석 계산·스킬 규칙·검증)를 쓰고, 저장소·LLM 실행·작업 큐·인증만 다르게 끼운다.
- 가장 큰 일은 "작업 폴더 = 전역 경로" 가정을 걷어 내는 것이다(3절). 한 번에 갈아엎지 않고, **로컬판 테스트가 계속 통과하는 상태로** 단계적으로 바꾼다.

## 1. 현재 구조 조사 결과

| 항목 | 현재 | 서버판에 주는 의미 |
|---|---|---|
| 규모 | Python `scripts/` 약 19,100줄, UI 정적 JS 약 7,400줄, 테스트 70여 개 | 테스트가 리팩터링 안전망이 된다 |
| 작업 폴더 | `common.work_root()`가 `YT_COACH_HOME` 환경 변수 또는 현재 폴더를 돌려줌. `work_root/workspace_dir/category_dir` 호출 약 150곳(6절 S2 측정) | **프로세스 전역 = 사용자 1명** 가정. 서버에서 가장 먼저 풀어야 함 |
| 파일 입출력 | `read_text/write_text/open/glob/exists/shutil` 등 약 316곳. `"drafts"`, `"mine"`, `"refs"` 같은 경로 문자열이 코드 곳곳에 있음 | 저장소 인터페이스로 모아야 함 |
| 원본 데이터 | md 프런트매터가 유일한 원본, 본문·`## 내 메모` 바이트 보존(`refstore`) | PG에 옮겨도 "프런트매터 JSON + 본문 원문" 의미는 그대로 유지 |
| LLM | `llm.py` 엔진 계층 이미 있음: `claude-cli`(내 구독) / `anthropic-api`(키) | **잘 된 이음매.** 서버는 `anthropic-api`만 쓰고 사용자별 사용량 기록만 추가 |
| 작업 실행 | `ui/jobs.py` JobManager: 프로세스 안 스레드, `SINGLE_KINDS`·`EXCLUSIVE`로 동시 실행 제한 | 제한이 "PC 전체" 기준. 서버는 사용자별 제한 + 워커 풀로 바꿔야 함 |
| HTTP | 표준 라이브러리 `ThreadingHTTPServer`, 자체 라우터, `api_*.py` 14개, 주소 key 쿠키 인증 | 핸들러 함수는 재사용, 서버·인증만 교체 |
| 영상별 원본 | `workspace/raw|cuts|frames|shots/{video_id}` | 이미 영상 ID 기준 → **사용자 간 공유 캐시**로 바로 쓸 수 있음 |
| 버전 이력 | 콘티만 `workspace/conti_history/<stem>/NNNN.json` | 아카이브 버전 모델의 출발점 |
| 공용 라이브러리 | 지인과 비공개 git 저장소 공유(`library.py`, MCP 서버) | 서버판에서는 DB 안 공유 기능으로 대체 |
| 촬영 영상 | `footage.py`가 내 PC 폴더의 원본을 읽기만 함(업로드 없음) | 서버판은 업로드 정책 결정 필요(7절) |
| 자막 영상 | Remotion(Node + 헤드리스 Chrome)로 mp4 렌더 | 서버 워커에 무거운 렌더 전용 큐 필요 |

실사용 데이터(사용자 1명, 레퍼런스 20개·기획안 6편):

- 전체 약 255MB. `drafts/` 156MB 중 **콘티 HTML 한 파일이 33~46MB**로 대부분이다. 원인은 장면 그림이 아니라 **컷마다 base64로 박은 레퍼런스 사진(JPEG)** 이다: 장면으로 합치지 않은 콘티 4개(357~469컷)가 사진 357~469장을 담는다. 장면으로 합치고 장면 그림(SVG, 장면당 2~3KB)을 쓴 콘티는 1MB, 0.13MB. `workspace/frames` 60MB(영상당 3~4MB), `trash` 30MB.
- 서버판에서는 콘티 HTML에 사진·그림을 넣지 않고 **개별 객체로 저장하고 링크**해야 한다. 그렇지 않으면 버전마다 수십 MB씩 쌓인다(S4, 로컬판에도 먼저 적용).

## 2. 목표 구조

**결정(2026-11-01): 물리 이동 없음.** 처음 안의 `core/ local/ web/` 재배치는 하지 않았다. 플러그인 경로(`commands/*.md`의 `${CLAUDE_PLUGIN_ROOT}`, `bin/ytc`, `.mcp.json`)를
그대로 두려는 것이고, 로컬판 동작을 바꾸지 않는다. `scripts/`가 코어와 로컬 UI를 겸하고, 서버판은 새 패키지 `server/`, 배포는 `deploy/`다.

```
yt-coach/
├── scripts/      코어(수집·계산·도메인 규칙·저장소 Protocol·LLM) + 로컬 UI(stdlib). ui/static = 두 판 공용 웹
├── server/       서버판(Python 패키지): config, auth·oidc, asgi(Starlette 어댑터), pgstore·s3store·keyindex·scratch·files,
│                 jobs_registry, usage, admin(CLI), migrations(Alembic), web/(로그인·메시지 화면). scripts만 import
├── deploy/       Dockerfile(base·local·server), local/·server/ compose + .env.example, Caddyfile, smoke.py
├── commands/ skills/ templates/ remotion/ bin/ .claude-plugin/ .mcp.json   로컬 플러그인(변경 없음)
└── tests/        코어 테스트 + tests/contract(File·Pg·S3 같은 계약) + tests/server(PG·MinIO 필요, YTC_TEST_DOCKER=1)
```

의존 방향은 `server → scripts`만이다. `scripts`가 `server`를 import하면 `tests/test_layout_gates.py`가 실패한다.
서버 구현은 `Ctx(backend=…)`로만 들어온다.

### 2.1 코어 인터페이스

```python
class Ctx:                      # 요청·작업 하나의 실행 문맥
    user_id: str | None         # 로컬판은 None(단일 사용자)
    store: DocStore             # md 문서(프런트매터 dict + 본문 str)
    objects: ObjectStore        # 바이너리·큰 파일(프레임, 그림, srt, mp4, 촬영본)
    shared: SharedCache         # 영상 ID 기준 공유 원본(raw, cuts, frames, shots)
    llm: LlmRunner              # 기존 llm.ask 계층
    usage: UsageMeter           # 로컬판은 아무것도 안 함

class DocStore:                 # 경로 대신 "문서 키"
    get(key) -> Doc             # Doc(front: dict, body: str, version)
    put(key, doc, *, expect_version)   # 낙관적 잠금, 본문·내 메모 보존 규칙은 여기서 강제
    list(prefix) -> list[DocMeta]
    delete(key)                 # 휴지통으로
# 키 = 작업 폴더 기준 POSIX 상대 경로 문자열 그대로(S3 설계 ADR-1). 확장자와 `ref_` 접두사를 포함한다.
# 키 예: refs/ref_travel/videos/Q8PSsikq8PA.md, drafts/2026-10-25-부루마블-1.md, drafts/2026-10-25-부루마블-1-conti.md,
#       mine/<이름>.md, missions/2026-10-31.md, workspace/frames/<영상ID>/0003.jpg
# 키 함수는 scripts/keys.py 한 곳. 서버판 PgStore·S3ObjectStore가 키를 행·객체 이름으로 바꾸는 변환은 S5에서 그 안에 둔다.
```

- 로컬판 `FileStore`: 키 → 지금과 똑같은 파일 경로. **작업 폴더 구조와 파일 내용은 바뀌지 않는다.**
- 서버판 `PgStore`: 키 → PG 행. 프런트매터는 JSONB, 본문은 text.

### 2.2 전환 요령: contextvars로 전역 경로 걷어 내기

200곳을 한 번에 고치지 않는다.

1. `work_root()`가 환경 변수 대신 `contextvars.ContextVar[Ctx]`를 먼저 보게 바꾼다. 로컬판은 호출마다(`common.run`)·요청마다(UI 서버) 문맥을 씌운다(시작 때 한 번 설정하지 않는다) → 동작 그대로.
2. 파일 입출력을 모듈 단위로 `ctx.store`/`ctx.objects` 호출로 옮긴다. 모듈 하나 옮길 때마다 기존 테스트가 통과해야 한다.
3. 다 옮긴 모듈은 `work_root()`를 더 부르지 않는다. 남은 호출 수를 CI에서 세어 0으로 줄인다.

## 3. 서버판 데이터 모델

### 3.1 공유 vs 개인

| 공유(사용자 무관, 영상 ID 기준) | 개인(user_id 필수, RLS) |
|---|---|
| 영상 메타데이터, 자막 원본, 컷 통계, 프레임, 샷 분류, 프레임 설명, **기본 분석 결과** | 프로필, 카테고리, 내가 넣은 레퍼런스와 역할(ref/mine), **섹션·유형 교정과 메모**, 유형화, 모티브, 기법 카탈로그, 기획안, 다듬기, 콘티와 이력, 진단, 미션, 회고, 리포트, 촬영 영상 처리 결과, 자막 SRT·자막 영상 |

- 교정은 공유 분석을 덮어쓰지 않는다: `user_videos.overrides`(JSONB)에 저장하고, 계산 입력은 "공유 분석 ⊕ 내 교정"으로 만든다. 로컬판의 "md를 고치면 교정" 원칙과 결과가 같아야 한다.
- 유형화는 `role = 'ref'`만 쓴다(기존 원칙). 쿼리와 테스트로 보장한다.

### 3.2 PostgreSQL 스키마 골격

> **실제 구현(2026-11-01)은 이 골격보다 작다.** 문서는 종류별 테이블 대신 `docs` 한 테이블(원문 바이트 + version + mtime), 객체는 `objects`·`shared_objects`
> 인덱스 + MinIO, 그 밖에 `users`·`folders`·`trash_items`·`llm_usage`다. `projects`·`doc_versions`·`analysis_runs`·`jobs`·`library_items`는 만들지 않았다
> (버전은 문서 쪽 규칙이 이미 있고, 작업은 프로세스 메모리, 사용량은 `llm_usage`). 정확한 DDL은 구현 설계 §3.1(`server/migrations`)을 본다.
> 아래는 처음 골격이며 S10·S11에서 필요하면 되살린다.

```sql
-- 사용자
users(id uuid pk, email unique, display_name, created_at)
profiles(user_id pk → users, front jsonb, body text, version int)

-- 공유
videos(id text pk /*youtube id*/, meta jsonb, fetched_at, fetch_status)
video_assets(video_id → videos, kind /*raw|cuts|shots|frame_desc*/, data jsonb, version, run_id)
video_analyses(id, video_id → videos, schema_version, front jsonb, body text, run_id, created_at)

-- 개인 문서
categories(id, user_id, slug, name, unique(user_id, slug))
user_videos(id, user_id, category_id, video_id → videos, role /*ref|mine*/,
            analysis_id → video_analyses, overrides jsonb, memo text, version,
            unique(user_id, category_id, video_id))
docs(id, user_id, key text, kind /*index|techniques|draft|conti|diagnose|mission|review|report*/,
     front jsonb, body text, version int, updated_at, deleted_at, unique(user_id, key))
projects(id, user_id, category_id, title, stage, source_motif, draft_doc_id, conti_doc_id, created_at)

-- 버전·아카이브
doc_versions(id, doc_id → docs, n int, kind /*created|auto|named|before_recreate|before_merge|restored|import*/,
             name, front jsonb, body text, snapshot_object_id → storage_objects, run_id, created_at)
storage_objects(id uuid pk, bucket, object_key unique, sha256, size_bytes, content_type,
                owner_user_id /*공유면 null*/, ref_type, ref_id, status /*pending|ready|deleting|deleted*/,
                created_at, deleted_at)

-- 실행·계측
analysis_runs(id, user_id, kind, engine, model, prompt_version /*skills 파일 해시*/, input_hash,
              input_tokens, output_tokens, cost_usd, status, error, started_at, finished_at)
jobs(id, user_id, kind, status, items jsonb, queue jsonb, progress jsonb, worker, heartbeat_at, created_at)

-- 공유 라이브러리(git 라이브러리 대체)
library_items(id, author_id, kind /*technique|template*/, category, front jsonb, body text,
              visibility /*private|friends|public*/, worked bool, created_at)
```

- 모든 개인 테이블에 **Row Level Security**: 연결마다 `SET app.user_id`, 정책 `user_id = current_setting('app.user_id')`.
- 콘티 이력(`conti_history`)은 `doc_versions`로 옮기고, 기획안·기법·진단도 같은 모델로 버전을 남긴다. `GROUP_SEC`(5분 묶음) 같은 기존 규칙은 그대로.

### 3.3 S3 호환 스토리지(MinIO) 아카이브

코드는 S3 API만 쓴다(개발 MinIO, 운영 S3·R2 등 endpoint 설정만 교체). 버킷 하나 + 버전 관리 켬.

```
shared/videos/{video_id}/raw.json
shared/videos/{video_id}/frames/{nnnn}.jpg
users/{uid}/projects/{pid}/conti/art/{scene_id}-{basis8}-{sha6}.svg  장면 그림(로컬판과 같은 이름), HTML에 넣지 않고 개별 객체
users/{uid}/projects/{pid}/conti/v{n}/conti.html               인쇄·공유용 렌더(그림은 링크)
users/{uid}/projects/{pid}/subtitles/v{n}/{captions|lines}.srt
users/{uid}/projects/{pid}/media/captions-v{n}.mp4             자막 영상
users/{uid}/projects/{pid}/footage/{fingerprint}/…             촬영본 처리 결과(7절 결정에 따라 원본 포함 여부)
users/{uid}/reports/{category}-{date}.html
users/{uid}/exports/{date}-{pid}.zip
trash/{uid}/{date}/…                                           30일 뒤 삭제
```

추적 규칙
- **업로드**: `storage_objects`에 `pending` 행 → 업로드 → 크기·sha256 확인 → `ready`. 큰 파일은 브라우저가 presigned URL로 멀티파트 직접 업로드.
- **삭제**: `deleting` → 객체 삭제 → `deleted`. 프로젝트 삭제는 `trash/`로 옮기고 30일 유예.
- **객체 메타데이터 태그**에도 `user-id`, `project-id`, `ref-type`을 넣어 양쪽에서 추적.
- **정합성 점검 작업**(매일): 버킷 목록 ↔ `storage_objects` 대조 → 고아 객체·깨진 참조 보고. 필요하면 MinIO 버킷 알림을 PG로 받아 감사 로그.
- **접근**: 브라우저는 스토리지에 직접 접근하지 않고, 서버가 권한 확인 후 짧은 만료의 presigned GET을 준다.
- **중복 제거**: 같은 sha256이면 객체 재사용.
- **쿼터**: 사용자별 `sum(size_bytes)`. 촬영본·자막 영상은 별도 한도.
- **수명 주기**: 오래된 버전은 최근 N개만 유지하거나 저비용 등급으로 이동. 레퍼런스 영상 원본은 지금처럼 저장하지 않는다(프레임 추출 후 삭제).
- **내보내기**: 프로젝트 zip = `manifest.json`(버전·출처·run 정보) + 로컬판 작업 폴더와 **같은 구조**의 md·srt·그림. 로컬판에서 그대로 열 수 있다.

## 4. 서버판 실행 구조

| 구성 | 내용 |
|---|---|
| API 서버 | Starlette(ASGI) 어댑터 한 개(`server/asgi.py`)가 기존 `ROUTES` 표를 그대로 부른다. 요청마다 세션·사용자(비활성·epoch) 확인 → `Ctx(user_id, PgBackend)` |
| 인증 | Google 로그인(OIDC, PKCE·state·nonce), 초대한 이메일만, 첫 로그인에 `google_sub` 고정. 세션 쿠키 + CSRF(Origin·JSON). 로컬판의 주소 key는 쓰지 않는다 |
| 작업 실행 (ADR-S6) | **앱 프로세스 안 `JobManager`를 사용자마다 하나**(`jobs_registry.Registry.for_user`, uvicorn `--workers 1`). `SINGLE_KINDS`·`EXCLUSIVE`가 사용자별로 적용되고, 진행 표시·확인 대기·취소는 기존 메모리 구조 그대로(화면은 폴링). 전역 자원은 `scripts/limits.py` 슬롯: media 2, llm 4, 유튜브 수집은 서버 전체 1개 순번. 재시작하면 진행 중 작업은 사라진다(저장된 md는 그대로) |
| S7b (보류) | PG `jobs` 큐 + 별도 `media` 워커 프로세스. 조건: media 대기 p95가 5분을 넘거나, 재시작으로 작업을 잃는 일이 주 1회 이상 |
| LLM | `anthropic-api` 엔진만(엔진·모델·max_tokens는 env로 고정, 사용자 설정 무시). 호출마다 `llm_usage` 기록, 사용자별 일 한도(한국 시간, `YTC_DAILY_USD`), 가격표에 없는 모델은 시작 거부. `claude-cli`는 로컬판 전용 |
| YouTube 수집 | `YT_PROXY`, `YT_FETCH_INTERVAL`을 서버 전체 순번으로. 같은 영상 수집은 PG advisory lock, 공유 캐시에 있으면 생략 |
| 비활성 | 공용 라이브러리, 자막 영상, 촬영 영상, srtfit(`/api/features`로 숨기고 API는 403 `feature_disabled`) |
| 도움말 챗봇 | 지금처럼 개수·상태만 입력(본문·메모·경로 제외) |

## 5. 로컬판(v1) 유지 방침

- 지금 `main`을 `v1.0.0`으로 태그하고 `release/v1` 브랜치를 만든다.
- 로컬판은 기능 동결 + 버그 수정 + 코어 개선만 받는다(7절 결정에 따라 조정).
- 작업 폴더 형식은 바꾸지 않는다. FileStore가 지금 파일 구조를 그대로 읽고 쓴다.
- 공용 git 라이브러리, MCP 서버, `claude-cli` 엔진, 촬영 폴더 직접 읽기는 로컬판에만 남는다.
- CLAUDE.md의 보류 항목 "호스팅·다중 사용자 웹 서비스"는 `server/`에 한해 해제하도록 고친다.

## 6. 진행 단계

각 단계는 **로컬판 테스트 전부 통과**가 기본 완료 조건이다.

| 단계 | 작업 | 완료 조건 |
|---|---|---|
| S0 준비 | `v1.0.0` 태그, `release/v1` 브랜치, 7절 결정, CLAUDE.md 범위 수정 | 결정 사항이 이 문서에 기록됨 |
| S1 모노레포 재배치 | `core/ local/ web/ server/` 이동만(동작 변경 없음), import 경로 정리, `bin/ytc`·플러그인 경로 수정 | 플러그인 재설치 후 커맨드 12개·`ytc ui` 정상, pytest 통과 **대체(2026-11-01).** 물리 이동 없이 `server/`·`deploy/`만 추가(2절). 로컬 동작 변경 0 |
| S2 Ctx 도입 | `Ctx`·contextvar, `work_root()`가 Ctx를 보게 변경, 로컬판 시작 지점에서 설정 | 동작 동일, `work_root()` 호출 수 측정 스크립트 추가. 측정(2026-10-31, `python -m scripts.count_work_root`): `work_root(` 100 · `workspace_dir(` 42 · `category_dir(` 4, 합계 146곳 / 36개 모듈(많은 순 conti 12, mission 12, videomd 10, common 8, ui/api_diagnose 8). 로컬판 시작 지점은 `common.run`과 UI 요청 스레드가 `ctx.use()`로 호출 동안만 설정하고, 작업 스레드는 `copy_context()`로 이어받는다 |
| S3 저장소 인터페이스 | `DocStore`·`ObjectStore`·`SharedCache` 정의, `FileStore` 구현, 모듈별로 파일 입출력 이전(refstore → videomd → motif → draft → conti·conti_history → mission → similarity → report …) | **완료(2026-10-31).** 허용 목록 밖 `work_root(`·`workspace_dir(`·`category_dir(`·`existing_category_dir(`·`move_to_trash(` 호출 0, 경로 문자열 상수·`ctx.root` 접근 0(`tests/test_no_direct_paths.py`가 AST로 강제). 측정(`python -m scripts.count_work_root`): 허용 목록(allowed) 6곳(common 3 · ctx 1 · ui/__main__ 2), 그 밖(outside) 0곳. S2 측정 146곳 → 0 |
| S4 콘티 그림 분리 | 콘티 HTML이 그림을 박지 않고 별도 파일을 참조(로컬판에도 적용, 인쇄용 단일 파일은 옵션) | 콘티 HTML 수백 KB 수준, 화면·인쇄 동일 |
| S5 PG·S3 구현 | 스키마·마이그레이션(Alembic), RLS, `PgStore`, `S3ObjectStore`, `storage_objects`·`doc_versions`, docker-compose(PG + MinIO) | core 테스트를 FileStore·PgStore 두 구현으로 같이 돌려 통과 **완료(2026-10-31).** PgDocStore·S3ObjectStore·S3SharedCache·ServerScratch, Alembic `0001_init`, RLS(앱 롤 NOBYPASSRLS). 계약 테스트가 File·Pg·S3에서 같이 통과 |
| S6 서버 API·인증 | ASGI 앱, 로그인, 요청별 Ctx, 기존 핸들러 연결, presigned 업로드·다운로드 | 사용자 2명이 서로 데이터를 못 보는 것을 테스트로 확인 **완료(2026-10-31).** Starlette 어댑터, Google 로그인, 요청별 Ctx, `/files` 프록시(presigned 대신 앱이 스트리밍). 두 사용자 격리 테스트 통과 |
| S7 작업 큐·워커 | `jobs` 테이블, light/media/render 워커, 사용자별 동시 실행 제한, 진행 표시·확인 대기·취소 | 두 사용자가 동시에 수집·콘티를 돌려도 서로 막히지 않음 **완료(2026-10-31), 변경:** 큐·워커 프로세스 대신 사용자별 JobManager(4절). 분리는 S7b로 보류 |
| S8 사용량·쿼터 | `analysis_runs` 기록, 사용자별 LLM 한도·스토리지 쿼터, 관리자 집계 화면 | 한도 초과 시 친절한 오류 **최소판 완료(2026-10-31):** 호출 기록 + 사용자별 일 한도 + `admin usage`. 월 한도·스토리지 쿼터·집계 화면은 보류 |
| S9 가져오기·내보내기 | 로컬 작업 폴더 → 서버 계정 import(현재 travel 데이터로 검증), 프로젝트 zip 내보내기 | 현재 레퍼런스 20개·모티브 22장·기획안 6편·콘티 이력이 그대로 보임 **최소판 완료(2026-10-31):** `admin import`(재실행 안전, 내 메모 보호, 가져온 뒤 목록 비교). zip 내보내기는 보류 |
| S10 정합성·운영 | 스토리지 정합성 점검, 휴지통 만료, 백업(PG 덤프 + 버킷 복제), 수집 프록시·속도 제한 | 점검 리포트에 고아·깨진 참조 0 **대기.** 백업(PG 덤프 + 버킷), 정합성 점검, 휴지통 만료 |
| S11 공유 라이브러리 | `library_items` + 공개 범위, git 라이브러리에서 가져오기 | 지인 기법이 서버에서 검색·사용됨 **대기.** 서버 v1에서는 비활성 |

S1~S4는 로컬판에도 이득이 되는 리팩터링이라 서버 결정이 늦어져도 먼저 진행할 수 있다.

## 7. 결정 (2026-10-31, 사용자 확인)

1. **서버판 이름**: 같은 `yt-coach`, 서버판을 v2로 올린다(코어를 같이 쓰므로 이름을 나누지 않는다).
2. **로컬판 유지 범위**: 서버판이 안정될 때까지 버그 수정 + 코어 개선만. 새 기능은 서버판에서 만든다.
3. **촬영 영상**: 처리 결과(장면·사진·받아쓰기)만 올린다. 원본은 올리지 않는다.
4. **자막 범위**: 레퍼런스 자막도 공유 영역에 보관(3.1 설계 그대로).
5. **인증 방식**: Google 로그인.
6. **사용자 규모·배포 위치**: 지인 수십 명 규모. 집 서버 또는 작은 클라우드 1대. 공개 서비스는 하지 않는다.
7. **LLM 비용 부담**: 운영자 키 + 사용자별 일/월 한도. 사용자 키 등록은 하지 않는다.

진행 순서: S0(태그·범위 수정) → 로컬판에도 이득인 S4(콘티 HTML에서 사진·그림 빼기)와 S2(Ctx 도입) → S3. S1(폴더 재배치)은 플러그인 재설치가 필요하므로 서버판 구현(S5)을 시작하기 직전에 한다.

## 8. 위험

| 위험 | 대응 |
|---|---|
| 저장소 리팩터링 중 로컬판 회귀 | 모듈 단위 이전, 기존 테스트 + 실제 작업 폴더 사본으로 수동 확인 |
| 서버 IP에서 YouTube 차단 | 프록시, 전역 속도 제한, 공유 캐시로 요청 수 감소 |
| LLM 비용 폭증 | 사용자별 한도, `analysis_runs` 집계, 같은 입력 해시 재사용 |
| 콘티 그림·자막 영상 용량 | 그림 분리(S4), 버전 수 제한, 쿼터 |
| md "본문 바이트 보존"이 DB에서 깨짐 | `DocStore.put`에서 보존 규칙 강제 + 왕복 테스트(md → DB → md 동일) |
| Remotion 서버 렌더 부하·라이선스 | render 전용 큐, 동시 1개. 회사 규모가 되면 Remotion 회사 라이선스 확인 |
