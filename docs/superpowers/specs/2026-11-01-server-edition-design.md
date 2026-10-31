# 서버판(v2) 구현 설계 — S1·S5·S6·S7·S8(최소)·S9(최소)·패키징

작성일: 2026-11-01 / 기준: `feat/server` (main @ db8ffd5, S2·S3 완료)
상위 설계: `2026-10-31-multiuser-server-design.md`(§2~§7 결정), `2026-10-31-s3-storage-design.md`(ADR-1~7, Protocol)
상태: 초안. 이 문서와 상위 설계가 다르면 이 문서의 ADR이 우선하고, 구현이 끝나면 상위 설계 §2·§4·§6을 고친다(T14).

## 0. 범위와 완료 정의

- 사용자 결정(2026-11-01): 서버판을 먼저 만들고, **두 판을 각각 docker-compose로 묶는다.**
  ① 개인판(로컬, 단일 사용자, 작업 폴더 볼륨) ② 중앙 서버판(PostgreSQL + MinIO + app). 두 컨테이너 모두 Claude는 `ANTHROPIC_API_KEY`.
- 이번 범위: S1(배치), S5(PG·MinIO 저장소), S6(API·Google 로그인·사용자 격리), S7(작업 실행·사용자별 제한),
  S8 최소(LLM 사용량 기록 + 사용자별 일 한도), S9 최소(로컬 작업 폴더 → 서버 계정 가져오기), 패키징.
- 보류: S10(정합성·백업·휴지통 만료), S11(공유 라이브러리), 서버 자막 영상 렌더, 촬영본 업로드, presigned URL, 다중 프로세스 워커(S7b).
- 완료 정의: ① 각 Task 끝에 `pytest tests/` 전부 통과(로컬판 회귀 0), ② 저장소 계약 테스트가 File·Pg 두 구현에서 통과,
  ③ 두 사용자 격리 테스트 통과, ④ 두 compose 스택 스모크 통과.

## 1. 결정 기록(ADR)

| # | 결정 | 이유 | 기각한 대안 |
|---|---|---|---|
| ADR-S1 | **물리 이동 없음.** `scripts/`가 코어+로컬 UI로 남고, 서버판은 새 최상위 패키지 `server/`, 배포는 `deploy/` | 플러그인 경로(`commands/*.md`의 `${CLAUDE_PLUGIN_ROOT}/bin/ytc`, `bin/ytc`의 `python -m scripts.<x>`, skills의 스크립트 이름, `.claude-plugin`, `.mcp.json`), 테스트 70여 개의 `from scripts import`가 그대로다. 이동은 기능 이득 0, 플러그인 재설치·import 전면 수정 위험만 있다. 판 분리는 **의존 방향 게이트 + 별도 requirements + Docker target**으로 충분히 강제된다 | 상위 §2의 `core/ local/ server/ web/` 이동(로컬판을 동결한 뒤 `scripts→core` 이름 변경만 따로 검토) |
| ADR-S2 | 의존 방향: `server → scripts`만. `scripts`는 `server`를 import하지 않는다(AST 게이트). 서버 구현은 `Ctx(backend=…)`로만 들어온다 | 순환 의존 금지, 로컬판이 서버 의존성(psycopg, boto3) 없이 돈다 | 코어 안 `if edition == "server"` 분기 |
| ADR-S3 | 문서는 **`docs` 한 테이블에 원문 바이트(bytea)** + version(sha256) + mtime_ns. 상위 §3.2의 종류별 테이블(categories, user_videos, projects, doc_versions …)은 만들지 않는다 | Protocol이 바이트 계층(S3 ADR-2)이라 파싱해서 나눠 넣으면 왕복 바이트 동일성이 깨진다. 수십 명·사용자당 수백 행이라 질의 성능 요구 없음. 종류별 테이블은 "공유 분석 ⊕ 내 교정"(상위 §3.1)을 할 때 **파생 인덱스**로 추가한다(원본은 계속 `docs.raw`) | `front jsonb + body text`(BOM·CRLF·키 순서 손실) |
| ADR-S4 | 사용자 객체·공유 캐시 바이트는 MinIO, **목록·stat·exists의 원본은 PG 인덱스 행**(`objects`, `shared_objects`) | `Meta.mtime_ns`(conti_history 캐시 스탬프)가 S3 LastModified(ms)로는 부족하고, 목록 순서·폴더 의미를 PG에서 정확히 재현할 수 있다. 쓰기 순서 "객체 PUT → 행 upsert"로 행이 있으면 객체가 있다 | MinIO ListObjects를 원본으로(정밀도·일관성 부족), 작은 객체 PG 인라인(이중 경로) |
| ADR-S5 | HTTP는 **Starlette(ASGI) + 기존 `ROUTES` 표를 그대로 부르는 어댑터 한 개.** 핸들러 수정 없음 | 핸들러가 이미 `Request` 데이터클래스 → dict/tuple/Response 순수 계약이다. Starlette는 세션 미들웨어·OIDC(authlib)·스트리밍·TestClient를 준다. FastAPI의 Pydantic 모델은 dict 핸들러에 이득이 없다 | stdlib 서버 + oauth2-proxy(헤더 신뢰·컨테이너 1개 추가·ThreadingHTTPServer 운영 노출), 핸들러를 FastAPI 라우터로 재작성 |
| ADR-S6 | 작업은 **앱 프로세스 안 JobManager를 사용자마다 하나씩**(uvicorn `--workers 1`). PG 큐 + 별도 워커 프로세스는 S7b로 미룬다 | 작업 함수 27곳이 클로저이고 `wait_decision`·`next_task`·`push_task`·`take_review`로 HTTP 스레드와 메모리 안에서 주고받는다. 직렬화하려면 api_* 전부를 다시 써야 한다. 수십 명·VM 1대에서 무거운 일은 전역 세마포어로 묶으면 충분 | 상위 §4의 `jobs` 테이블 + `FOR UPDATE SKIP LOCKED` 워커(지금 규모에 과함) |
| ADR-S7 | `/files/<key>`는 **앱이 권한 확인 후 MinIO에서 스트리밍(Range 포함)**. presigned 안 씀 | URL·콘티 HTML 안 상대 링크가 그대로 동작, MinIO를 외부에 열지 않음, 쿠키 인증 한 곳 | presigned GET(MinIO 공개 엔드포인트·CORS 필요) |
| ADR-S8 | DB 접근은 **psycopg 3 + psycopg_pool 직접 SQL**, 마이그레이션은 Alembic(op.execute 원시 SQL) | 테이블 7개·질의 수십 개, ORM 매핑 이득 없음. Alembic은 상위 설계 결정 | SQLAlchemy ORM |
| ADR-S9 | 격리는 **RLS가 최종 방어선**: 개인 테이블 `FORCE ROW LEVEL SECURITY`, 트랜잭션마다 `set_config('app.user_id', uid, true)`, 앱 롤은 `NOBYPASSRLS` | 저장소 코드 실수(WHERE 누락)가 있어도 다른 사용자 행이 안 보인다. 설정이 없으면 행 0개(fail closed) | 애플리케이션 WHERE만 |
| ADR-S10 | LLM은 두 판 모두 `anthropic-api`. 서버판은 엔진·모델·max_tokens를 **환경 변수로 고정**하고 사용자 `yt-coach.json`의 `llm` 절을 무시 | 운영자 키 비용 통제(상위 결정 7). 컨테이너에는 claude CLI가 없다 | 사용자별 모델 선택 |
| ADR-S11 | 서버 v1 비활성: 공용 라이브러리, 자막 영상(Remotion), srtfit, 촬영 영상 처리·컷 파일. 화면은 `/api/features`로 숨김, API는 403 `feature_disabled` | 모두 사용자 PC 경로(촬영 원본·영상 파일)·git 자격 증명·Node+Chrome이 필요하다. 상위 결정 3(처리 결과만 업로드)은 업로드 API가 생길 때(S7b 이후) 한다. 로컬에서 처리한 결과는 S9 가져오기로 보존된다 | 서버에 render 워커(무겁고 VM 1대 자원 경쟁) |

## 2. S1 배치

