"""admin: 관리 화면(서버 설계 §4.2 관리 화면, T16) — 감사 기록 표와 owner 소유 SECURITY DEFINER 관리 함수.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02

- `users.is_admin`은 0001에 이미 있다(boolean NOT NULL DEFAULT false). 첫 관리자는 관리 CLI `make-admin`(owner).
- `admin_audit(id, actor_id, action, target_user_id, detail jsonb, at)`: 앱 롤에는 표 권한이 없다. 쓰기·읽기는 아래 함수로만.
  사용자 행은 지우지 않지만 FK는 `ON DELETE SET NULL`(기록은 남는다). 비밀번호·해시는 detail에 넣지 않는다.
- 함수(전부 owner 소유, `SECURITY DEFINER`, `SET search_path = pg_catalog, public, pg_temp`, 표·함수 이름은 `public.`으로 고정):
  맨 먼저 `admin_actor()`로 호출자 = `current_setting('app.user_id')`가 **활성 관리자**인지 본다. 설정이 없거나 uuid가 아니거나
  관리자가 아니거나 비활성이면 SQLSTATE `YTA01`(fail closed). 앱은 요청마다 `Db.user_tx(로그인한 사용자)`로 부르므로
  RLS와 같은 신뢰 모델이다(앱 코드가 다른 사용자 id를 설정하지 않는다).
  쓰는 함수는 `pg_advisory_xact_lock(ADMIN_LOCK)`으로 서로 직렬화한 뒤 호출자를 확인한다 — 두 관리자가 서로를 동시에 강등해도
  관리자가 0명이 되지 않는다. 자기 비활성·자기 강등은 `YTA03`. 호출자는 늘 활성 관리자이고 자기 자신은 뺄 수 없으므로 함수 경로로는
  마지막 활성 관리자가 남는다. `admin_keep_one`(`YTA04`)은 그 규칙 밑의 받침(backstop)일 뿐 따로 닿는 경로가 아니다.
  없는 대상은 `YTA02`, 잘못된 값은 `YTA05`. 바꾸는 함수는 감사 행을 하나 쓴다(목록·사용량·감사 읽기는 기록하지 않는다).
- EXECUTE는 PUBLIC에서 걷고 앱 롤에만 준다(내부 `admin_actor()`는 앱 롤도 못 부른다). 앱 롤은 여전히 users에 직접 쓰지 못한다.
- 비밀번호 해시는 앱(`server.passwords`)이 만들고 함수는 해시만 받는다(0002의 CHECK가 형식을 묶는다).
- 되돌리면 함수와 표만 지운다.
"""
from __future__ import annotations

from typing import Any, Final

from alembic import context, op
from psycopg import sql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

ADMIN_LOCK: Final = 8751729245835585902          # int.from_bytes(b"ytcadmin"): one key for every admin write
PATH: Final = "SET search_path = pg_catalog, public, pg_temp"

# signatures the app role may EXECUTE (regprocedure text)
APP_FUNCTIONS: Final = (
    "admin_list_users()",
    "admin_add_user(text, text, numeric, text, boolean)",
    "admin_set_password(uuid, text)",
    "admin_clear_password(uuid)",
    "admin_set_disabled(uuid, boolean)",
    "admin_logout_all(uuid)",
    "admin_set_limit(uuid, numeric)",
    "admin_set_admin(uuid, boolean)",
    "admin_usage(integer)",
    "admin_audit_log(integer)",
)
INTERNAL_FUNCTIONS: Final = ("admin_actor()", "admin_target(uuid)", "admin_keep_one(uuid)")

TABLE_SQL = """
CREATE TABLE admin_audit (
  id bigserial PRIMARY KEY,
  actor_id uuid REFERENCES users ON DELETE SET NULL,
  action text NOT NULL,
  target_user_id uuid REFERENCES users ON DELETE SET NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  at timestamptz NOT NULL DEFAULT now());
CREATE INDEX admin_audit_at ON admin_audit (at);
"""

