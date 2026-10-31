"""서버판 인증 설정·세션·CSRF 단위 테스트(서버 설계 §4.2·§4.4). PG·MinIO가 필요 없다."""
from __future__ import annotations

import time
import uuid

import pytest

from scripts.common import ScriptError

pytest.importorskip("starlette")

from server import auth  # noqa: E402
from server.config import AuthSettings, ServerSettings  # noqa: E402

SECRET = "s" * 32


def _env(**over: str) -> dict[str, str]:
    env = {"YTC_PUBLIC_URL": "https://ytc.example.com", "YTC_SESSION_SECRET": SECRET,
           "YTC_GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "YTC_GOOGLE_CLIENT_SECRET": "google-secret-x"}
    env.update(over)
    return env


# ---- AuthSettings ----------------------------------------------------------------

def test_defaults_are_production_without_dev_login():
    s = AuthSettings.from_env(_env())
    assert s.env == "production" and s.production and not s.dev_login
    assert s.public_origin == "https://ytc.example.com" and s.secure_cookie


def test_repr_hides_the_secret():
    assert SECRET not in repr(AuthSettings.from_env(_env()))


@pytest.mark.parametrize("missing", ["YTC_PUBLIC_URL", "YTC_SESSION_SECRET"])
def test_required_values(missing):
    env = _env()
    del env[missing]
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(env)
    assert e.value.code == "config_missing"


def test_short_secret_is_refused():
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_SESSION_SECRET="x" * 31))
    assert e.value.code == "config_invalid"


@pytest.mark.parametrize("env_name", ["production", "PRODUCTION", ""])
def test_dev_login_refused_in_production(env_name):
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_ENV=env_name, YTC_DEV_LOGIN="1"))
    assert e.value.code == "config_invalid" and "YTC_DEV_LOGIN" in e.value.message


def test_dev_login_allowed_outside_production_and_http_allowed_there():
    s = AuthSettings.from_env(_env(YTC_ENV="dev", YTC_DEV_LOGIN="1", YTC_PUBLIC_URL="http://127.0.0.1:8080"))
    assert s.dev_login and not s.production
    assert s.public_origin == "http://127.0.0.1:8080" and not s.secure_cookie


@pytest.mark.parametrize("name", ["prod", "staging", "development", "local"])
def test_env_name_allowlist(name):
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_ENV=name))
    assert e.value.code == "config_invalid"


@pytest.mark.parametrize("name", ["dev", "test", "TEST"])
def test_env_names_outside_production(name):
    assert AuthSettings.from_env(_env(YTC_ENV=name, YTC_PUBLIC_URL="http://localhost:8080")).env == name.lower()


def test_production_requires_https():
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_PUBLIC_URL="http://ytc.example.com"))
    assert e.value.code == "config_invalid"


@pytest.mark.parametrize("value", ["2", "yes", "true"])
def test_dev_login_flag_values(value):
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_ENV="dev", YTC_DEV_LOGIN=value))
    assert e.value.code == "config_invalid"


@pytest.mark.parametrize("url,origin", [
    ("https://YTC.example.com/", "https://ytc.example.com"),
    ("https://ytc.example.com:443", "https://ytc.example.com"),
    ("https://ytc.example.com:8443", "https://ytc.example.com:8443"),
])
def test_public_origin_is_normalized(url, origin):
    assert AuthSettings.from_env(_env(YTC_PUBLIC_URL=url)).public_origin == origin


@pytest.mark.parametrize("url", ["ytc.example.com", "ftp://x", "https://u:p@x.com", "https://x.com/app",
                                 "https://x.com/?a=1", "https://"])
def test_bad_public_url(url):
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_PUBLIC_URL=url))
    assert e.value.code == "config_invalid"


def test_server_settings_bundle_storage_and_auth():
    env = _env(YTC_DB_DSN="postgresql://ytc_app:pw@db/ytc", YTC_S3_ENDPOINT="http://minio:9000",
               YTC_S3_ACCESS_KEY="a", YTC_S3_SECRET_KEY="b")
    s = ServerSettings.from_env(env)
    assert s.storage.s3_endpoint == "http://minio:9000" and s.auth.public_origin == "https://ytc.example.com"


# ---- CSRF ------------------------------------------------------------------------

ORIGIN = "https://ytc.example.com"
JSON = {"content-type": "application/json"}


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_not_checked(method):
    assert auth.csrf_error(method, {}, ORIGIN) is None


@pytest.mark.parametrize("headers", [
    {"origin": ORIGIN, **JSON},
    {"origin": "HTTPS://YTC.example.com", "content-type": "application/json; charset=utf-8"},
    {"sec-fetch-site": "same-origin", **JSON},
])
def test_same_origin_json_passes(headers):
    assert auth.csrf_error("POST", headers, ORIGIN) is None


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example", **JSON},
    {"origin": "null", **JSON},
    {"origin": "https://ytc.example.com.evil.example", **JSON},
    {"origin": "http://ytc.example.com", **JSON},
    {**JSON},                                            # no Origin and no Sec-Fetch-Site
    {"sec-fetch-site": "cross-site", **JSON},
    {"sec-fetch-site": "same-site", **JSON},
])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_cross_origin_is_refused(method, headers):
    code, body = auth.csrf_error(method, headers, ORIGIN)
    assert code == 403 and body["error"] == "bad_origin"


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                                   "application/jsonx", "application/json-patch+json"])
def test_non_json_content_type_is_refused(ctype):
    headers = {"origin": ORIGIN}
    if ctype:
        headers["content-type"] = ctype
    code, body = auth.csrf_error("POST", headers, ORIGIN)
    assert code == 403 and body["error"] == "bad_content_type"