```
(저장소 루트, 플러그인 경로 전부 그대로)
├── .claude-plugin/ commands/ skills/ templates/ remotion/ bin/ytc .mcp.json   변경 없음
├── scripts/                 코어 + 로컬 UI(stdlib). ui/static/ = 두 판 공용 웹(로그인 화면만 서버 전용)
├── server/                  서버판 Python 패키지 (scripts만 import)
│   ├── config.py            env → Settings(frozen). 필수 값 없으면 시작 거부
│   ├── db.py                pool, user_tx(uid), admin_tx()
│   ├── pgstore.py           PgDocStore, PgTrash
│   ├── s3store.py           S3ObjectStore, S3SharedCache (PG 인덱스 + MinIO)
│   ├── scratch.py           ServerScratch
│   ├── backend.py           ServerBackend(Backend) — store/objects/shared/scratch/trash/usage
│   ├── usage.py             UsageMeter, 가격표, 한도
│   ├── jobs_registry.py     사용자별 JobManager, 전역 세마포어 설치
│   ├── asgi.py              Starlette 앱, ROUTES 어댑터, features 가드
│   ├── auth.py              Google OIDC, 세션, CSRF, dev 로그인
│   ├── files.py             /files 프록시(Range)
│   ├── admin.py             CLI: add-user · disable-user · import · usage
│   ├── migrations/ alembic.ini
│   └── web/login.html
├── deploy/
│   ├── Dockerfile           target: base · local · server
│   ├── local/docker-compose.yml  .env.example
│   ├── server/docker-compose.yml  docker-compose.test.yml  .env.example  Caddyfile
│   └── smoke.py
├── requirements.txt         그대로(플러그인 .venv용)
├── requirements-server.txt  -r requirements.txt + starlette uvicorn authlib itsdangerous httpx psycopg[binary] psycopg_pool alembic sqlalchemy boto3
└── tests/  tests/server/    서버 테스트는 PG·MinIO 환경 변수가 없으면 skip
```

- 게이트(`tests/test_no_direct_paths.py` 확장): ① `scripts/**`가 `server`를 import하면 실패 ② `server/**`는 경로 조립 허용 목록
  (`scratch.py`, `files.py`, `admin.py`의 import 걷기, 임시 파일)만 ③ `server/`도 키 문자열 상수 금지(키는 `keys.py`).
- `scripts/` 쪽 변경은 **이음매(seam)만**: 아래 §3.6·§5.4·§6·§7·§8의 작은 훅. 기본값은 로컬판 동작 그대로.

## 3. S5 저장소

### 3.1 PostgreSQL 스키마 (Alembic `0001_init`)

```sql
-- 롤: ytc_owner(마이그레이션, 테이블 소유, 슈퍼유저 또는 BYPASSRLS) / ytc_app(이름·비밀번호는 YTC_DB_DSN.
--     LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT, 어떤 롤에도 속하지 않음, DML만)
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL UNIQUE CHECK (email = lower(email)),
  google_sub text UNIQUE,                 -- 첫 로그인 때 채운다. 이후 sub 불일치면 거부
  display_name text,
  is_admin boolean NOT NULL DEFAULT false,
  disabled boolean NOT NULL DEFAULT false,
  session_epoch int NOT NULL DEFAULT 0,   -- 올리면 모든 세션 무효
  daily_usd_limit numeric(8,2),           -- null = YTC_DAILY_USD
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE docs (                       -- DocStore. 키는 ADR-1 그대로(.md)
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  key text COLLATE "C" NOT NULL,
  raw bytea NOT NULL,                     -- 디스크 바이트 그대로(BOM·CRLF 포함)
  version char(64) NOT NULL,              -- sha256(raw)
  size int NOT NULL,
  mtime_ns bigint NOT NULL,
  PRIMARY KEY (user_id, key));

CREATE TABLE folders (                    -- ensure_folder + 쓰기 때 조상 폴더. 문서·객체 공용(같은 작업 폴더 트리)
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  prefix text COLLATE "C" NOT NULL,       -- '/'로 끝남
  PRIMARY KEY (user_id, prefix));

CREATE TABLE objects (                    -- ObjectStore 인덱스. 바이트는 MinIO
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  key text COLLATE "C" NOT NULL,
  size bigint NOT NULL, sha256 char(64) NOT NULL, mtime_ns bigint NOT NULL,
  s3_key text NOT NULL CHECK (s3_key = 'users/' || user_id::text || '/' || key),
  PRIMARY KEY (user_id, key));

CREATE TABLE shared_objects (             -- SharedCache 인덱스(RLS 없음)
  key text COLLATE "C" PRIMARY KEY,
  size bigint NOT NULL, sha256 char(64) NOT NULL, mtime_ns bigint NOT NULL,
  s3_key text NOT NULL, created_by uuid REFERENCES users ON DELETE SET NULL);

CREATE TABLE trash_items (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  stamp text NOT NULL, key text COLLATE "C" NOT NULL,
  kind text NOT NULL CHECK (kind IN ('doc','object')),
  raw bytea, s3_key text, size bigint NOT NULL, mtime_ns bigint NOT NULL,
  CHECK (s3_key IS NULL OR s3_key = 'trash/' || user_id::text || '/' || stamp || '/' || key),
  moved_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, stamp, key));

CREATE TABLE llm_usage (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  at timestamptz NOT NULL DEFAULT now(),
  job_kind text, model text NOT NULL,
  input_tokens int NOT NULL, output_tokens int NOT NULL,
  cache_read_tokens int NOT NULL DEFAULT 0, cache_write_tokens int NOT NULL DEFAULT 0,
  cost_usd numeric(10,6) NOT NULL, ok boolean NOT NULL);
CREATE INDEX llm_usage_user_at ON llm_usage (user_id, at);

-- RLS: docs, folders, objects, trash_items, llm_usage
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY; ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON <t> USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
                         WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid);
```

- 인덱스: 목록은 `(user_id, key)` PK의 범위 질의 `key >= $prefix AND key < $prefix || U&'\FFFF'`(COLLATE "C"라 btree 사용).
  사용자당 문서 수백 개 규모라 추가 인덱스 없음. `llm_usage`만 한도 조회용 `(user_id, at)`.
- `users`는 RLS 없음: 로그인 전 이메일 조회가 필요. `ytc_app`은 `users`에 SELECT·UPDATE(google_sub, display_name)만.
  사용자 추가·비활성은 `admin.py`가 `ytc_owner`로.
- `trash_items.raw`(문서) / `s3_key`(객체 `trash/{uid}/{stamp}/{key}`). 만료(30일)는 S10.
- `s3_key` CHECK: `objects`는 정확히 `users/{user_id}/{key}`, `trash_items`는 NULL 또는 `trash/{user_id}/{stamp}/{key}`.
  RLS가 행을 막아도 행 안의 `s3_key`가 남의 MinIO 객체를 가리키면 `/files`·휴지통 복원이 그 바이트를 읽는다 — 행과 객체 위치를 DB에서 묶는다.
- 롤 안전(마이그레이션과 `Db.open()`이 `db_role_unsafe`로 거부): 앱 롤 = owner, owner가 슈퍼유저·BYPASSRLS가 아님,
  앱 롤이 슈퍼유저·BYPASSRLS, 앱 롤이 다른 롤의 멤버(`pg_auth_members`: SET ROLE·`pg_read_all_data` 등으로 우회).
  앱 롤 비밀번호는 클라이언트에서 SCRAM-SHA-256으로 해시해 `ALTER ROLE … PASSWORD '<검증자>'`로 보낸다(평문이 문장·로그에 남지 않음).

### 3.2 키 → 저장 위치 (모든 `keys.kind`)

| keys.kind | 키 예(ADR-1) | 서버 저장 | 비고 |
|---|---|---|---|
| doc | `refs/ref_<cat>/videos/<vid>.md`, `…/index.md`, `…/techniques.md`, `drafts/<n>.md`, `drafts/<n>-conti.md`, `mine/*.md`, `missions/*.md`, `reviews/*.md`, `profile.md` | `docs(user_id, key)` | 폴더는 `folders` |
| object | `drafts/<n>-conti.html`, `-{captions,lines}.srt`, `reports/*.html`, `workspace/typify/<cat>*.json`, `workspace/conti_history/<stem>/NNNN.json`, `workspace/conti-art/<stem>/*.svg`, `workspace/footage_match/*.json`, `workspace/footage/<fid>/…`(가져오기로만), `yt-coach.json`(llm 절 무시), `.gitignore` | `objects` 행 + MinIO `users/{uid}/{key}` | 키를 그대로 객체 이름 뒤에 붙인다(변환 없음, `objects.s3_key` CHECK가 강제). 상위 §3.3의 `projects/{pid}` 배치는 projects 테이블이 생길 때 |
| object(비활성 기능) | `drafts/<n>-captions.mp4`, `-cuts/`, `-remaining.html`, `-footage.csv`, `library.json` | 위와 같음(가져오기는 함), 새로 쓰는 경로는 §8로 막힘 | |
| shared | `workspace/raw/<vid>.json`, `workspace/cuts/<vid>.json`, `workspace/shots/<vid>.json`, `workspace/frames/<vid>/<nnnn>.jpg` | `shared_objects` 행 + MinIO `shared/videos/{vid}/{raw.json\|cuts.json\|shots.json\|frames/<name>}` | 변환은 `s3store.shared_s3_key(key)` 한 함수. 문자열은 데이터 안에 키 그대로 남는다 |
| scratch | `workspace/tmp/<name>` | 프로세스 임시 폴더 `/tmp/ytc/{uid}/` | 같은 프로세스 안 HTTP→작업 핸드오프(ADR-S6이라 성립) |
| (Scratch.local_path) | `timings.json`, `.last_fetch`, `library/` | `timings`·`.last_fetch`: `/var/lib/ytc/state/`(서버 전역, 볼륨) / `library`: NotImplementedError | `.last_fetch` = 서버 전체 YouTube 속도 제한(상위 §4) |
| trash | `workspace/trash/<stamp>[-n]/…` | `trash_items` + MinIO `trash/{uid}/{stamp}/{key}`(`trash_items.s3_key` CHECK) | 반환 문자열 형식 동일. 구현(T8): 한 트랜잭션(사용자 잠금 → 행·객체 키 잠금 → 서버 쪽 COPY → 행 이동) 커밋 뒤 원래 객체 삭제(실패하면 고아, 경고 로그). 폴더 키는 그 아래 문서·객체·폴더 행째 |
| (밖) | `ui.lock`, `ui.log`, 촬영 원본 폴더, `remotion/`, `templates/`, `skills/` | 없음 / 이미지 안 `PLUGIN_ROOT` | |

