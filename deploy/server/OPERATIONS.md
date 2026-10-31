# 서버판 운영 메모

모든 명령은 `deploy/server`에서 실행한다(`docker compose`가 같은 폴더의 `.env`를 읽는다).

## 처음 올리기

1. `cp .env.example .env` 후 필수 값(`YTC_PUBLIC_URL`, `YTC_SESSION_SECRET`, DB·S3 비밀번호)을 채운다. 로그인 방법은
   `YTC_PASSWORD_LOGIN=1`(이메일+비밀번호, 예시 파일 기본값)과 Google 클라이언트(선택) 중 하나 이상이 있어야 production이 시작한다.
   비밀번호·키는 영문·숫자·`-_`만 쓴다(주소에 끼워 넣는다). `python -c "import secrets;print(secrets.token_urlsafe(48))"`
2. `docker compose up -d --build` (도메인과 자동 HTTPS가 필요하면 `--profile tls`, `.env`에 `YTC_DOMAIN`).
3. 첫 사용자(관리자): `docker compose --profile admin run --rm admin add-user you@example.com --admin --password`
   (비밀번호를 두 번 묻는다. 화면에 보이지 않고 출력·로그에도 남지 않는다. 10자 이상)
   이미 있는 계정을 관리자로 만들려면 `... admin make-admin you@example.com`.
4. 그다음 사용자 관리는 웹의 **관리 화면**에서 한다(아래 "관리 화면").

## 관리 화면 (`#/admin`)

- 관리자(`users.is_admin`)에게만 머리글의 "관리" 링크와 도움말 메뉴의 "관리"가 보인다. 관리자가 아니면 API가 403이고 DB도 거부한다.
- 탭: **사용자**(검색·필터·정렬·50명씩 쪽, 사용자 추가, 자세히 패널) / **사용량**(7·30·90일) / **감사 기록**(누가·무엇을·누구에게·기간 필터,
  쪽) / **서버 정보**(사람 수, 오늘·이번 달 전체 사용액, 기본 하루 한도, 로그인 방법, 꺼진 기능 — 보기만).
- 사용자 추가: 이메일, 이름, 첫 비밀번호(Google 로그인이 꺼진 서버는 필수 `password_required`, 켜져 있으면 선택), 관리자 여부, 하루 한도.
- 자세히 패널: 추가한 날, 최근 로그인(Google·비밀번호·dev 로그인 성공 때 `users.last_login_at`), 문서·파일·휴지통 개수와 바이트,
  오늘·이번 달 사용액. 고치기는 한 번에 한 트랜잭션(하나라도 거절되면 아무것도 안 바뀜): 이메일(소문자, 겹치면 409 `user_exists`,
  **바꾸면 Google 연결이 풀리고 모든 세션이 끊긴다** — 새 이메일의 Google 계정으로 다시 로그인하면 새로 연결된다), 이름, 하루 한도
  (비우면 `YTC_DAILY_USD`), 관리자, 비활성. 비밀번호 정하기(정한 비밀번호는 화면에 다시 나오지 않으니 직접 알려 준다)·지우기, 모든 기기 로그아웃.
- 사용자 삭제(되돌릴 수 없다): 그 사람의 이메일을 그대로 입력해야 버튼이 켜지고 서버와 DB가 다시 확인한다(다르면 400 `confirm_mismatch`).
  순서: 먼저 비활성으로 바꿔 커밋(감사 `disable`, 그 사람의 다음 요청은 401) → 그 사용자의 작업 취소(최대 5초, 그동안 새 작업도 막음) → DB 함수가 문서·폴더·파일 인덱스·휴지통 행과 사용자 행을 지우고 감사 기록을 남김(커밋)
  → 앱이 MinIO `users/{id}/`·`trash/{id}/`를 지움. 공유 캐시(`shared/`)는 그대로, AI 사용 기록(`llm_usage`)은 `user_id`를 비운 채 남아
  합계에 "삭제된 사용자"로 잡힌다. MinIO 정리가 실패하면 화면에 남은 개수를 알리고 서버 로그에 `user purge ... (S10)`을 남긴다
  (행은 이미 지워졌으므로 S10 정합성 점검이나 `mc rm`으로 치운다). 감사 기록은 지운 사람의 이메일을 계속 보여 준다.
  DB가 삭제를 거절하면 작업 막음을 풀고, 원래 활성이었으면 다시 활성으로 돌린다(감사 `enable`).
  잠시 막기만 하려면 삭제 대신 비활성으로 바꾼다.
