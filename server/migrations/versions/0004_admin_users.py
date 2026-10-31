"""admin_users: 관리 화면에서 사용자 만들기·고치기·지우기, 목록 검색·정렬·쪽, 상세, 감사 기록 필터, 서버 정보, 마지막 로그인.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03

보안 모델은 0003과 같다: 함수는 전부 owner 소유 `SECURITY DEFINER`, `SET search_path = pg_catalog, public, pg_temp`,
이름은 `public.`으로 고정. 관리 함수는 맨 먼저 `admin_actor()`(호출자 = `app.user_id`가 활성 관리자, 아니면 `YTA01`),
쓰는 함수는 `pg_advisory_xact_lock(ADMIN_LOCK)` 뒤에 호출자를 확인하고 감사 행을 하나 쓴다. 앱 롤의 users 직접 쓰기는 `UPDATE (google_sub)`(Google 로그인 고정)만 남는다(아래).

스키마:
- `users.last_login_at timestamptz NULL`: 로그인 성공 때 앱이 `user_record_login()`을 부른다(`Db.user_tx(그 사용자)` 안에서,
  호출자 자신의 행만, 비활성이면 바꾸지 않음). 감사 기록은 남기지 않는다(관리 행위가 아니다).
- `admin_audit.actor_email`·`target_email text NULL`: 사용자를 지울 때 그 사용자를 가리키던 감사 행에 이메일을 남긴다
  (id 열은 FK `ON DELETE SET NULL`로 비워진다). 읽을 때는 `coalesce(지금 users의 이메일, 남긴 이메일)`. 인덱스 `(actor_id)`, `(target_user_id)`.
- `llm_usage.user_id`: NOT NULL을 풀고 FK를 `ON DELETE SET NULL`로 바꾼다. 사용자를 지워도 비용 기록은 익명으로 남는다
  (RLS 정책은 그대로라 user_id가 NULL인 행은 앱 롤에 보이지 않는다. 합계·사용량 표에는 "지운 사용자"로 잡힌다).
- 앱 롤의 `UPDATE (display_name) ON users`를 걷는다(0001이 준 것. 앱 코드는 쓰지 않았고, 이제 이름은 감사되는 `admin_set_name`만 바꾼다).
  앱 롤에 남는 users 쓰기는 Google 로그인의 `UPDATE (google_sub)`뿐이다.

새 함수(앱 롤 EXECUTE):
- `admin_list_users_page(q, status, role, login, sort, desc, limit, offset)` → jsonb `{total, rows}`.
  status `all|active|disabled`, role `all|admin|member`, login `all|password|google|none`,
  sort `email|last_login|created|today`(동점은 이메일), limit 1~200, offset ≥ 0, q ≤ 200자(이메일·이름 부분 일치, 대소문자 무시).
- `admin_user_detail(uid)` → jsonb(상태, 만든 때, 마지막 로그인, 문서·파일·휴지통 개수와 바이트, 오늘·이번 달 사용액).
- `admin_overview()` → jsonb(사용자·관리자·비활성 수, 로그인 방법별 수, 오늘·이번 달 전체 사용액).
- `admin_set_email(uid, email)` → epoch: 소문자 CHECK·UNIQUE, Google 연결(`google_sub`)을 풀고 `session_epoch`를 올린다.
  같으면 아무것도 안 한다.
- `admin_set_name(uid, name)`: 100자 이하(앱이 앞뒤 공백을 떼고 빈 값은 NULL로). 같으면 아무것도 안 한다.
- `admin_delete_user(uid, confirm_email)` → jsonb 요약 + `s3_keys`: 확인 이메일이 대상 이메일과 같아야 하고(`YTA06`),
  자기 자신은 안 되고(`YTA03`), 마지막 활성 관리자는 받침(`admin_keep_one`, `YTA04`)이 막는다. 문서·폴더·파일 인덱스·휴지통 행은
  FK CASCADE로 함께 지워지고, 사용량 행은 익명으로 남는다. MinIO 객체(`users/{uid}/`, `trash/{uid}/`)는 앱이 커밋 뒤에 지운다
  (공유 캐시는 건드리지 않는다). 감사 detail에 이메일·개수·바이트·사용량 합계를 남긴다.
- `admin_audit_page(actor, action, target, from, to, limit, offset)` → jsonb `{total, rows}`. actor·target은 이메일 부분 일치,
  action은 정확히, 날짜는 한국 시간 날짜(포함), limit 1~200.
- `user_record_login()` → boolean: `app.user_id` 자신의 `last_login_at = now()`. 설정이 없거나 uuid가 아니면 `YTA07`.

내부 함수(앱 롤 EXECUTE 없음, 관리 CLI가 owner로 직접 부른다): `admin_apply_email(uid, email, actor, via)`,
`admin_apply_name(uid, name, actor, via)`, `admin_apply_delete(uid, actor, via)`. 검사(호출자·자기 보호·확인 이메일)는 바깥 함수가 한다.

`admin_usage(days)`는 지운 사용자의 행도 세도록 LEFT JOIN으로 바꾼다(이메일 NULL). 되돌리면 0003 본문으로 돌아간다.
되돌리기(downgrade): 새 함수·열·인덱스를 지우고, **익명이 된 사용량 행(user_id NULL)은 지운다**(NOT NULL·CASCADE로 돌아가야 하므로).
"""
from __future__ import annotations