### 3.3 Protocol 준수 규칙 (계약 테스트가 고정)

- **바이트 왕복**: `docs.raw`·MinIO 객체는 받은 바이트 그대로. `Doc.raw`, `version = sha256(raw)` 동일.
- **없는 키**: `FileNotFoundError`(ADR-3). 종류가 틀린 키: `ValueError`. 키 검사: `keys.check` 먼저(bad_path).
- **write(expect_version)**: 한 트랜잭션에서 `SELECT raw, version … FOR UPDATE` → 버전 다르면 `VersionConflict`
  (행이 없을 때의 의미는 FileDocStore와 같게, 계약 테스트로 정한다) → `refstore.check_memo(old, new)` 위반이면 `memo_lost`, 롤백
  → `INSERT … ON CONFLICT (user_id, key) DO UPDATE` → 조상 폴더 `folders` upsert → 새 version 반환.
- **create**: `INSERT … ON CONFLICT DO NOTHING RETURNING version`, 행 없으면 `FileExistsError`(O_EXCL 의미).
- **list(prefix, pattern)**: prefix 바로 아래만(키에서 prefix를 뗀 나머지에 `/` 없음), `fnmatch`는 **대소문자 무시**,
  순서는 `sorted(keys, key=PureWindowsPath)`. 이유: 원본 작업 폴더가 Windows에서 만들어졌고 FileStore(Windows)와 같은 순서·일치를 내야
  가져온 뒤 목록이 같다. `keys.is_doc`도 대소문자 무시라 일관된다. (리눅스 개인판 컨테이너의 FileStore는 대소문자 구분 — §10 위험)
- **folders(prefix, pattern)**: `folders` 행 중 prefix 바로 아래 폴더 + 문서·객체 키에서 유도한 바로 아래 폴더의 합집합, 같은 일치·순서.
- **folder_exists / ensure_folder**: `folders` 행 존재 또는 그 아래 키 존재. ensure는 조상까지 upsert. 파일 삭제·휴지통 이동은 폴더 행을 지우지 않는다(디스크와 같음).
- **Meta**: `mtime = mtime_ns / 1e9`, `mtime_ns = time.time_ns()`(쓰기 시각), `size = len(raw)`.
- **label()** = `ytc:{email}`(사용자마다 다름 → `conti_history` 캐시 키 `(label(), key)` 충돌 없음), `Doc.label` = 키, `display_path` = 키.
- **local_path(key)**: 임시 파일에 바이트를 써서 yield, 끝나면 삭제. **write_path(key)**: 임시 경로 yield → 정상 종료 시 업로드, 예외면 버림.
  폴더 키(`frames_dir`, `footage_dir`, `-cuts/`)는 임시 폴더 yield → 정상 종료 시 `delete_prefix` 후 하위 파일 전부 업로드(이름순).
  업로드 전에 모든 파일의 키·객체 이름·크기(64MB)를 먼저 검사해, 하나라도 안 되면 예전 내용을 지우지 않고 멈춘다.
  **주의(T7)**: 서버의 폴더 `write_path`는 그 폴더를 통째로 비운다(FileStore는 비우지 않는다). 지금 비활성 기능의 두 호출부가
  여기에 걸린다 — `footage.decode_scenes`(`write_path(footage_dir(fid))`: 앞서 쓴 `scenes.json`·`transcript.json`까지 지운다),
  `footage_out.export_clips`(`write_path(cut_files_dir)`로 폴더만 만든 뒤 컷 파일을 따로 `write_path`: 같은 폴더의 다른 파일을 지운다).
  footage(§8)를 켜기 전에 두 호출부의 키를 실제로 쓰는 하위 폴더(`…/frames/`·`…/montage/`)·파일 키로 좁히거나 이 규칙을 바꾼다.
- **put 순서**: MinIO PUT → 인덱스 행 upsert(같은 트랜잭션 밖이라도 "행이 있으면 객체가 있다"가 유지). **delete**: 행 삭제 → 객체 삭제(실패하면 고아, S10 점검).
  구현(T7): 같은 키의 put·delete는 `pg_advisory_xact_lock(class, hashtext(uid/key))`로 한 번에 하나씩, 새 키의 행 upsert가 실패하면
  올린 객체를 지운다. `delete_prefix`는 지울 키마다 잠금을 하나씩(키 순서) 잡는다 — 지금 호출부(프레임 ≤ 40장, 이력·컷 폴더 수십 개)에는
  충분하고, 수천 개 키의 접두사를 지우게 되면 put·delete도 함께 잡는 접두사 잠금 하나로 바꾼다.
