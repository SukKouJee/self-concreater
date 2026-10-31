# 서버판 운영 메모

모든 명령은 `deploy/server`에서 실행한다(`docker compose`가 같은 폴더의 `.env`를 읽는다).

## 처음 올리기

1. `cp .env.example .env` 후 필수 값(`YTC_PUBLIC_URL`, `YTC_SESSION_SECRET`, DB·S3 비밀번호, Google 클라이언트)을 채운다.
   비밀번호·키는 영문·숫자·`-_`만 쓴다(주소에 끼워 넣는다). `python -c "import secrets;print(secrets.token_urlsafe(48))"`
2. `docker compose up -d --build` (도메인과 자동 HTTPS가 필요하면 `--profile tls`, `.env`에 `YTC_DOMAIN`).
3. 첫 사용자: `docker compose --profile admin run --rm admin add-user you@gmail.com --admin`

## 사용자 추가·가져오기

- 초대: `docker compose --profile admin run --rm admin add-user friend@gmail.com [--daily-limit 2.00]`
  초대된 이메일만 Google로 로그인할 수 있다.
- 끄기: `... admin disable-user friend@gmail.com` (이미 나간 세션도 다음 요청에서 끊긴다)
- 사용량: `... admin usage --days 7`
- 내 PC의 작업 폴더 가져오기(읽기 전용으로 붙인다):
  `docker compose --profile admin run --rm -v /경로/작업폴더:/import:ro admin import /import --user you@gmail.com`
  먼저 `--dry-run`으로 보고서를 본다. 같은 폴더를 다시 가져와도 변경은 0건이다.

## 백업

백업할 것은 두 가지다: PostgreSQL(문서·사용자·사용량)과 MinIO 버킷(콘티 HTML·리포트·프레임 등). 둘 다 같은 시점 근처에 받는다.

```bash
# 1) DB (owner 계정으로 덤프. 비밀번호는 컨테이너 환경에서 읽는다)
docker compose exec -T postgres sh -c 'pg_dump -U ytc_owner -d ytc --format=custom' > ytc-$(date +%F).dump

# 2) 버킷 (mc mirror). 호스트 폴더로 내려받는다
mkdir -p backup-bucket
docker compose run --rm --no-deps -v "$PWD/backup-bucket:/backup" \
  -e MC_HOST_src="http://<S3_ACCESS_KEY>:<S3_SECRET_KEY>@minio:9000" --entrypoint mc minio-init mirror src/ytc /backup
```

(2번의 `<...>`는 `.env`의 `YTC_S3_ACCESS_KEY`·`YTC_S3_SECRET_KEY` 값. 셸 기록에 남기기 싫으면 `--env-file`을 쓴다.)
볼륨 통째 복사(`pgdata`, `minio`)는 컨테이너를 멈춘 상태에서만 일관성이 보장된다.

### 복원 순서

1. 스택을 새로 올린다: `docker compose up -d --build` (빈 DB에 마이그레이션이 테이블과 `ytc_app` 롤을 만든다).
2. 앱을 멈춘다: `docker compose stop app`.
3. DB 복원: `docker compose exec -T postgres pg_restore -U ytc_owner -d ytc --clean --if-exists < ytc-날짜.dump`
   (복원한 덤프의 `ytc_app` 롤 비밀번호가 지금 `.env`와 다르면 `docker compose run --rm migrate`로 맞춘다)
4. 버킷 복원: 위 mirror를 반대로(`mirror /backup src/ytc`).
5. `docker compose start app` 후 `/healthz`와 로그인을 확인한다.

DB와 버킷의 시점이 다르면 문서가 가리키는 파일이 없거나 남는 파일이 생길 수 있다(앱은 없는 파일을 404로 처리한다).

## 비밀번호·설정 변경 주의

- `YTC_DB_OWNER_PASSWORD`는 **PostgreSQL이 처음 초기화될 때만** 쓰인다(`pgdata` 볼륨이 비어 있을 때). 나중에 `.env`만 바꾸면 효과가 없고
  접속이 실패한다. 바꾸려면 `docker compose exec postgres psql -U ytc_owner -d ytc -c "ALTER ROLE ytc_owner PASSWORD '새값'"` 후 `.env`를 같이 바꾼다.
- `YTC_DB_APP_PASSWORD`는 `migrate`가 올릴 때마다 `ytc_app` 롤에 맞춘다: `.env`를 바꾸고 `docker compose up -d`로 충분하다.
- `YTC_S3_ACCESS_KEY`·`YTC_S3_SECRET_KEY`는 MinIO 관리자 계정이다. MinIO 데이터 볼륨이 이미 있으면 바꾼 값이 앱과 MinIO 양쪽에 반영되도록 둘을 함께 재시작한다.
- `YTC_SESSION_SECRET`을 바꾸면 모두 로그아웃된다.

## MinIO 이미지(다이제스트) 갱신

MinIO 공식 이미지는 더 받을 수 없어 Chainguard 빌드를 다이제스트로 고정해 쓴다. 갱신:

1. `docker pull cgr.dev/chainguard/minio:latest`
2. `docker image inspect --format '{{index .RepoDigests 0}}' cgr.dev/chainguard/minio:latest`
3. `docker-compose.yml`의 `minio`·`minio-init`, `docker-compose.test.yml`의 `minio`에서 `@sha256:...`를 같은 값으로 교체
4. `YTC_TEST_DOCKER=1 pytest tests/test_deploy_smoke.py tests/contract` 와 `python deploy/smoke.py server`로 확인
5. 백업을 받은 뒤 `docker compose up -d`

postgres·caddy 이미지도 같은 방식으로 다이제스트를 갱신한다(`docker buildx imagetools inspect <이미지:태그>`).

## 자주 보는 것

- 상태: `docker compose ps`, 로그: `docker compose logs -f app` (컨테이너당 최대 30MB로 순환).
- 메모리 상한 조정: `.env`의 `YTC_APP_MEM_LIMIT`.
- `--profile tls`에서 caddy가 `YTC_DOMAIN이 비어 있습니다`로 끝나면 `.env`에 도메인을 넣는다(다른 서비스는 도메인 없이도 뜬다).