FUNCTIONS_SQL = f"""
-- caller = app.user_id, an enabled admin; anything else is YTA01 (fail closed)
CREATE FUNCTION public.admin_actor() RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  raw text := nullif(current_setting('app.user_id', true), '');
  uid uuid;
BEGIN
  IF raw IS NULL THEN
    RAISE EXCEPTION 'admin only' USING ERRCODE = 'YTA01';
  END IF;
  BEGIN
    uid := raw::uuid;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'admin only' USING ERRCODE = 'YTA01';
  END;
  PERFORM 1 FROM public.users u WHERE u.id = uid AND u.is_admin AND NOT u.disabled;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'admin only' USING ERRCODE = 'YTA01';
  END IF;
  RETURN uid;
END $fn$;

-- the target row, locked; YTA02 when there is none
CREATE FUNCTION public.admin_target(p_user uuid) RETURNS public.users
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  row public.users;
BEGIN
  SELECT * INTO row FROM public.users u WHERE u.id = p_user FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'user not found' USING ERRCODE = 'YTA02';
  END IF;
  RETURN row;
END $fn$;

-- Backstop under the self-protection rule: the caller is always an enabled admin and cannot remove themself (YTA03),
-- so through these functions this never fires today. It stays so that a future rule change cannot leave zero admins.
CREATE FUNCTION public.admin_keep_one(p_leaving uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
BEGIN
  PERFORM 1 FROM public.users u WHERE u.is_admin AND NOT u.disabled AND u.id <> p_leaving;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'last admin' USING ERRCODE = 'YTA04';
  END IF;
END $fn$;

CREATE FUNCTION public.admin_list_users()
RETURNS TABLE (id uuid, email text, display_name text, is_admin boolean, disabled boolean, has_password boolean,
               has_google boolean, daily_usd_limit numeric, today_usd numeric, created_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
#variable_conflict use_column
DECLARE
  day_start timestamptz := date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul';
BEGIN
  PERFORM public.admin_actor();
  RETURN QUERY
    SELECT u.id, u.email, u.display_name, u.is_admin, u.disabled, u.password_hash IS NOT NULL,
           u.google_sub IS NOT NULL, u.daily_usd_limit,
           coalesce((SELECT sum(l.cost_usd) FROM public.llm_usage l WHERE l.user_id = u.id AND l.at >= day_start), 0),
           u.created_at
      FROM public.users u ORDER BY u.email;
END $fn$;

CREATE FUNCTION public.admin_add_user(p_email text, p_display_name text, p_daily_limit numeric, p_password_hash text,
                                      p_is_admin boolean) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  new_id uuid;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  IF p_email IS NULL OR p_is_admin IS NULL OR (p_daily_limit IS NOT NULL AND (
      p_daily_limit < 0 OR p_daily_limit > 999999.99 OR p_daily_limit <> round(p_daily_limit, 2))) THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  INSERT INTO public.users (email, display_name, daily_usd_limit, password_hash, is_admin)
    VALUES (p_email, p_display_name, p_daily_limit, p_password_hash, p_is_admin) RETURNING users.id INTO new_id;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, detail)
    VALUES (actor, 'add_user', new_id, jsonb_build_object('email', p_email, 'is_admin', p_is_admin,
            'password', p_password_hash IS NOT NULL, 'daily_usd_limit', p_daily_limit));
  RETURN new_id;
END $fn$;

CREATE FUNCTION public.admin_set_password(p_user uuid, p_hash text) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  epoch integer;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  IF p_hash IS NULL THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  PERFORM public.admin_target(p_user);
  UPDATE public.users u SET password_hash = p_hash, session_epoch = u.session_epoch + 1 WHERE u.id = p_user
    RETURNING u.session_epoch INTO epoch;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id) VALUES (actor, 'set_password', p_user);
  RETURN epoch;
END $fn$;

CREATE FUNCTION public.admin_clear_password(p_user uuid) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  epoch integer;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  PERFORM public.admin_target(p_user);
  UPDATE public.users u SET password_hash = NULL, session_epoch = u.session_epoch + 1 WHERE u.id = p_user
    RETURNING u.session_epoch INTO epoch;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id) VALUES (actor, 'clear_password', p_user);
  RETURN epoch;
END $fn$;

CREATE FUNCTION public.admin_set_disabled(p_user uuid, p_disabled boolean) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  target public.users;
  epoch integer;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  IF p_disabled IS NULL THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  target := public.admin_target(p_user);
  IF p_disabled AND p_user = actor THEN
    RAISE EXCEPTION 'self' USING ERRCODE = 'YTA03';
  END IF;
  IF p_disabled AND target.is_admin AND NOT target.disabled THEN
    PERFORM public.admin_keep_one(p_user);
  END IF;
  -- disabling ends every session at once; enabling does not bring old sessions back either way (epoch stays)
  UPDATE public.users u SET disabled = p_disabled,
         session_epoch = u.session_epoch + CASE WHEN p_disabled THEN 1 ELSE 0 END
   WHERE u.id = p_user RETURNING u.session_epoch INTO epoch;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id)
    VALUES (actor, CASE WHEN p_disabled THEN 'disable' ELSE 'enable' END, p_user);
  RETURN epoch;
END $fn$;

CREATE FUNCTION public.admin_logout_all(p_user uuid) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  epoch integer;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  PERFORM public.admin_target(p_user);
  UPDATE public.users u SET session_epoch = u.session_epoch + 1 WHERE u.id = p_user
    RETURNING u.session_epoch INTO epoch;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id) VALUES (actor, 'logout_all', p_user);
  RETURN epoch;
END $fn$;

CREATE FUNCTION public.admin_set_limit(p_user uuid, p_usd numeric) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  target public.users;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  IF p_usd IS NOT NULL AND (p_usd < 0 OR p_usd > 999999.99 OR p_usd <> round(p_usd, 2)) THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  target := public.admin_target(p_user);
  UPDATE public.users u SET daily_usd_limit = p_usd WHERE u.id = p_user;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, detail)
    VALUES (actor, 'set_limit', p_user, jsonb_build_object('from', target.daily_usd_limit, 'to', p_usd));
END $fn$;

CREATE FUNCTION public.admin_set_admin(p_user uuid, p_admin boolean) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  target public.users;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  IF p_admin IS NULL THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  target := public.admin_target(p_user);
  IF NOT p_admin AND p_user = actor THEN
    RAISE EXCEPTION 'self' USING ERRCODE = 'YTA03';
  END IF;
  IF NOT p_admin AND target.is_admin AND NOT target.disabled THEN
    PERFORM public.admin_keep_one(p_user);
  END IF;
  UPDATE public.users u SET is_admin = p_admin WHERE u.id = p_user;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, detail)
    VALUES (actor, 'set_admin', p_user, jsonb_build_object('is_admin', p_admin));
END $fn$;

CREATE FUNCTION public.admin_usage(p_days integer)
RETURNS TABLE (email text, day date, calls bigint, failed bigint, input_tokens bigint, output_tokens bigint,
               cost_usd numeric)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
#variable_conflict use_column
BEGIN
  PERFORM public.admin_actor();
  IF p_days IS NULL OR p_days < 1 OR p_days > 90 THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  RETURN QUERY
    SELECT u.email, (l.at AT TIME ZONE 'Asia/Seoul')::date AS d, count(*), count(*) FILTER (WHERE NOT l.ok),
           sum(l.input_tokens)::bigint, sum(l.output_tokens)::bigint, sum(l.cost_usd)
      FROM public.llm_usage l JOIN public.users u ON u.id = l.user_id
     -- timestamptz lower bound (KST midnight p_days-1 days ago) so the (user_id, at) index applies
     WHERE l.at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') - make_interval(days => p_days - 1))
                   AT TIME ZONE 'Asia/Seoul'
     GROUP BY u.email, d ORDER BY d DESC, u.email;
END $fn$;

CREATE FUNCTION public.admin_audit_log(p_limit integer)
RETURNS TABLE (id bigint, at timestamptz, actor_email text, action text, target_email text, detail jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
#variable_conflict use_column
BEGIN
  PERFORM public.admin_actor();
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 200 THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  RETURN QUERY
    SELECT a.id, a.at, actor.email, a.action, target.email, a.detail
      FROM public.admin_audit a
      LEFT JOIN public.users actor ON actor.id = a.actor_id
      LEFT JOIN public.users target ON target.id = a.target_user_id
     ORDER BY a.id DESC LIMIT p_limit;
END $fn$;
"""


def _driver() -> Any:
    return op.get_bind().connection.driver_connection


def upgrade() -> None:
    role = sql.Identifier(str(context.config.attributes["app_role"]))
    op.execute(TABLE_SQL)
    op.execute(FUNCTIONS_SQL)
    drv = _driver()
    for signature in (*APP_FUNCTIONS, *INTERNAL_FUNCTIONS):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in APP_FUNCTIONS:
        drv.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public." + signature + " TO {}").format(role))


def downgrade() -> None:
    for signature in (*APP_FUNCTIONS, *INTERNAL_FUNCTIONS):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("DROP TABLE admin_audit")