- **SharedCache 쓰기**: 누구나 덮어쓸 수 있다(원본 데이터, 교정은 사용자 md에). 같은 영상 수집 경합은 advisory lock으로
  수집 단계에서 직렬화. `Trash.move`는 shared 키를 옮기지 않고 `moved`에서 뺀다(다른 사용자 데이터).
  구현(T11): 코어 이음매 `limits.collecting(vid)`(로컬 no-op)가 `fetch.fetch`(캐시 확인 → 메타·자막 → `save_raw`),
  `cuts.cut_stats`(캐시 확인 → 프레임 폴더 비우기 → 다운로드·분석 → `cuts.json`), `stt.transcribe`(raw 읽기 → STT → `save_raw`)를 감싼다.
  서버는 `Db.collect_lock`: 앱 롤로 **풀 밖 연결 하나**를 열어 세션 잠금 `pg_advisory_lock(bigint)`(키 = sha256("ytc:collect:"+vid) 앞 8바이트) —
  몇 분짜리 다운로드가 풀 연결을 붙잡지 않고, 연결이 죽으면 PG가 푼다. bigint 한 개 형식이라 T7 객체 잠금(int 두 개)과 키 공간이 겹치지 않는다.
  같은 스레드의 재진입은 통과. 뒤에 온 쪽은 "다른 사용자가 같은 영상을 받는 중"(진행 단계 `wait`)을 보고, 앞쪽이 끝나면 캐시를 쓴다.
  기다림은 `pg_try_advisory_lock`을 1초마다 다시 묻고 900초(`Db.collect_wait_max`)가 지나면 `collect_busy`("다른 사용자가 같은 영상을 오래 받고
  있습니다. 잠시 뒤 다시 시도하세요.")로 그 영상만 실패한다. 잠금 연결은 `connect_timeout=10`·TCP keepalive 60초. 기다리는 동안 작업 취소는 보지
  않는다(코어에 작업 문맥이 없다) — 취소는 잠금을 얻거나 포기한 뒤 다음 `check_cancelled`에서 걸린다(최대 900초 늦음).
- **프로세스 잠금**(`INDEX_LOCK`, `HISTORY_LOCK`, `_RELABEL_LOCK`): 프로세스가 하나라 그대로 유효(사용자 간 과잉 직렬화는 수십 명 규모에서 허용).

### 3.4 Scratch (서버)

`tmp_path(name)`·`tmp_file(key)` → `/tmp/ytc/{uid}/<name>`(시작 때 비움). `arg_path`: 절대 경로는 tmp 폴더 안만, 상대는 tmp 기준.
`key_of`: 문자열 키는 `keys.check`, tmp 안 경로는 `workspace/tmp/…`, 그 밖은 `bad_path`. `workdir()`·`local_path("library")`: NotImplementedError.
구현(T8): `local_path("tmp")` = 그 사용자의 tmp 폴더(cuts·stt·미션 정리가 쓴다), `timings.json`·`.last_fetch` = `YTC_STATE_DIR`,
그 밖은 전부 NotImplementedError. 상대 `key_of`는 FileScratch의 resolve처럼 `./`·`a/../`를 접은 뒤 `keys.check`. 시작 때 비우기는
`clear_scratch_root`(이름이 UUID인 하위 폴더만 지운다).

### 3.5 MinIO

버킷 1개(`YTC_S3_BUCKET`, 기본 `ytc`), 접두사 `users/{uid}/`, `shared/videos/{vid}/`, `trash/{uid}/{stamp}/`. 버전 관리는 v1에서 끈다
(용량, S10에서 결정). 코드는 S3 API(boto3)만 써서 `YTC_S3_ENDPOINT`만 바꾸면 S3·R2로 옮긴다. 객체 크기 상한 64MB(옛 콘티 HTML 46MB 수용).

### 3.6 코어 이음매(S5)

- `stores.Backend`에 `usage(user_id) -> UsageMeter` 추가, `Ctx.usage` cached_property(backend 없으면 `NullMeter`).
- `Ctx(root=None, user_id, backend)` 그대로 사용. 서버판은 `common.work_root()`를 부르는 허용 목록 경로(`ui/__main__`, `/files`)를 타지 않는다.

## 4. S6 서버 API·인증

### 4.1 어댑터 (`server/asgi.py`)

1. `scripts/ui/server.py`에서 매칭·결과 정규화를 순수 함수로 뺀다(동작 동일): `match(method, path) -> (Route, groups) | None`,
   `call(rt, req, groups) -> Response`(ScriptError→400, 예외→500, dict/tuple→JSON 바이트, 지금 `_send` 헤더 규칙 포함). stdlib 핸들러도 이것을 쓴다.
2. Starlette 캐치올 라우트: 인증 → 기능 가드(§8) → `Request(method, path, query, json, headers, app=UserApp)` 생성 →
   `anyio.to_thread.run_sync`에서 `with ctx.use(Ctx(user_id=uid, backend=BACKEND)): call(...)` → `Response.file`이 있으면 StreamingResponse.
   핸들러가 시작한 작업 스레드는 `copy_context()`로 Ctx를 이어받는다(S2 그대로).
3. `UserApp(root=None, token="", port=0, server=None, jobs=registry.for_user(uid))`. 핸들러가 쓰는 `req.app.*`는 `jobs`뿐(나머지는 server.py 기본 라우트).
4. 서버가 덮어쓰는 기본 라우트: `GET /`(로그인 안 됐으면 `/auth/login`으로 302, 됐으면 index.html), `/files/<key>`(§4.3),
   `GET /api/ping`(`{"ok", "edition": "server", "user": email}`), `POST /api/shutdown`(404), `GET /healthz`(PG·MinIO 확인, 인증 없음).
5. 정적 파일: `scripts/ui/static`을 같은 출처 `/static/`으로(인증 필요, 지금과 같음). 본문 상한 10MB 그대로.
6. `api.js`: 401 `login_required`면 `/auth/login`으로 이동(로컬판은 401을 내지 않으므로 영향 없음).
7. 구현(T10): 경로는 `scope["raw_path"]`(퍼센트 인코딩 그대로, 쿼리 제외) = stdlib의 `urlsplit(self.path).path`라 가드와 `match`가 같은 문자열을 본다.
   순서: `/healthz` → CSRF → `/auth/*` → 세션(요청마다 users 조회, Ctx 전) → 덮어쓴 기본 라우트 → `match` → 가드 → 본문 → 작업 스레드.
   판 문맥은 `scripts.ui.server.edition("server", 끌 기능)`(ContextVar, 요청 스레드에서 `ctx.use`와 함께 건다 → 작업 스레드도 이어받음).
   `disabled_features()`는 env ∪ 판 문맥이라 `/api/features`(edition 포함)와 코어 쪽 라이브러리 출처(`_library_templates`, 도움말 상황)가 같은 답을 낸다.
   라이브러리 폴더가 없는 저장소(서버 scratch)에서 `library.clone_dir()`은 `library_unavailable`(400)이라 `library:<id>` 기획안 출처·모티브 `library_ids`도 500이 아니라 400.
   `common.scrub`은 작업 폴더가 없으면(서버) 절대 경로 가리기만 한다(이전에는 `work_root()`가 RuntimeError).
   응답 헤더: 표준 헤더 + CSP(앱 `APP_CSP`, `/files`는 스크립트 없는 `FILES_CSP` — 리포트 iframe 때문에 `frame-ancestors 'self'`,
   `sandbox allow-same-origin`(스크립트·폼·팝업 없음, 출처는 유지해 `img-src 'self'`가 그림에 맞는다), SVG는 `SVG_CSP`) + https면 HSTS.
   500 응답 본문은 고정 문구("서버 오류가 났습니다. 잠시 뒤 다시 시도해 주세요"), 자세한 내용은 stderr 로그에만(로컬판은 그대로).
   `/files`의 저장소 오류(ScriptError·OSError)도 같은 JSON 오류 모양. 모르는 메서드(HEAD·OPTIONS·FOO …)도 dispatch로 와서 JSON 404(비로그인 401).
   시작: `server.asgi:app`은 lifespan 시작 때 한 번만 앱을 만들고, 실패하면 `lifespan.startup.failed`. 시작 전 http 요청은 503 `starting`.
   `YTC_ENV`는 production·dev·test만(그 밖 `config_invalid`).
   상태를 바꾸는 요청은 `Registry.close()` 뒤 503 `restarting`. `/auth/login`은 `server/web/login.html`+`login.js`(인라인 스크립트 없음),
   `/auth/methods`가 dev 로그인 여부를 알려 준다(Google 버튼은 T12).

### 4.2 인증·세션·CSRF (`server/auth.py`)

| 항목 | 결정 |
|---|---|
| 로그인 | Google OIDC Authorization Code + PKCE(authlib Starlette client), scope `openid email profile`. `/auth/login` → Google → `/auth/callback` |
| 허용 | `email_verified=true`이고 `users`에 있는 이메일만(초대제, `admin add-user`). 처음이면 `google_sub` 저장, 이후 sub가 다르면 거부. `disabled`면 거부 |
| 세션 | Starlette `SessionMiddleware`(itsdangerous 서명 쿠키 `ytc_session`), 내용 `{uid, epoch, iat}`. `HttpOnly; Secure; SameSite=Lax; Path=/`, 만료 14일. 요청마다 users 조회로 `disabled`·`session_epoch` 확인(사용자 수십 명, 캐시 불필요) |
| 로그아웃 | `POST /auth/logout` → 세션 비움. 전체 무효화는 `admin disable-user` 또는 epoch 증가 |
| CSRF | 안전하지 않은 메서드(POST·PUT·DELETE)는 ① `Origin`이 `YTC_PUBLIC_URL`의 출처와 같아야 하고(없으면 `Sec-Fetch-Site: same-origin` 요구) ② `Content-Type: application/json` 필수(api.js가 이미 항상 보냄 → 단순 폼 교차 전송 불가). SameSite=Lax가 한 겹 더 |
| 보안 헤더 | 기존 `nosniff`, `no-store`, `Referrer-Policy` + `Strict-Transport-Security`(HTTPS일 때), `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'` (정적 JS가 인라인 스크립트를 쓰면 T10에서 확인 후 조정), SVG CSP 유지 |
| dev 로그인 | `YTC_DEV_LOGIN=1`일 때만 `POST /auth/dev {"email"}`(users에 있는 이메일). `YTC_ENV=production`이면 설정 자체를 시작 시 거부 |
| 시크릿 | `YTC_SESSION_SECRET`(32바이트 이상), `GOOGLE_CLIENT_ID/SECRET`, `ANTHROPIC_API_KEY`, DB·S3 비밀번호는 env만. 로그에 쿠키·키를 남기지 않는다(접근 로그는 경로만, 쿼리 제외). uvicorn 기본 접근 로그는 쿼리 문자열(OIDC `code`·`state` 등)까지 찍으므로 `--no-access-log`로 끄거나 쿼리를 지우는 log config를 준다(T13이 연결) |
| TLS | 앱은 HTTP(내부 127.0.0.1:8080). 외부는 Caddy(자동 TLS) 또는 Tailscale 등 사설망 — §11 사용자 결정 |
| 구현(T10) | `YTC_ENV` 기본 production. production이면 `YTC_PUBLIC_URL`은 https만(쿠키 `Secure`·HSTS), 개발(`YTC_ENV=dev` 등)의 http 주소는 `Secure` 없이(파이썬 스모크가 http로 쿠키를 보내게). `YTC_SESSION_SECRET` 32바이트 미만·`YTC_DEV_LOGIN` 0/1 밖 → `config_invalid`. 사용자 조회는 `Db.public_tx()`(app.user_id 없음: RLS 테이블 0행). 세션 `{uid, epoch, iat}`, iat 뒤 14일(+시계 60초) 밖·비활성·epoch 다름·없는 사용자면 세션을 비우고 401. Origin 비교는 소문자·끝 `/` 제거 후 출처 일치(`null` 거부) |

### 4.3 `/files/<key>` (`server/files.py`)

`keys.check` → `FILE_DIRS` 접두사 검사(지금과 같음: `workspace/frames`, `workspace/conti-art`, `drafts`, `reports`) → `keys.kind`로 저장소 선택
(shared는 로그인 사용자 누구나 — 공개 YouTube 영상 프레임, 그 밖은 자기 RLS 행) → 없으면 404 → MinIO GET(Range 헤더 전달, 206/416) 스트리밍,
`content_type`·SVG CSP 재사용. 남의 키는 RLS 때문에 "없음"과 똑같이 404.
구현(T10): Range는 로컬처럼 영상(`VIDEO_SUFFIXES`)만, `S3Bucket.open_range`(S3 ranged GET, 1MB씩 스트리밍), 끝 없는 `bytes=N-`는 `MAX_OPEN_RANGE`.
폴더 키(끝 `/`)·`workspace/tmp`는 404. md 문서(`drafts/*.md`)도 로컬처럼 내준다.

### 4.4 설정(env, `server/config.py`)

`YTC_EDITION=server`, `YTC_ENV`, `YTC_PUBLIC_URL`, `YTC_SESSION_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `YTC_DB_DSN`(ytc_app),
`YTC_DB_OWNER_DSN`(migrate·admin만), `YTC_S3_ENDPOINT`, `YTC_S3_BUCKET`, `YTC_S3_ACCESS_KEY`, `YTC_S3_SECRET_KEY`, `ANTHROPIC_API_KEY`,
`YTC_LLM_ENGINE=anthropic-api`, `YTC_LLM_MODEL`(기본 claude-sonnet-5-5, 가격표에 있어야 함), `YTC_LLM_MAX_TOKENS`(호출당 상한, 기본 16000), `YTC_DAILY_USD`(기본 1.00), `YTC_MEDIA_SLOTS`(2), `YTC_LLM_SLOTS`(4),
`YT_PROXY`, `YT_FETCH_INTERVAL`, `YTC_DEV_LOGIN`. 필수 값이 없으면 시작하지 않는다(`config_missing`).
저장소(T8, `StorageSettings`): `YTC_DB_POOL_MIN`(1)·`YTC_DB_POOL_MAX`(10), `YTC_S3_MAX_POOL_CONNECTIONS`(10, boto 연결 풀),
`YTC_SCRATCH_DIR`(`/tmp/ytc`), `YTC_STATE_DIR`(`/var/lib/ytc/state`). 잘못된 값은 `config_invalid`.

## 5. S7 작업 실행

### 5.1 구조

- `jobs_registry.Registry`: `for_user(uid) -> JobManager`(없으면 만든다, 잠금 안). `SINGLE_KINDS`·`EXCLUSIVE`가 **사용자별**로 자동 적용.
- 진행 표시·awaiting·decide·cancel·`take_review`·작업 줄(`push_task`)은 같은 프로세스 메모리라 **코드 변경 없이** 그대로 동작. 화면은 지금처럼 폴링.
- 남의 job_id로 조회하면 그 사용자의 JobManager에 없으므로 `job_not_found`(격리 테스트 대상).
- 종료: SIGTERM → 새 작업 거절(503 `restarting`) → `registry.shutdown(timeout=60)`(취소) → 종료. compose `stop_grace_period: 90s`.
  재시작하면 진행 중·확인 대기 작업은 사라진다(v1 수용, 화면에 "서버가 다시 시작되어 작업이 멈췄습니다" 표시). 저장된 md는 영향 없음.

### 5.2 전역 자원 제한 (코어 이음매 `scripts/limits.py`)

```python
def slot(name: Literal["media", "llm"]) -> ContextManager[None]   # 기본 no-op(로컬판). 서버가 시작 때 install(media=Semaphore(n), llm=Semaphore(m))
```
| 종류 | 어디서 잡나 | 서버 기본 | 근거 |
|---|---|---|---|
| media | 영상 다운로드+컷·프레임 추출(cuts/OpenCV), STT(PyAV 디코드), 샷용 프레임 처리 | 2 | 측정(T11, 아래) 결과 2를 유지: 2개 동시가 처리량 이득의 대부분이고 4개는 거의 늘지 않는다. 4 vCPU·8GB VM에서 UI 응답 여지를 남긴다 |
| llm | `llm.ask` 한 번 | 4 | 운영자 키 rate limit·비용 폭주 방지 |
| fetch | 기존 `.last_fetch` 간격(`YT_FETCH_INTERVAL`) + 프로세스 잠금 | 서버 전체 1개 | YouTube 차단 대비(상위 §4) |

자리를 기다리는 동안 항목 `message="다른 사용자 작업이 끝나기를 기다리는 중"`. 같은 영상 수집 중복은 §3.3 advisory lock.

구현(T11):
- 설치: `server.jobs_registry.install_runtime(LlmSettings, db)`를 `asgi.from_env`가 앱을 만들 때 한 번 부른다(`create_app(runtime=…)`,
  lifespan 종료 때 `uninstall_runtime`). media·llm `Semaphore(YTC_MEDIA_SLOTS·YTC_LLM_SLOTS)`, 수집 잠금(§3.3), `llm.pin`(§6).
- fetch: `fetch.wait_turn`이 프로세스 잠금 안에서 **순번을 예약**한다(`.last_fetch` = max(지금, 마지막 예약 + 간격)을 쓰고 기다림은 잠금 밖).
  같은 프로세스의 작업 스레드들이 같은 `.last_fetch`를 읽고 동시에 받던 경합이 없어진다. 서버는 프로세스 하나 + `.last_fetch`가
  `YTC_STATE_DIR`라 이것이 서버 전체 1개다. 로컬판은 한 스레드면 예전과 같다(예약 시각 = 기다린 뒤 시각). STT 음성 받기(`stt.download_audio`)는
  예전처럼 간격 밖이다(로컬 동작 유지).
- llm 슬롯 대기 문구는 넣지 않았다: `llm.ask`에는 진행 채널이 없고(호출부 27곳이 작업 항목·단계를 따로 관리), 한 호출은 수십 초라
  기다림이 짧다. 필요하면 작업 문맥 ContextVar + `limits.slot(on_wait)`로 붙인다.
- media 측정(2026-11-01, Windows 20코어 개발 PC, 합성 360p 60초 30fps 영상, 장면 3초마다, `cuts.analyze_video`만 — 다운로드·STT 제외):
  1개 6.5초·CPU 1.3~1.5코어·RSS +45MB, 2개 동시 9.0초(영상당 4.5초, 2.6코어), 4개 동시 17.4초(영상당 4.35초, 2.75코어, RSS +120MB).
  장면 감지가 GIL을 많이 잡아 3개 이상은 처리량이 거의 늘지 않고 각 작업만 느려진다. 설계 초안의 RAM 300~500MB는 다운로드·STT(Whisper 모델)를
  포함한 추정이라 분석만으로는 훨씬 작다. VM에서 실제 영상으로 다시 재는 것은 T13 스모크(`--with-youtube`)에서 한다.

### 5.3 어디서 도나

| 일 | v1 실행 위치 |
|---|---|
| YouTube 수집(yt-dlp, 자막) | app 프로세스 작업 스레드(+fetch 제한, `YT_PROXY`) |
| 컷·프레임(OpenCV), STT(PyAV) | 작업 스레드 + media 슬롯(C 확장이 GIL을 놓는 구간이 대부분) |
| LLM(분석·카드·기획·콘티·미션 등) | 작업 스레드 + llm 슬롯 + 사용량 한도 |
| 자막 영상·촬영본·컷 파일·srtfit | 비활성(ADR-S11) |

### 5.4 S7b로 넘기는 조건(기록만)

media 대기 p95가 5분을 넘거나, 앱 재시작으로 작업을 잃는 일이 주 1회 이상이면 `jobs` 테이블 + `media` 워커 프로세스로 분리한다.

## 6. S8 최소: 사용량·일 한도

- `UsageMeter`(Protocol): `check(job_kind) -> None`, `record(model, usage: dict, ok: bool) -> None`. 로컬판 `NullMeter`.
- 코어 이음매: `llm.ask`가 엔진 호출 전 `ctx.here().usage.check()`, 호출 후 `record(...)`. `llm_anthropic.ask`는 응답의 `usage`
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`)를 `options["on_usage"]` 콜백으로 넘긴다
  (반환 dict 형식은 그대로). 실패 응답도 usage가 있으면 기록(`ok=false`).
