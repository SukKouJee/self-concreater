# 서버판(v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로컬판을 그대로 두고, 같은 코어 위에 PostgreSQL + MinIO + Google 로그인 서버판을 만들고, 두 판을 각각 docker-compose로 묶는다.

**Architecture:** `docs/superpowers/specs/2026-11-01-server-edition-design.md`(이하 "서버 설계")가 이 계획의 본문이다. `scripts/`는 코어로 남고, `server/`(PgStore·S3Store·ASGI 어댑터·인증·사용량·관리 CLI)와 `deploy/`(Dockerfile, compose 두 벌)를 새로 만든다. 코어 이음매(usage, limits, features, host)는 기본값이 no-op이라 로컬판은 바뀌지 않는다.

**Tech Stack:** Python 3.11+, PostgreSQL 16 + Alembic + psycopg 3, MinIO(S3 API, boto3 또는 minio), Starlette + uvicorn, authlib(Google OIDC), Docker 29 / Compose v5.

**Spec:** `docs/superpowers/specs/2026-11-01-server-edition-design.md` (상위: `2026-10-31-multiuser-server-design.md`, 저장소: `2026-10-31-s3-storage-design.md`)

## Global Constraints
- 로컬판 바이트·CLI JSON·UI JSON 변화 0(이음매 기본값 no-op). 각 작업 끝에 로컬판 관련 테스트 통과, 전체 `pytest tests/`는 컨트롤러가 T2·T4·T9·T11·T14 뒤에 돌린다.
- 게이트 `tests/test_no_direct_paths.py`는 계속 통과(서버 모듈의 허용 목록은 서버 설계 T1대로).
- 비밀값(API 키, OAuth, DB 비밀번호)은 env로만, `.env`는 git에 넣지 않는다(`.env.example`만). 커밋 서명 금지, Conventional Commit.
- 사용자 대면 문구 한국어. 서버판 비활성 기능은 서버 설계 §8 표대로.
- 운영 값(OAuth 클라이언트, 도메인/HTTPS, 일 한도 금액, 초대 이메일, YT_PROXY)은 사용자가 나중에 env로 넣는다 — 구현은 dev 로그인과 기본값으로 시험한다.

## Review Focus
- PgStore/S3Store가 FileStore와 다른 의미(목록 순서·대소문자·폴더 존재·mtime·없는 키 expect_version·create 경합) → 계약 테스트로 양쪽 동일 단언.
- 두 사용자 격리: 다른 사용자의 문서·객체·작업·/files를 볼 수 없음(RLS fail-closed 포함).
- 핸들러의 숨은 로컬 가정(Path, FileSlice, scratch.workdir, 절대 경로 출력)이 서버에서 500 → 라우트 전수 스모크.
- 인증: production에서 dev 로그인 거부, CSRF(Origin+JSON), 세션 쿠키 플래그, 초대제.
- 비용: 한도 초과 거절, 가격표에 없는 모델 시작 거부.

---

각 작업의 요구사항은 **서버 설계 §11 표의 해당 행 + 그 행이 가리키는 절(§2~§10)**이다.

| Task | 서버 설계 행 | stack / agent |
|---|---|---|
| 1 | T1 배치 골격·게이트 | python / efl:python-developer |
| 2 | T2 로컬 서버 이음매(match/call 분리, /healthz, --host, /api/features, YTC_DISABLE, data-feature) | python+frontend / efl:python-developer |
| 3 | T3 개인판 컨테이너(Dockerfile base·local, deploy/local compose, llm env, smoke local) | infra / efl:python-developer (+efl:infra-docker) |
| 4 | T4 코어 이음매(usage, limits.slot) | python |
| 5 | T5 PG 스키마·RLS·db.py·docker-compose.test.yml | python (+efl:python-sqlalchemy 참고, psycopg) |
| 6 | T6 PgDocStore + 계약 테스트 | python |
| 7 | T7 S3ObjectStore·S3SharedCache + 계약 | python |
| 8 | T8 ServerScratch·PgTrash·ServerBackend·export_tree·동등성 | python |
| 9 | T9 admin(add-user/disable-user/import/usage) | python |
| 10 | T10 ASGI 앱(어댑터·세션·dev 로그인·CSRF·요청별 Ctx·Registry·/files·가드·api.js 401) | python+frontend |
| 11 | T11 자원·한도(slots, fetch 제한, advisory lock, UsageMeter) | python |
| 12 | T12 Google OIDC | python (+efl:architect-security 참고) |
| 13 | T13 서버판 패키징(compose: pg·minio·init·migrate·app·caddy profile, smoke server) | infra |
| 14 | T14 문서(README 두 판, CLAUDE.md, 상위 설계 갱신) | docs |