# ---- session ----------------------------------------------------------------------

def _user(**over):
    base = {"id": uuid.uuid4(), "email": "a@example.com", "disabled": False, "session_epoch": 3}
    base.update(over)
    return auth.UserRow(**base)


def test_session_round_trip():
    user = _user()
    session: dict = {"stale": 1}
    auth.start_session(session, user, now=1000)
    assert session == {"uid": str(user.id), "epoch": 3, "iat": 1000}
    claim = auth.session_claim(session)
    assert claim == auth.SessionClaim(user.id, 3, 1000)
    assert auth.session_valid(claim, user, now=1000 + auth.SESSION_MAX_AGE - 1)


@pytest.mark.parametrize("session", [{}, {"uid": "nope", "epoch": 0, "iat": 1}, {"uid": str(uuid.uuid4())},
                                     {"uid": str(uuid.uuid4()), "epoch": "0", "iat": 1},
                                     {"uid": str(uuid.uuid4()), "epoch": True, "iat": 1}])
def test_malformed_sessions_have_no_claim(session):
    assert auth.session_claim(session) is None


def test_session_invalid_when_disabled_epoch_bumped_expired_or_other_user():
    user = _user()
    claim = auth.SessionClaim(user.id, 3, int(time.time()))
    now = claim.iat
    assert auth.session_valid(claim, user, now=now)
    assert not auth.session_valid(claim, None, now=now)
    assert not auth.session_valid(claim, _user(id=user.id, disabled=True), now=now)
    assert not auth.session_valid(claim, _user(id=user.id, session_epoch=4), now=now)
    assert not auth.session_valid(claim, _user(), now=now)
    assert not auth.session_valid(claim, user, now=now + auth.SESSION_MAX_AGE + 1)
    assert not auth.session_valid(claim, user, now=now - 3600)      # issued in the future (clock/forgery)


# ---- Google OIDC 설정(T12) ------------------------------------------------------------

GOOGLE = {"YTC_GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "YTC_GOOGLE_CLIENT_SECRET": "google-secret-x"}


def test_google_settings_and_redirect_uri_come_from_env():
    s = AuthSettings.from_env(_env(**GOOGLE, YTC_PUBLIC_URL="https://YTC.example.com:8443/"))
    assert s.google is not None and s.google.client_id == "cid.apps.googleusercontent.com"
    assert s.google.redirect_uri == "https://ytc.example.com:8443/auth/google/callback"
    assert "google-secret-x" not in repr(s)


@pytest.mark.parametrize("only", ["YTC_GOOGLE_CLIENT_ID", "YTC_GOOGLE_CLIENT_SECRET"])
def test_google_needs_both_id_and_secret(only):
    env = {"YTC_PUBLIC_URL": "http://localhost:8080", "YTC_SESSION_SECRET": SECRET, "YTC_ENV": "dev", only: GOOGLE[only]}
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(env)
    assert e.value.code == "config_missing"
    missing = ({"YTC_GOOGLE_CLIENT_ID", "YTC_GOOGLE_CLIENT_SECRET"} - {only}).pop()
    assert missing in e.value.message


def test_production_without_any_login_method_refuses_to_start():
    env = _env()
    for key in GOOGLE:
        env.pop(key, None)
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(env)
    assert e.value.code == "config_missing" and "YTC_GOOGLE_CLIENT_ID" in e.value.message


def test_outside_production_google_is_optional():
    s = AuthSettings.from_env({"YTC_PUBLIC_URL": "http://localhost:8080", "YTC_SESSION_SECRET": SECRET, "YTC_ENV": "dev"})
    assert s.google is None and not s.dev_login


# ---- 이메일+비밀번호 로그인 설정(T15) -------------------------------------------------------

def _no_google(**over: str) -> dict[str, str]:
    env = _env(**over)
    for key in GOOGLE:
        env.pop(key, None)
    return env


@pytest.mark.parametrize(("google", "password", "starts"), [
    (True, False, True),            # google only
    (False, True, True),            # password only
    (True, True, True),             # both
    (False, False, False),          # neither: no way to sign in
])
def test_production_login_method_matrix(google, password, starts):
    env = _env() if google else _no_google()
    if password:
        env["YTC_PASSWORD_LOGIN"] = "1"
    if not starts:
        with pytest.raises(ScriptError) as e:
            AuthSettings.from_env(env)
        assert e.value.code == "config_missing"
        assert "YTC_PASSWORD_LOGIN" in e.value.message and "YTC_GOOGLE_CLIENT_ID" in e.value.message
        return
    s = AuthSettings.from_env(env)
    assert s.production and s.password_login is password and (s.google is not None) is google


def test_password_login_is_off_unless_enabled():
    assert AuthSettings.from_env(_env()).password_login is False
    assert AuthSettings.from_env(_env(YTC_PASSWORD_LOGIN="")).password_login is False
    assert AuthSettings.from_env(_env(YTC_PASSWORD_LOGIN="0")).password_login is False


@pytest.mark.parametrize("value", ["yes", "true", "2", "on"])
def test_password_login_flag_values(value):
    with pytest.raises(ScriptError) as e:
        AuthSettings.from_env(_env(YTC_PASSWORD_LOGIN=value))
    assert e.value.code == "config_invalid" and "YTC_PASSWORD_LOGIN" in e.value.message


def test_password_login_works_outside_production_too():
    s = AuthSettings.from_env(_no_google(YTC_ENV="dev", YTC_PUBLIC_URL="http://localhost:8080", YTC_PASSWORD_LOGIN="1"))
    assert s.password_login and not s.production