- 비용: `server/usage.py` 가격표(모델 id → 백만 토큰당 입력·출력·캐시 단가)를 **구현 시점 Anthropic 공식 가격**으로 채운다. 가격표에 없는
  모델이면 시작을 거부한다(비용 모르는 호출 금지). 비용은 `numeric`으로 저장.
- 한도: 오늘(Asia/Seoul 0시 기준) `sum(cost_usd) >= coalesce(users.daily_usd_limit, YTC_DAILY_USD)`이면 거절. 한 호출이 넘칠 수 있으므로
  `YTC_LLM_MAX_TOKENS`로 호출당 상한을 둔다. 월 한도는 같은 질의로 S8 정식판에서.
- 구현(T11):
  - 서버 LLM 고정(ADR-S10): `llm.pin(engine, model, max_tokens)` — `llm.config()`가 사용자 `yt-coach.json`·env를 읽지 않는다.
    `YTC_LLM_ENGINE`은 `anthropic-api`만, `YTC_LLM_MODEL` 기본 `llm_anthropic.DEFAULT_MODEL`(claude-sonnet-5-5), 가격표에 없으면 `config_invalid`로 시작 거부.
    `YTC_LLM_MAX_TOKENS`(기본 16000 = 코드의 최대 요청 `conti_art.MAX_TOKENS`)는 **상한**: 호출 값(없으면 엔진 기본 8000)과 상한 중 작은 값.
    `ANTHROPIC_API_KEY`는 시작 때 요구하지 않는다(없으면 호출 때 기존 `llm_not_configured`). production에서 비어 있으면 시작 때 경고 로그(`ytc.server`).
  - 가격표 `server.usage.PRICES`(2026-09-25 공식 가격, USD/MTok): 입력·출력·캐시 읽기(모델별: Opus 5.5 $0.20, Sonnet 5.5 $0.20, Fable 5.1 $0.25, 그 밖 입력의 0.1배)·
    캐시 쓰기(5분 TTL = 입력의 1.25배; 엔진이 `cache_control`을 쓰지 않음). 비용은 numeric(10,6) 반올림.
  - `check()`·호출·`record()`를 모두 llm 슬롯 **안에서** 한다(`llm.ask`). 검사가 DB 오류면 그 오류를 올린다(비용 모르는 호출 금지).
    `record()`는 최선만: DB 오류·가격 없음은 `ytc.usage` 로그로 남기고 올리지 않는다 — LLM 결과나 원래 오류를 가리지 않는다. 기록 못 한 비용
    (가격을 모르면 가격표 필드별 최고 단가로 잡은 값)은 프로세스 안 장부(`UnrecordedLedger`, `ServerBackend` 하나에 하나, 사용자·KST 날짜별)에 남아
    `check()`의 오늘 사용에 더해진다. 재시작하면 장부는 사라진다.
    `at`은 검사와 같은 시계(앱 시각)로 쓴다. `job_kind`는 지금 NULL(`llm.ask`가 작업 종류를 모른다).
  - **초과 한계**: 검사를 통과하고 아직 기록되지 않은 호출은 프로세스 전체에서 `YTC_LLM_SLOTS`개 이하다. 그래서 한 사용자의 하루 사용은
    최대 `한도 + k × C_max`, k = 그 사용자의 동시 LLM 호출 수(≤ `YTC_LLM_SLOTS`, 기본 4), C_max = 호출 하나의 최대 비용 =
    입력 토큰 × 입력 단가 + `YTC_LLM_MAX_TOKENS` × 출력 단가. 기본값(Sonnet 5.5, 16000)에서 출력 몫은 호출당 $0.16, 입력 5만 토큰이면 $0.10 →
    최악 약 $1.04 초과. 더 조이려면 `YTC_LLM_SLOTS`·`YTC_LLM_MAX_TOKENS`를 낮춘다. 기록 실패도 장부로 잡히므로 한계 안이다(재시작 전까지).