- 막는 것: 자기 계정 비활성·강등(409 `self_protected`)·삭제(409 `self_delete`). 요청한 사람은 늘 활성 관리자이고 자기 자신은 뺄 수 없으니 화면으로는
  관리자가 0명이 되지 않는다(두 관리자가 동시에 서로를 빼도 DB가 차례로 처리해 한 명은 남는다). DB의 "마지막 관리자" 검사는
  그 규칙 밑의 받침일 뿐 화면에서 따로 보이는 경우는 없다.
- 비밀번호 재설정·지우기·비활성·모든 기기 로그아웃은 그 사람의 세션을 바로 끊는다(`session_epoch` 증가).
- 구조: 앱은 여전히 owner DB 자격이 없다. 앱 롤이 users에 직접 쓸 수 있는 것은 Google 로그인의 `google_sub` 고정뿐이고(0004에서 `display_name` 권한도 걷음), 마이그레이션 `0003`·`0004`의 owner 소유 SECURITY DEFINER 함수
  (`admin_*`, 로그인 기록 `user_record_login`)만 실행할 수 있다. 함수는 먼저 요청한 사람(`app.user_id`)이 활성 관리자인지 확인하고, 바꾼 것은 `admin_audit`에 남긴다
  (비밀번호·해시는 남기지 않는다). 비밀번호 해시는 앱이 만들고 함수에는 해시만 넘어간다.
- 관리자를 모두 잃었거나 화면이 잠겼으면 owner 경로로 되살린다: `... admin make-admin you@example.com`.
  되살릴 계정은 **활성**이어야 로그인할 수 있다 — 출력의 `"disabled": true`면 먼저 `... admin enable-user you@example.com`.
  (`revoke-admin`으로 걷기. CLI는 마지막 관리자도 걷을 수 있는 운영자의 탈출구다. 감사 기록에는 "운영자(명령줄)"로 남는다.)
- 명령줄로도 된다(owner 경로, 감사 기록 "운영자(명령줄)"): `... admin set-email old@example.com new@example.com`,
  `... admin set-name friend@example.com "지수"`, `... admin delete-user friend@example.com --confirm friend@example.com`
  (명령줄 삭제는 앱의 작업을 멈추지 못하므로 먼저 `disable-user`로 세션을 끊어 두거나 웹 화면에서 지운다).
  명령줄은 웹 관리 함수와 같은 DB 잠금을 잡아 순서대로 처리된다. 명령줄로 이메일을 바꾸면 세션은 바로 끊기지만, 떠 있는 앱은
  문서 저장소 표시(`ytc:<이메일>`)에 옛 이메일을 앱을 다시 시작할 때까지 보여 줄 수 있다(웹 화면에서 바꾸면 바로 바뀐다).
- 되돌리기(롤백): `docker compose run --rm migrate python -m alembic -c server/alembic.ini downgrade 0003`처럼 `0004`를 되돌리면
  **지운 사용자의 익명 사용량 행(`llm_usage.user_id IS NULL`)이 함께 지워진다**(0003의 NOT NULL·CASCADE로 돌아가야 하므로).
  지출 합계를 남기려면 되돌리기 전에 `admin usage --days 90` 출력이나 `llm_usage` 덤프를 받아 둔다. 마지막 로그인·감사 기록의
  보존 이메일 열도 함께 사라진다(감사 행 자체는 남는다).
- 감사 기록을 DB에서 직접 보기: `docker compose exec postgres psql -U ytc_owner -d ytc -c "SELECT at, action, detail FROM admin_audit ORDER BY id DESC LIMIT 20"`

## 사용자 추가·가져오기(명령줄)

- 초대(관리 화면에서도 된다): `docker compose --profile admin run --rm admin add-user friend@example.com [--daily-limit 2.00] [--password]`
  초대된 이메일만 로그인할 수 있다(비밀번호를 정했으면 이메일+비밀번호로, Google이 켜져 있으면 그 이메일의 Google 계정으로).

## 비밀번호(이메일+비밀번호 로그인, `YTC_PASSWORD_LOGIN=1`)