from typing import Any, Final

from alembic import context, op
from psycopg import sql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

ADMIN_LOCK: Final = 8751729245835585902          # same key as 0003: every admin write is serialized
PATH: Final = "SET search_path = pg_catalog, public, pg_temp"
KST_DAY: Final = "date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul'"
KST_MONTH: Final = "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul'"

APP_FUNCTIONS: Final = (
    "admin_list_users_page(text, text, text, text, text, boolean, integer, integer)",
    "admin_user_detail(uuid)",
    "admin_overview()",
    "admin_set_email(uuid, text)",
    "admin_set_name(uuid, text)",
    "admin_delete_user(uuid, text)",
    "admin_audit_page(text, text, text, date, date, integer, integer)",
    "user_record_login()",
)
INTERNAL_FUNCTIONS: Final = (
    "admin_apply_email(uuid, text, uuid, text)",
    "admin_apply_name(uuid, text, uuid, text)",
    "admin_apply_delete(uuid, uuid, text)",
)

SCHEMA_SQL = """
ALTER TABLE users ADD COLUMN last_login_at timestamptz;
ALTER TABLE admin_audit ADD COLUMN actor_email text, ADD COLUMN target_email text;
CREATE INDEX admin_audit_actor ON admin_audit (actor_id);
CREATE INDEX admin_audit_target ON admin_audit (target_user_id);
ALTER TABLE llm_usage ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_user_id_fkey;
ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES users ON DELETE SET NULL;
"""

UNDO_SCHEMA_SQL = """
DELETE FROM llm_usage WHERE user_id IS NULL;
ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_user_id_fkey;
ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES users ON DELETE CASCADE;
ALTER TABLE llm_usage ALTER COLUMN user_id SET NOT NULL;
DROP INDEX admin_audit_target;
DROP INDEX admin_audit_actor;
ALTER TABLE admin_audit DROP COLUMN target_email, DROP COLUMN actor_email;
ALTER TABLE users DROP COLUMN last_login_at;
"""

# admin_usage: 0003 joined users (deleted users' anonymous rows vanished); 0004 keeps them with a NULL email
USAGE_TEMPLATE = """
CREATE OR REPLACE FUNCTION public.admin_usage(p_days integer)
RETURNS TABLE (email text, day date, calls bigint, failed bigint, input_tokens bigint, output_tokens bigint,
               cost_usd numeric)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {path} AS $fn$
#variable_conflict use_column
BEGIN
  PERFORM public.admin_actor();
  IF p_days IS NULL OR p_days < 1 OR p_days > 90 THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  RETURN QUERY
    SELECT u.email, (l.at AT TIME ZONE 'Asia/Seoul')::date AS d, count(*), count(*) FILTER (WHERE NOT l.ok),
           sum(l.input_tokens)::bigint, sum(l.output_tokens)::bigint, sum(l.cost_usd)
      FROM public.llm_usage l {join} public.users u ON u.id = l.user_id
     -- timestamptz lower bound (KST midnight p_days-1 days ago) so the (user_id, at) index applies
     WHERE l.at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') - make_interval(days => p_days - 1))
                   AT TIME ZONE 'Asia/Seoul'
     GROUP BY u.email, d ORDER BY d DESC, u.email;
END $fn$;
"""