- 오류(400, 화면은 기존 오류 표시 그대로):
  `ScriptError("llm_limit", "오늘 쓸 수 있는 AI 사용량(1.00달러)을 다 썼습니다. 내일 0시(한국 시간)에 다시 쓸 수 있습니다. 오늘 사용: 1.02달러", used=1.02, limit=1.00, resets_at="2026-11-02T00:00:00+09:00")`
- 관리: `python -m server.admin usage [--days 7]` → 사용자별 일 합계 JSON. 관리자 화면은 S8 정식판.

## 7. S9 최소: 가져오기

`python -m server.admin import <작업 폴더> --user <email> [--dry-run] [--overwrite]` (owner DSN으로 사용자 확인 후, 저장은 `user_tx(uid)` = RLS 경로)

1. 작업 폴더를 걸어(`admin.py` 허용 목록) 모든 파일의 POSIX 상대 경로 = 키. `keys.check` 실패는 `skipped_bad_key`로 보고.
2. 제외: `workspace/tmp/`, `workspace/trash/`, `workspace/library/`, `workspace/library.lock`, `workspace/ui.lock`, `workspace/ui.log`,
   `workspace/timings.json`, `workspace/.last_fetch`, `.git/`, `library.json`(비활성 기능, 다음 판에서).
3. `keys.kind`별: doc → `PgDocStore.import_raw(key, raw, mtime_ns)`(Protocol 밖 관리 메서드: **원래 mtime 보존** — `api_refs` "added",
   미션·진단 mtime 정렬이 바뀌지 않게), object → `S3ObjectStore.import_raw`, shared → 이미 있으면(sha 같음/다름 모두) 건너뜀, 없으면 넣음.
   빈 폴더 → `folders`. 같은 sha면 건너뜀(재실행 안전), 다르면 `--overwrite` 없이는 `conflict`로 보고만.
4. 출력 JSON: `{"user", "docs": n, "objects": n, "shared_new": n, "shared_skipped": n, "skipped": {...}, "conflicts": [...]}`.
5. 구현(T9): 쓰기 메서드는 `import_raw(key, raw, mtime_ns, *, overwrite, dry_run) -> new|same|replaced|conflict|skipped`(PgDocStore·S3ObjectStore·S3SharedCache,
   키마다 트랜잭션 하나라 중간에 멈춰도 재실행하면 이어진다). 출력 JSON은 위 4번에 `dry_run`·`folders`(새 폴더 수)·`unchanged`·`excluded`(걸린 제외 규칙)·
   `changes`(docs+objects+shared_new+folders, 재실행 0)·`verify`를 더한다. `skipped`는 `bad_key`·`too_large`(객체 64MB 초과)·`not_regular`(링크 등).
   읽을 수 없는 파일은 건너뛰지 않고 `import_unreadable`로 멈춘다(재실행하면 이어서). `--no-verify`로 검증을 끈다. `add-user`·`disable-user`·`usage`는
   `YTC_DB_OWNER_DSN`만 쓰고 가져오기만 저장소 설정 전부를 쓴다. `--overwrite`로 덮어써도 서버 문서의 `## 내 메모`가 바뀌면 건드리지 않고 conflict(reason `memo`)로 보고한다(내 메모 가드). 메모까지 되돌리려면 `--overwrite-memo`. 충돌이 있으면 종료 코드 0, 보고서 `ok:false`+`message`, 검증 `skipped_conflicts`. 비활성 사용자에게는 가져오지 않는다. 문서도 64MB 상한.
   서버 `Meta.mtime`은 `mtime_ns/1e9`라 로컬 `st_mtime`과 마지막 자리가 달라, 검증의 JSON 비교는 실수 1ms 허용.
   `export_tree`는 `plan_tree(...)`(키·폴더 먼저, 바이트는 키마다 읽는 `items()`·`hashes()`·`write()`)의 `collect()`다.
6. 검증: 가져온 뒤 같은 프로세스에서 로컬 `Ctx(root=폴더)`와 서버 `Ctx(user, backend)`로 `status.overview()`를 각각 계산해
   `work_root`·라벨을 뺀 JSON이 같은지 비교, 다르면 종료 코드 1. 실데이터(레퍼런스 20·모티브 22·기획안 6·콘티 이력)는 수동 확인 1회.

## 8. 비활성 기능과 화면

- `GET /api/features` (두 판 공통, `scripts/ui/server.py`): `{"edition": "local"|"server", "disabled": {"<기능>": "<문구>"}}`.
  로컬 native는 비어 있고, 개인판 컨테이너는 `YTC_DISABLE=library,caption_video`(git 자격 증명·Remotion 없음), 서버판은 아래 전부.

| 기능 | 막는 API 접두사(어댑터 가드, 403 `feature_disabled`) | 문구 |
|---|---|---|
| library | `/api/library` | 공용 라이브러리는 서버판에서 아직 쓸 수 없습니다 |
| caption_video | `/api/conti/…/caption-video`류(구현 때 표에 정확한 패턴 고정) | 자막 영상 만들기는 내 PC 판에서만 됩니다 |
| srtfit | srtfit 시작 라우트 | 자막 시간 맞추기는 내 PC 판에서만 됩니다(영상 파일 필요) |
| footage | `/api/footage` 전부 | 촬영 영상 처리는 내 PC 판에서 하고, 결과는 가져오기로 옮기세요 |