- 정하기·재설정(잊어버렸을 때): `docker compose --profile admin run --rm admin set-password friend@example.com`
  터미널에서 두 번 묻는다. 바꾸면 그 사용자의 모든 기기 세션이 끊긴다(`session_epoch` 증가). 새 비밀번호는 안전한 경로로 전한다.
- 스크립트·터미널 없는 곳: `--password-stdin`으로 표준 입력 한 줄을 넘긴다(`run`에 `-T`).
  `docker compose --profile admin run --rm -T admin set-password friend@example.com --password-stdin < pw.txt`
  `add-user ... --password <값>`처럼 값을 직접 쓰면 셸 기록·프로세스 목록에 남으므로 쓰지 않는다.
- 비밀번호 로그인 막기: `... admin clear-password friend@example.com` (비밀번호를 지우고 세션도 끊는다. Google 로그인은 그대로)
- 규칙: 10자 이상. 저장은 scrypt 해시(`scrypt$N$r$p$salt$hash`)만, 평문은 어디에도 남지 않는다.
- 틀린 횟수 제한(앱 메모리, 재시작하면 초기화): 이메일별 10분에 5번, 접속 IP별 10분에 20번 → 429 "로그인 시도가 너무 많습니다".
  맞히면 그 이메일의 횟수는 초기화된다. 누가 남의 이메일로 일부러 틀리면 그 이메일은 10분까지 막힐 수 있다(재시작하면 풀린다).
- 접속 IP: caddy(`--profile tls`) 뒤에서는 caddy가 쓴 `X-Forwarded-For`를 쓴다(앱은 caddy의 고정 IP `YTC_FRONT_NET`.2에서 온 것만 믿는다).
  Tailscale serve처럼 `127.0.0.1:8080`으로 직접 들어오면 모든 요청이 docker 게이트웨이 IP 하나로 보이므로 IP별 제한을 다 같이 나눠 쓴다
  (이메일별 제한은 그대로). 클라이언트가 보낸 `X-Forwarded-For`는 믿지 않는다.
- **공개 인터넷에 열 때는 caddy 경로(`--profile tls`)만 쓴다.** Tailscale Funnel·ngrok·cloudflared 같은 터널이나 다른 프록시가
  게시 포트(`127.0.0.1:8080`)로 바로 붙으면 모든 사람이 게이트웨이 IP 하나를 나눠 써서, 한 사람이 IP별 한도(10분 20번)를 채우면
  모두의 비밀번호 로그인이 10분 동안 막히고 IP별 제한이 공격자를 따로 막지 못한다. 그런 도구는 tailnet 안(지인끼리)에서만 쓴다.
- IPv6 클라이언트는 /64 단위로 센다(한 가입자가 주소를 바꿔 가며 보내도 같은 IP로 친다).
- 이 기능 전에 올린 스택을 갱신할 때: front 네트워크에 고정 대역이 생겨 compose가 네트워크를 다시 만들어야 한다.
  `docker compose --profile tls down`(볼륨 `-v`는 붙이지 않는다: 데이터가 지워진다) 후 `docker compose up -d --build`.
  `migrate`가 `0002`(비밀번호 열)를 적용한다. 기존 사용자는 `set-password`로 비밀번호를 정할 때까지 Google로만 로그인한다.
- 끄기: `... admin disable-user friend@gmail.com` (이미 나간 세션도 다음 요청에서 끊긴다)
- 모든 기기에서 로그아웃: `... admin logout-all friend@gmail.com`. 화면의 로그아웃은 이 브라우저의 쿠키만 지우므로,
  기기를 잃어버렸을 때는 이 명령으로 세션을 모두 끊는다(`session_epoch` 증가, 계정은 그대로라 다시 로그인하면 된다).
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
- 메모리 상한 조정: `.env`의 `YTC_APP_MEM_LIMIT`(기본 3g). 임시 폴더 `YTC_SCRATCH_SIZE`(tmpfs, 기본 1g)에 쓴 만큼이
  메모리 상한에 포함되므로, 임시 폴더를 키우면 상한도 같이 올린다(임시 폴더 + 작업 메모리 < 상한).
- `--profile tls`에서 caddy가 `YTC_DOMAIN이 비어 있습니다`로 끝나면 `.env`에 도메인을 넣는다(다른 서비스는 도메인 없이도 뜬다).