FUNCTIONS_SQL = f"""
-- ---- internal: the change itself (target locked, audit row). Callers check the actor and the rules first. ----

CREATE FUNCTION public.admin_apply_email(p_user uuid, p_email text, p_actor uuid, p_via text) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  target public.users;
  epoch integer;
BEGIN
  IF p_email IS NULL THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  target := public.admin_target(p_user);
  IF target.email = p_email THEN
    RETURN target.session_epoch;                 -- nothing to change, nothing to audit
  END IF;
  -- a new address is a new identity: drop the Google pin and end every session
  UPDATE public.users u SET email = p_email, google_sub = NULL, session_epoch = u.session_epoch + 1
   WHERE u.id = p_user RETURNING u.session_epoch INTO epoch;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, detail)
    VALUES (p_actor, 'set_email', p_user,
            jsonb_build_object('from', target.email, 'to', p_email, 'google_unlinked', target.google_sub IS NOT NULL)
            || CASE WHEN p_via IS NULL THEN '{{}}'::jsonb ELSE jsonb_build_object('via', p_via) END);
  RETURN epoch;
END $fn$;

CREATE FUNCTION public.admin_apply_name(p_user uuid, p_name text, p_actor uuid, p_via text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  target public.users;
BEGIN
  IF p_name IS NOT NULL AND (length(p_name) > 100 OR p_name = '') THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  target := public.admin_target(p_user);
  IF target.display_name IS NOT DISTINCT FROM p_name THEN
    RETURN;
  END IF;
  UPDATE public.users u SET display_name = p_name WHERE u.id = p_user;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, detail)
    VALUES (p_actor, 'set_name', p_user, jsonb_build_object('from', target.display_name, 'to', p_name)
            || CASE WHEN p_via IS NULL THEN '{{}}'::jsonb ELSE jsonb_build_object('via', p_via) END);
END $fn$;

-- Deletes the user row (docs, folders, objects, trash_items cascade; llm_usage rows stay with user_id NULL).
-- Returns the counts and every MinIO key the rows pointed at, for the app to delete after commit.
CREATE FUNCTION public.admin_apply_delete(p_user uuid, p_actor uuid, p_via text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  target public.users;
  n_docs bigint; b_docs bigint; n_folders bigint;
  n_objects bigint; b_objects bigint; k_objects text[];
  n_trash bigint; b_trash bigint; k_trash text[];
  n_usage bigint; usd numeric;
  summary jsonb;
BEGIN
  target := public.admin_target(p_user);
  SELECT count(*), coalesce(sum(d.size), 0) INTO n_docs, b_docs FROM public.docs d WHERE d.user_id = p_user;
  SELECT count(*) INTO n_folders FROM public.folders f WHERE f.user_id = p_user;
  SELECT count(*), coalesce(sum(o.size), 0), coalesce(array_agg(o.s3_key ORDER BY o.s3_key), '{{}}')
    INTO n_objects, b_objects, k_objects FROM public.objects o WHERE o.user_id = p_user;
  SELECT count(*), coalesce(sum(t.size), 0),
         coalesce(array_agg(t.s3_key ORDER BY t.s3_key) FILTER (WHERE t.s3_key IS NOT NULL), '{{}}')
    INTO n_trash, b_trash, k_trash FROM public.trash_items t WHERE t.user_id = p_user;
  SELECT count(*), coalesce(sum(l.cost_usd), 0) INTO n_usage, usd FROM public.llm_usage l WHERE l.user_id = p_user;
  summary := jsonb_build_object('email', target.email, 'display_name', target.display_name,
                                'docs', n_docs, 'docs_bytes', b_docs, 'folders', n_folders,
                                'objects', n_objects, 'objects_bytes', b_objects,
                                'trash', n_trash, 'trash_bytes', b_trash,
                                'usage_rows', n_usage, 'usage_usd', usd);
  -- the ids below become NULL with the row (ON DELETE SET NULL): keep the address on the audit rows
  UPDATE public.admin_audit a SET target_email = coalesce(a.target_email, target.email) WHERE a.target_user_id = p_user;
  UPDATE public.admin_audit a SET actor_email = coalesce(a.actor_email, target.email) WHERE a.actor_id = p_user;
  INSERT INTO public.admin_audit (actor_id, action, target_user_id, target_email, detail)
    VALUES (p_actor, 'delete_user', p_user, target.email,
            summary || CASE WHEN p_via IS NULL THEN '{{}}'::jsonb ELSE jsonb_build_object('via', p_via) END);
  DELETE FROM public.users u WHERE u.id = p_user;
  RETURN summary || jsonb_build_object('s3_keys', to_jsonb(k_objects || k_trash));
END $fn$;

-- ---- app-facing admin functions ----

CREATE FUNCTION public.admin_set_email(p_user uuid, p_email text) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  RETURN public.admin_apply_email(p_user, p_email, actor, NULL);
END $fn$;

CREATE FUNCTION public.admin_set_name(p_user uuid, p_name text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  PERFORM public.admin_apply_name(p_user, p_name, actor, NULL);
END $fn$;

CREATE FUNCTION public.admin_delete_user(p_user uuid, p_confirm_email text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  actor uuid;
  target public.users;
BEGIN
  PERFORM pg_advisory_xact_lock({ADMIN_LOCK});
  actor := public.admin_actor();
  target := public.admin_target(p_user);
  IF p_confirm_email IS NULL OR p_confirm_email <> target.email THEN
    RAISE EXCEPTION 'confirmation does not match' USING ERRCODE = 'YTA06';
  END IF;
  IF p_user = actor THEN
    RAISE EXCEPTION 'self' USING ERRCODE = 'YTA03';
  END IF;
  IF target.is_admin AND NOT target.disabled THEN
    PERFORM public.admin_keep_one(p_user);
  END IF;
  RETURN public.admin_apply_delete(p_user, actor, NULL);
END $fn$;

CREATE FUNCTION public.admin_list_users_page(p_query text, p_status text, p_role text, p_login text, p_sort text,
                                             p_desc boolean, p_limit integer, p_offset integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  day_start timestamptz := {KST_DAY};
  month_start timestamptz := {KST_MONTH};
  q text := lower(nullif(btrim(coalesce(p_query, '')), ''));
  result jsonb;
BEGIN
  PERFORM public.admin_actor();
  IF coalesce(p_status, '') NOT IN ('all', 'active', 'disabled')
     OR coalesce(p_role, '') NOT IN ('all', 'admin', 'member')
     OR coalesce(p_login, '') NOT IN ('all', 'password', 'google', 'none')
     OR coalesce(p_sort, '') NOT IN ('email', 'last_login', 'created', 'today')
     OR p_desc IS NULL OR p_limit IS NULL OR p_limit < 1 OR p_limit > 200 OR p_offset IS NULL OR p_offset < 0
     OR length(coalesce(p_query, '')) > 200 THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  WITH spend AS (
    SELECT l.user_id, coalesce(sum(l.cost_usd) FILTER (WHERE l.at >= day_start), 0) AS today_usd,
           sum(l.cost_usd) AS month_usd
      FROM public.llm_usage l WHERE l.at >= least(day_start, month_start) AND l.user_id IS NOT NULL
     GROUP BY l.user_id),
  base AS (
    SELECT u.id, u.email, u.display_name, u.is_admin, u.disabled, u.password_hash IS NOT NULL AS has_password,
           u.google_sub IS NOT NULL AS has_google, u.daily_usd_limit, coalesce(s.today_usd, 0) AS today_usd,
           coalesce(s.month_usd, 0) AS month_usd, u.created_at, u.last_login_at
      FROM public.users u LEFT JOIN spend s ON s.user_id = u.id
     WHERE (q IS NULL OR strpos(u.email, q) > 0 OR strpos(lower(coalesce(u.display_name, '')), q) > 0)
       AND (p_status = 'all' OR (p_status = 'disabled') = u.disabled)
       AND (p_role = 'all' OR (p_role = 'admin') = u.is_admin)
       AND (p_login = 'all'
            OR (p_login = 'password' AND u.password_hash IS NOT NULL)
            OR (p_login = 'google' AND u.google_sub IS NOT NULL)
            OR (p_login = 'none' AND u.password_hash IS NULL AND u.google_sub IS NULL))),
  ranked AS (
    SELECT b.*, row_number() OVER (ORDER BY
             CASE WHEN p_sort = 'last_login' AND NOT p_desc THEN b.last_login_at END ASC NULLS LAST,
             CASE WHEN p_sort = 'last_login' AND p_desc THEN b.last_login_at END DESC NULLS LAST,
             CASE WHEN p_sort = 'created' AND NOT p_desc THEN b.created_at END ASC,
             CASE WHEN p_sort = 'created' AND p_desc THEN b.created_at END DESC,
             CASE WHEN p_sort = 'today' AND NOT p_desc THEN b.today_usd END ASC,
             CASE WHEN p_sort = 'today' AND p_desc THEN b.today_usd END DESC,
             CASE WHEN p_sort = 'email' AND p_desc THEN b.email END DESC,
             b.email ASC) AS ord
      FROM base b),
  page AS (SELECT r.* FROM ranked r ORDER BY r.ord LIMIT p_limit OFFSET p_offset)
  SELECT jsonb_build_object(
           'total', (SELECT count(*) FROM base),
           'rows', coalesce((SELECT jsonb_agg(jsonb_build_object(
               'id', p.id, 'email', p.email, 'display_name', p.display_name, 'is_admin', p.is_admin,
               'disabled', p.disabled, 'has_password', p.has_password, 'has_google', p.has_google,
               'daily_usd_limit', p.daily_usd_limit, 'today_usd', p.today_usd, 'month_usd', p.month_usd,
               'created_at', p.created_at, 'last_login_at', p.last_login_at) ORDER BY p.ord) FROM page p),
             '[]'::jsonb))
    INTO result;
  RETURN result;
END $fn$;

CREATE FUNCTION public.admin_user_detail(p_user uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  day_start timestamptz := {KST_DAY};
  month_start timestamptz := {KST_MONTH};
  u public.users;
  result jsonb;
BEGIN
  PERFORM public.admin_actor();
  SELECT * INTO u FROM public.users x WHERE x.id = p_user;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'user not found' USING ERRCODE = 'YTA02';
  END IF;
  SELECT jsonb_build_object(
      'id', u.id, 'email', u.email, 'display_name', u.display_name, 'is_admin', u.is_admin, 'disabled', u.disabled,
      'has_password', u.password_hash IS NOT NULL, 'has_google', u.google_sub IS NOT NULL,
      'daily_usd_limit', u.daily_usd_limit, 'created_at', u.created_at, 'last_login_at', u.last_login_at,
      'docs', (SELECT count(*) FROM public.docs d WHERE d.user_id = p_user),
      'docs_bytes', (SELECT coalesce(sum(d.size), 0) FROM public.docs d WHERE d.user_id = p_user),
      'objects', (SELECT count(*) FROM public.objects o WHERE o.user_id = p_user),
      'objects_bytes', (SELECT coalesce(sum(o.size), 0) FROM public.objects o WHERE o.user_id = p_user),
      'trash', (SELECT count(*) FROM public.trash_items t WHERE t.user_id = p_user),
      'trash_bytes', (SELECT coalesce(sum(t.size), 0) FROM public.trash_items t WHERE t.user_id = p_user),
      'today_usd', (SELECT coalesce(sum(l.cost_usd), 0) FROM public.llm_usage l
                     WHERE l.user_id = p_user AND l.at >= day_start),
      'month_usd', (SELECT coalesce(sum(l.cost_usd), 0) FROM public.llm_usage l
                     WHERE l.user_id = p_user AND l.at >= month_start))
    INTO result;
  RETURN result;
END $fn$;

CREATE FUNCTION public.admin_overview() RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  day_start timestamptz := {KST_DAY};
  month_start timestamptz := {KST_MONTH};
  result jsonb;
BEGIN
  PERFORM public.admin_actor();
  SELECT jsonb_build_object(
      'users', count(*), 'admins', count(*) FILTER (WHERE u.is_admin AND NOT u.disabled),
      'disabled', count(*) FILTER (WHERE u.disabled),
      'with_password', count(*) FILTER (WHERE u.password_hash IS NOT NULL),
      'with_google', count(*) FILTER (WHERE u.google_sub IS NOT NULL),
      'never_logged_in', count(*) FILTER (WHERE u.last_login_at IS NULL),
      'today_usd', (SELECT coalesce(sum(l.cost_usd), 0) FROM public.llm_usage l WHERE l.at >= day_start),
      'month_usd', (SELECT coalesce(sum(l.cost_usd), 0) FROM public.llm_usage l WHERE l.at >= month_start),
      'month_deleted_usd', (SELECT coalesce(sum(l.cost_usd), 0) FROM public.llm_usage l
                             WHERE l.at >= month_start AND l.user_id IS NULL))
    INTO result FROM public.users u;
  RETURN result;
END $fn$;

CREATE FUNCTION public.admin_audit_page(p_actor text, p_action text, p_target text, p_from date, p_to date,
                                        p_limit integer, p_offset integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  qa text := lower(nullif(btrim(coalesce(p_actor, '')), ''));
  qt text := lower(nullif(btrim(coalesce(p_target, '')), ''));
  act text := nullif(btrim(coalesce(p_action, '')), '');
  result jsonb;
BEGIN
  PERFORM public.admin_actor();
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 200 OR p_offset IS NULL OR p_offset < 0
     OR length(coalesce(p_actor, '')) > 200 OR length(coalesce(p_target, '')) > 200
     OR length(coalesce(p_action, '')) > 40 OR (p_from IS NOT NULL AND p_to IS NOT NULL AND p_from > p_to) THEN
    RAISE EXCEPTION 'bad input' USING ERRCODE = 'YTA05';
  END IF;
  WITH base AS (
    SELECT a.id, a.at, coalesce(actor.email, a.actor_email) AS actor_email, a.action,
           coalesce(target.email, a.target_email) AS target_email, a.detail
      FROM public.admin_audit a
      LEFT JOIN public.users actor ON actor.id = a.actor_id
      LEFT JOIN public.users target ON target.id = a.target_user_id
     -- timestamptz bounds (KST day edges) so the (at) index applies
     WHERE (p_from IS NULL OR a.at >= (p_from::timestamp AT TIME ZONE 'Asia/Seoul'))
       AND (p_to IS NULL OR a.at < ((p_to + 1)::timestamp AT TIME ZONE 'Asia/Seoul'))
       AND (act IS NULL OR a.action = act)),
  hit AS (
    SELECT b.* FROM base b
     WHERE (qa IS NULL OR strpos(coalesce(b.actor_email, ''), qa) > 0)
       AND (qt IS NULL OR strpos(coalesce(b.target_email, ''), qt) > 0)),
  page AS (SELECT h.* FROM hit h ORDER BY h.id DESC LIMIT p_limit OFFSET p_offset)
  SELECT jsonb_build_object(
           'total', (SELECT count(*) FROM hit),
           'rows', coalesce((SELECT jsonb_agg(jsonb_build_object(
               'id', p.id, 'at', p.at, 'actor_email', p.actor_email, 'action', p.action,
               'target_email', p.target_email, 'detail', p.detail) ORDER BY p.id DESC) FROM page p), '[]'::jsonb))
    INTO result;
  RETURN result;
END $fn$;

-- Login hook: the app calls this inside Db.user_tx(<the user who just logged in>). Only that row, never another.
CREATE FUNCTION public.user_record_login() RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS $fn$
DECLARE
  raw text := nullif(current_setting('app.user_id', true), '');
  uid uuid;
BEGIN
  IF raw IS NULL THEN
    RAISE EXCEPTION 'no user' USING ERRCODE = 'YTA07';
  END IF;
  BEGIN
    uid := raw::uuid;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'no user' USING ERRCODE = 'YTA07';
  END;
  UPDATE public.users u SET last_login_at = now() WHERE u.id = uid AND NOT u.disabled;
  RETURN FOUND;
END $fn$;
"""


def _driver() -> Any:
    return op.get_bind().connection.driver_connection


def upgrade() -> None:
    role = sql.Identifier(str(context.config.attributes["app_role"]))
    op.execute(SCHEMA_SQL)
    op.execute(FUNCTIONS_SQL)
    op.execute(USAGE_TEMPLATE.format(path=PATH, join="LEFT JOIN"))
    drv = _driver()
    for signature in (*APP_FUNCTIONS, *INTERNAL_FUNCTIONS):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in APP_FUNCTIONS:
        drv.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public." + signature + " TO {}").format(role))
    drv.execute(sql.SQL("REVOKE UPDATE (display_name) ON users FROM {}").format(role))


def downgrade() -> None:
    role = sql.Identifier(str(context.config.attributes["app_role"]))
    _driver().execute(sql.SQL("GRANT UPDATE (display_name) ON users TO {}").format(role))
    for signature in (*APP_FUNCTIONS, *INTERNAL_FUNCTIONS):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute(USAGE_TEMPLATE.format(path=PATH, join="JOIN"))
    op.execute(UNDO_SCHEMA_SQL)