- footage를 켜려면 먼저 §3.3 주의(폴더 `write_path`가 폴더를 비움: `footage.decode_scenes`, `footage_out.export_clips`)를 해결한다.
- 구현(T10): 서버판은 env와 상관없이 표의 네 기능을 모두 끈다(`server.asgi.SERVER_FEATURES`, `YTC_DISABLE`는 더할 수만 있다).
  화면: `#/library`(및 도움말·다른 화면의 라이브러리 링크)로 바로 와도 페이지 대신 그 기능의 문구 한 줄(`app.js` `PAGE_FEATURES`).

- 정적 JS: 버튼·메뉴에 `data-feature="<기능>"`을 달고 시작 때 `/api/features`를 읽어 숨기고 그 자리에 문구 한 줄. 가드는 서버가 최종(숨김만 믿지 않음).
- 테스트: 라우트 전수 표(ROUTES)를 돌며 서버 백엔드에서 비활성 표에 없는 라우트가 `NotImplementedError`/`Path` 접근으로 500을 내지 않는지(§9-4).

## 9. 패키징

### 9.1 Dockerfile (`deploy/Dockerfile`, 멀티스테이지)

| target | 내용 |
|---|---|
| base | `python:3.12-slim-bookworm@sha256:<고정>`, apt: `ca-certificates tini`(OpenCV는 headless 휠, PyAV 휠에 ffmpeg 내장 → 시스템 ffmpeg 없음), `pip install --no-cache-dir -r requirements.txt`, 플러그인 폴더 복사(`scripts templates skills`), 비루트 uid 10001, 빌드 검사 `python -c "import cv2, av, scenedetect, yt_dlp"` |
| local | base + `CMD ["python","-m","scripts.ui","--host","0.0.0.0","--port","8765","--no-open","--foreground"]`, `ENV YT_COACH_HOME=/work YTC_LLM_ENGINE=anthropic-api YTC_DISABLE=library,caption_video` |
| server | base + `requirements-server.txt` + `server/`, `CMD ["uvicorn","server.asgi:app","--host","0.0.0.0","--port","8080","--workers","1","--proxy-headers","--lifespan","on","--no-access-log"]` (`--lifespan on`: 앱은 lifespan 시작 때 만들어지고 설정·저장소 오류면 `lifespan.startup.failed`로 uvicorn이 끝난다. 접근 로그는 §4.2) |

- Remotion(Node·Chrome)은 두 이미지 모두 넣지 않는다. yt-dlp가 YouTube 추출에 JS 런타임을 요구하는 경우가 있어 T3 스모크(실제 링크 1개)로 확인하고 필요하면 base에 deno 단일 바이너리를 넣는다.
- 코어 이음매: `scripts.ui`에 `--host`(기본 127.0.0.1, `0.0.0.0`은 env `YTC_IN_CONTAINER=1`일 때만 허용)와 포그라운드 실행 옵션, `GET /healthz`(인증 없음, `{"ok": true}`만).
  Host 헤더 검사는 그대로 → 게시 포트와 내부 포트를 **같은 번호(8765)** 로 맞춘다.

### 9.2 개인판 `deploy/local/docker-compose.yml`

- 서비스 `ytc` 1개: `ports: ["127.0.0.1:8765:8765"]`(localhost만), `volumes: ["${YTC_WORK_DIR:-./work}:/work"]`,
  선택 `"${YTC_FOOTAGE_DIR}:/footage:ro"`(촬영 영상 처리: 화면에 `/footage/...` 경로 입력), `env_file: .env`, `user: "${YTC_UID:-1000}:${YTC_GID:-1000}"`,
  healthcheck `python -c` 로 `/healthz`, `restart: unless-stopped`.
- 열기: `docker compose logs ytc`에 나온 `?key=` 주소. 같은 작업 폴더를 native `ytc ui`와 동시에 열지 않는다(ui.lock이 컨테이너 안 pid라 서로 못 본다 — README 경고).
- `.env.example`: `ANTHROPIC_API_KEY=`, `YTC_WORK_DIR=`, `YT_PROXY=`, `YTC_LLM_MODEL=`. `.env`는 `.gitignore`.

### 9.3 서버판 `deploy/server/docker-compose.yml`

| 서비스 | 이미지·설정 | healthcheck |
|---|---|---|
| postgres | `postgres:16-alpine`(다이제스트 고정), 볼륨 `pgdata`, 포트 비공개, `POSTGRES_USER=ytc_owner` | `pg_isready` |
| minio | `cgr.dev/chainguard/minio@sha256:…`(아래 주), 볼륨 `minio`, 콘솔 포트 비공개 | `mc ready local` |
| minio-init | 같은 이미지 `entrypoint: mc`, 버킷 생성(1회) | — |
| migrate | server 이미지, `alembic upgrade head`(owner DSN, `ytc_app` 롤·비밀번호 생성 포함), `restart: "no"` | — |
| app | server 이미지, `depends_on: postgres healthy, minio healthy, migrate completed_successfully`, `ports: ["127.0.0.1:8080:8080"]`, 볼륨 `state:/var/lib/ytc/state`, tmpfs `/tmp/ytc`, `stop_grace_period: 90s` | `/healthz` |
| caddy (profile `tls`) | `caddy:2`, `Caddyfile`(`{$YTC_DOMAIN} { reverse_proxy app:8080 }`), 80·443 공개, 볼륨 `caddy_data` | — |

- 워커 서비스는 없다(ADR-S6). `docker-compose.test.yml`: postgres·minio만, 무작위 포트, tmpfs(테스트용).
- **MinIO 이미지(2026-10 확인)**: MinIO 커뮤니티 이미지는 더 이상 배포되지 않는다(`minio/minio` pull 거부, `quay.io/minio/minio` 401).
  `cgr.dev/chainguard/minio`(MinIO 소스 빌드, `mc` 포함, 비루트 65532)를 **다이제스트로 고정**해 쓴다(무료판은 `latest` 태그만).
  갱신: `docker pull cgr.dev/chainguard/minio:latest` → `docker image inspect --format '{{index .RepoDigests 0}}' cgr.dev/chainguard/minio:latest`
  → 두 compose의 `@sha256:` 교체 → 계약 테스트·smoke 재실행. 이 이미지도 못 쓰게 되면 SeaweedFS·Garage로 바꾼다 — 코드는 S3 API(boto3)만 쓰므로
  `YTC_S3_ENDPOINT`와 compose 서비스만 바뀐다(Range GET·버킷 생성은 계약 테스트로 확인).
- `.env.example`: §4.4 변수 전부 빈 값 + 생성 방법 주석(`python -c "import secrets;print(secrets.token_urlsafe(48))"`). 실제 `.env`는 커밋 금지(`.gitignore`,
  게이트 테스트가 `deploy/**/.env` 추적 여부 확인). 운영 DB·S3 비밀번호는 기본값 없이 필수.
- 첫 사용자: `docker compose run --rm app python -m server.admin add-user you@gmail.com --admin`.

## 10. 테스트 전략

1. **계약 테스트** `tests/contract/test_stores.py`: 백엔드 fixture를 `file`(tmp_path)·`pg`로 매개변수화. Protocol 전 메서드 —
   LF·CRLF·BOM·이모지·한글 이름 바이트 왕복, version, 없는 키, 키 종류 오류, `create` 중복, `expect_version` 충돌(두 스레드), 메모 가드,
   list 일치·순서(대소문자 섞음: Windows에서는 FileStore와 직접 비교, 리눅스 CI는 고정 기대 목록), folders·folder_exists·ensure_folder,
   Meta(mtime_ns 단조), local_path/write_path(파일·폴더), delete_prefix, Trash 반환 형식, SharedCache 접두사 검사.
   `pg` 매개변수는 `YTC_TEST_DB_DSN`·`YTC_TEST_S3_ENDPOINT`가 없으면 skip. 환경은 `docker compose -f deploy/server/docker-compose.test.yml up -d`
   (운영과 같은 이미지, testcontainers 의존성 추가 없음).
2. **동등성 시나리오** `tests/server/test_equivalence.py`: 같은 시나리오(scaffold·init_category → 영상 md 쓰기·relabel → 모티브 → 기획안 저장 →
   콘티 저장·이력 → 미션 give/submit → 휴지통)를 File Ctx와 Pg Ctx에서 돌리고, `export_tree(backend, uid) -> ExportedTree`
   (`.hashes()` = `{key: sha256}`, `.folders`, `.write(dest)`; S9 검증과 같은 함수)와 반환 JSON(라벨·display_path·쓰기 시각 제외)을 비교.
   허용 차이는 테스트 파일 머리말에 고정(휴지통 스탬프, 서버 휴지통은 공유 캐시를 옮기지 않음, scratch 제외). 기존 테스트 파일은 디스크를 직접 검사하므로 그대로 Pg에서 돌리지 않는다 — 대신 이 시나리오가 모듈 경로를 덮는다.
3. **격리** `tests/server/test_isolation.py`(ASGI TestClient + dev 로그인): A가 기획안·콘티 HTML·작업 생성 → B의 목록 비어 있음,
   B의 `/files/<A의 키>` 404, B가 A의 job_id 조회 `job_not_found`, B의 decide/cancel 거절. DB 직접: `ytc_app`으로 `app.user_id` 없이 SELECT → 0행,
   B로 설정 후 A 행 0행, A의 user_id로 INSERT 시도 → RLS 위반. 비로그인 → 401, 다른 Origin POST → 403, 폼 Content-Type → 403.
4. **라우트 전수 스모크**: ROUTES 표의 모든 GET과 안전한 POST(빈/최소 본문)를 서버 백엔드로 호출 → 500 0개(비활성은 403 `feature_disabled`).
5. **게이트**: scripts→server import 0, server 경로 허용 목록, `.env` 미추적, `outside == 0` 유지.
6. **compose 스모크** `deploy/smoke.py local|server`: build → up → healthz 대기 → (local) 로그에서 key 주소로 `/`·`/api/status`
   (server) admin add-user 2명 → dev 로그인(`YTC_ENV=dev`) → init·기획안 저장·`/api/status` → 격리 1건 확인 → `down -v`.
   `--with-youtube`: `tests/fixtures/links.txt`의 링크 1개 수집(LLM 비용 발생, 기본 끔).
7. 로컬판: 매 Task `pytest tests/` 전부(서버 테스트는 env 없으면 skip이라 로컬 개발 흐름 그대로).

## 11. 작업 순서

각 Task: 테스트 먼저, 끝나면 `pytest tests/` 전부 통과 + 해당 Task의 새 테스트 통과. 로컬판 바이트·JSON 변화 0(이음매 기본값은 no-op).

| # | Task | 바꾸는 곳 | 증명 |
|---|---|---|---|
| T1 | 배치 골격: `server/` 빈 패키지, `deploy/`, `requirements-server.txt`, 게이트(scripts→server 금지, server 경로 허용 목록, `.env` 미추적) | 새 파일, tests | 게이트 테스트, 기존 전부 |
| T2 | 로컬 서버 이음매: `server.py`의 `match`/`call` 분리, `/healthz`, `--host`(컨테이너 전용)·포그라운드, `/api/features` + `YTC_DISABLE` 가드 + JS `data-feature` 숨김 | scripts/ui | test_ui_server 기존 + features·healthz·host 테스트 |
| T3 | **개인판 컨테이너**: Dockerfile base·local, `deploy/local` compose·.env.example, `llm` env(`YTC_LLM_MODEL`, `YTC_LLM_MAX_TOKENS`), smoke local(+yt-dlp 실제 링크 1개로 JS 런타임 필요 여부 결정) | deploy, llm.py | smoke local 통과, test_llm |
| T4 | 코어 이음매: `Backend.usage`·`Ctx.usage`·`NullMeter`, `llm_anthropic` usage 콜백, `scripts/limits.slot` no-op + cuts·stt·llm 호출부 감싸기 | stores, ctx, llm*, cuts, stt, limits | 기존 전부 + usage 콜백 값 테스트(가짜 응답) |
| T5 | PG 스키마: Alembic `0001`, 롤·RLS, `db.py`(pool, `user_tx`), `docker-compose.test.yml` | server, deploy | RLS fail-closed·교차 사용자 테스트(DB 직접) |
| T6 | `PgDocStore` + 계약 테스트(File·Pg) | server/pgstore, tests/contract | 계약 doc 항목 전부 |
| T7 | `S3ObjectStore`·`S3SharedCache`(PG 인덱스 + MinIO, write_path 폴더) + 계약 | server/s3store | 계약 object·shared 항목 |
| T8 | `ServerScratch`·`PgTrash`·`ServerBackend` + `export_tree` + 동등성 시나리오 | server | test_equivalence |
| T9 | S9: `admin add-user/disable-user/import/usage`, import 검증(status 비교), `plan_tree`(키마다 읽는 내보내기) | server/admin·pgstore·s3store·backend | 픽스처 작업 폴더 가져오기 → `export_tree` 바이트 동일·status 동일, 재실행 0건 |
| T10 | ASGI 앱: 어댑터, 세션, dev 로그인, CSRF, 요청별 Ctx, 사용자별 Registry, `/files` 프록시, 서버 기능 가드, api.js 401 처리 | server/asgi·auth·files·jobs_registry | 격리 테스트, 라우트 전수 스모크 |
| T11 | 자원·한도: media/llm 슬롯 설치, fetch 전역 제한, advisory lock, `UsageMeter`(가격표·일 한도·오류 문구), media 슬롯 수 측정 | server/usage·jobs_registry | 한도 초과 테스트(가짜 usage), 두 사용자 동시 수집이 서로 job_running을 내지 않음 |
| T12 | Google OIDC: authlib 로그인·콜백·허용 목록·sub 고정·로그아웃·epoch, login.html | server/auth | 콜백을 가짜 IdP 토큰으로 단위 테스트(서명 검증 경로 포함), 실제 Google 1회 수동 |
| T13 | 서버판 패키징: Dockerfile server, `deploy/server` compose(pg·minio·init·migrate·app·caddy profile), healthcheck, smoke server | deploy | smoke server 통과 |
| T14 | 문서: README(두 판 실행·커맨드 표), CLAUDE.md 범위, 상위 설계 §2(배치)·§3.2(최소 스키마)·§4(작업 ADR-S6)·§6 갱신 | docs | 리뷰 |

T3은 서버 작업과 독립이라 먼저 끝내 개인판을 바로 쓸 수 있게 한다. T5~T9는 서버 앱 없이 검증된다(저장소 → 데이터 → 앱 순).

## 12. 위험

| 위험 | 대응 |
|---|---|
| PgStore가 FileStore와 의미가 미묘하게 다름(목록 순서·대소문자·폴더 존재·mtime·없는 키의 expect_version) → 화면 순서·캐시·충돌 판정이 달라짐 | 계약 테스트 + 동등성 시나리오 + import 후 status 비교. 개인판 리눅스 컨테이너의 대소문자 구분 glob 차이는 README에 명시(작업 폴더를 Windows에서 옮겨 올 때만 영향) |
| 단일 프로세스·메모리 작업: 재시작 시 진행 중 작업·확인 대기 손실, OpenCV·yt-dlp 스레드가 UI 응답을 늦춤 | media/llm 슬롯, uvicorn workers=1 고정, 재시작 안내 문구, S7b 전환 조건(§5.4) 기록 |
| 핸들러의 숨은 로컬 가정(Path 인자, `FileSlice`, `scratch.workdir`, 절대 경로 출력, tmp 핸드오프) 이 서버 백엔드에서 500 | 비활성 표(§8) + 라우트 전수 스모크(§10-4) |
| 서버 IP YouTube 차단·yt-dlp JS 런타임 요구 | `YT_PROXY`, 전역 fetch 간격, 공유 캐시, T3에서 실제 링크로 이미지 확인 |
| LLM 비용 초과(호출 하나가 한도를 넘김, 가격표 오류) | 호출 전 검사 + `max_tokens` 상한 + 가격표에 없는 모델 시작 거부 + `admin usage` |
| 공유 캐시 오염(T7 리뷰, T9): 공유 캐시는 "먼저 쓴 쪽이 이긴다". `admin import`가 shared 키를 넣으면, 틀린·악의적인 작업 폴더(`workspace/raw/<vid>.json`·`frames/`가 다른 영상 내용)가 모든 사용자의 그 영상 분석에 섞인다. 수집 경로도 같은 구조(첫 수집자의 raw가 고정) | 가져오기는 운영자만 실행(`ytc_owner` 필요)하고 신뢰하는 지인의 폴더만 올린다. 이미 있는 shared 키는 `--overwrite`로도 덮지 않는다(`skipped`). 영상 md는 사용자 것이라 교정은 md에서 하고 캐시는 원본 데이터로만 쓴다. 후속(S10): raw `id`와 키의 영상 id 일치 검사, `shared_objects.created_by` 기준 점검·삭제 명령 |
| 인증 우회·세션 탈취 | 초대제 + sub 고정 + Secure/HttpOnly/Lax 쿠키 + Origin·JSON 강제 + RLS. dev 로그인은 production에서 시작 거부 |
