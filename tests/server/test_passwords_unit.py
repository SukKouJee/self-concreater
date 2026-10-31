"""이메일+비밀번호 로그인의 순수 부분(서버 설계 §4.2, T15): scrypt 해시 형식·검증·더미 해시 경로, 비밀번호 규칙, 실패 횟수 제한.

PG·MinIO 없이 돈다. ASGI 경로는 test_password_login.py, PG(관리 CLI·마이그레이션)는 test_admin.py·test_db_schema.py.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from scripts.common import ScriptError
from server import login_limit, passwords

PW = "correct horse battery"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


# ---- 해시 형식 -----------------------------------------------------------------------------

def test_hash_is_self_describing_scrypt_with_design_parameters():
    encoded = passwords.hash_password(PW)
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2 ** 15), "8", "1")
    assert len(_unb64(salt)) == passwords.SALT_BYTES == 16
    assert len(_unb64(digest)) == passwords.KEY_BYTES == 32
    assert PW not in encoded


def test_every_hash_has_its_own_salt():
    assert passwords.hash_password(PW) != passwords.hash_password(PW)


def test_hash_matches_stdlib_scrypt_with_the_stored_salt():
    encoded = passwords.hash_password(PW)
    _, n, r, p, salt, digest = encoded.split("$")
    raw_salt = _unb64(salt)
    expected = hashlib.scrypt(PW.encode("utf-8"), salt=raw_salt, n=int(n), r=int(r), p=int(p),
                              maxmem=passwords.MAXMEM, dklen=passwords.KEY_BYTES)
    assert _b64(expected) == digest


def test_verify_accepts_the_right_password_and_refuses_others():
    encoded = passwords.hash_password(PW)
    assert passwords.verify_password(PW, encoded)
    assert not passwords.verify_password(PW + " ", encoded)
    assert not passwords.verify_password("", encoded)
    assert not passwords.verify_password(PW.upper(), encoded)


def test_verify_handles_non_ascii_passwords():
    encoded = passwords.hash_password("비밀번호는한글로도된다")
    assert passwords.verify_password("비밀번호는한글로도된다", encoded)
    assert not passwords.verify_password("비밀번호는한글로도된다!", encoded)


def test_verify_compares_in_constant_time(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[bytes, bytes]] = []
    real = passwords.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(passwords.hmac, "compare_digest", spy)
    passwords.verify_password(PW, passwords.hash_password(PW))
    assert len(seen) == 1


@pytest.mark.parametrize("bad", [
    "", "plain", "bcrypt$12$x", "scrypt$32768$8$1$c2FsdA",                  # wrong scheme / too few fields
    "scrypt$32767$8$1$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,                     # n not a power of two
    "scrypt$2$8$1$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,                         # n too small
    f"scrypt${2 ** 22}$8$1$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,                # n too large (memory)
    "scrypt$32768$0$1$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,
    "scrypt$32768$8$99$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,
    "scrypt$32768$8$1$!!!$" + "A" * 43,                                      # salt not base64
    "scrypt$32768$8$1$c2FsdHNhbHRzYWx0c2E$AAAA",                            # digest too short
    "scrypt$x$8$1$c2FsdHNhbHRzYWx0c2E$" + "A" * 43,
])
def test_malformed_stored_hash_is_bad_hash_not_a_silent_false(bad: str):
    with pytest.raises(passwords.BadHash):
        passwords.verify_password(PW, bad)


# ---- 더미 해시(사용자 열거·시간 차 방지) ------------------------------------------------------

def _count_scrypt(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = hashlib.scrypt

    def counting(*args, **kwargs):
        calls.append(kwargs.get("n", 0))
        return real(*args, **kwargs)

    passwords.dummy_hash()                     # warm the cached dummy before counting
    monkeypatch.setattr(passwords.hashlib, "scrypt", counting)
    return calls


def test_verify_without_a_stored_hash_still_runs_one_full_cost_scrypt(monkeypatch: pytest.MonkeyPatch):
    calls = _count_scrypt(monkeypatch)
    assert passwords.verify_password(PW, None) is False
    assert calls == [passwords.SCRYPT_N]


def test_verify_with_a_stored_hash_runs_exactly_one_scrypt(monkeypatch: pytest.MonkeyPatch):
    encoded = passwords.hash_password(PW)
    calls = _count_scrypt(monkeypatch)
    passwords.verify_password("wrong password!", encoded)
    assert calls == [passwords.SCRYPT_N]


def test_dummy_hash_uses_the_same_parameters_and_is_cached():
    assert passwords.dummy_hash() is passwords.dummy_hash()
    assert passwords.dummy_hash().split("$")[:4] == ["scrypt", str(passwords.SCRYPT_N), "8", "1"]


def test_overlong_password_is_refused_without_hashing_but_after_the_same_work(monkeypatch: pytest.MonkeyPatch):
    encoded = passwords.hash_password(PW)
    calls = _count_scrypt(monkeypatch)
    assert passwords.verify_password("x" * (passwords.MAX_LENGTH + 1), encoded) is False
    assert calls == [passwords.SCRYPT_N]       # same cost as a wrong password (dummy run)


# ---- 비밀번호 규칙 -----------------------------------------------------------------------------

@pytest.mark.parametrize("ok", ["a" * 10, "열글자비밀번호입니다", "  spaces ok  ", "x" * passwords.MAX_LENGTH])
def test_policy_accepts_ten_or_more_characters(ok: str):
    assert passwords.check_policy(ok) == ok


@pytest.mark.parametrize("bad", ["", "short", "a" * 9, " " * 12, "\t\n" * 6, "x" * (passwords.MAX_LENGTH + 1)])
def test_policy_refuses_short_blank_or_overlong(bad: str):
    with pytest.raises(ScriptError) as e:
        passwords.check_policy(bad)
    assert e.value.code == "weak_password"
    assert bad.strip() == "" or bad not in e.value.message           # never echo the password back


def test_policy_message_is_korean_and_names_the_minimum():
    with pytest.raises(ScriptError) as e:
        passwords.check_policy("abc")
    assert "10자 이상" in e.value.message


# ---- 실패 횟수 제한 -----------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_window_blocks_after_max_failures_and_reports_retry_after(clock: Clock):
    w = login_limit.FailureWindow(max_failures=3, window=600, clock=clock)
    for _ in range(2):
        w.fail("k")
        clock.t += 10
    assert w.retry_after("k") is None
    w.fail("k")                                              # third failure at t=1020
    assert w.retry_after("k") == 580                         # oldest (t=1000) leaves the window at 1600
    clock.t = 1599.5
    assert w.retry_after("k") == 1                           # rounded up
    clock.t = 1600
    assert w.retry_after("k") is None                        # sliding window: the first failure expired


def test_window_keys_are_independent_and_reset_clears_one(clock: Clock):
    w = login_limit.FailureWindow(max_failures=2, window=600, clock=clock)
    w.fail("a")
    w.fail("a")
    w.fail("b")
    assert w.retry_after("a") is not None and w.retry_after("b") is None
    w.reset("a")
    assert w.retry_after("a") is None


def test_window_memory_is_bounded(clock: Clock):
    w = login_limit.FailureWindow(max_failures=2, window=600, clock=clock, max_keys=100)
    for i in range(500):
        w.fail(f"k{i}")
    assert len(w) <= 100
    w.fail("k499")
    assert w.retry_after("k499") is not None                 # the newest keys are kept


def test_login_limiter_counts_per_email_and_per_ip(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=5, ip_failures=20, window=600, clock=clock)
    for i in range(5):
        assert limits.blocked("a@example.com", f"10.0.0.{i}") is None
        limits.failed("a@example.com", f"10.0.0.{i}")
    assert limits.blocked("a@example.com", "10.9.9.9") is not None          # the email is blocked from any IP
    assert limits.blocked("b@example.com", "10.0.0.1") is None              # other emails from those IPs are not


def test_login_limiter_blocks_an_ip_that_tries_many_emails(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=5, ip_failures=20, window=600, clock=clock)
    for i in range(20):
        limits.failed(f"user{i}@example.com", "203.0.113.7")
    assert limits.blocked("fresh@example.com", "203.0.113.7") is not None
    assert limits.blocked("fresh@example.com", "203.0.113.8") is None


def test_success_resets_the_email_counter_but_not_the_ip_counter(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=5, ip_failures=6, window=600, clock=clock)
    for _ in range(4):
        limits.failed("a@example.com", "198.51.100.1")
    limits.succeeded("a@example.com")
    for _ in range(4):
        limits.failed("a@example.com", "198.51.100.2")
    assert limits.blocked("a@example.com", "198.51.100.3") is None           # 4 since the reset
    limits.failed("x@example.com", "198.51.100.1")
    limits.failed("x@example.com", "198.51.100.1")
    assert limits.blocked("y@example.com", "198.51.100.1") is not None       # 4 + 2 from that IP


def test_login_limiter_normalizes_the_email_key(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=2, ip_failures=99, window=600, clock=clock)
    limits.failed(" A@Example.com ", "1.1.1.1")
    limits.failed("a@example.com", "1.1.1.2")
    assert limits.blocked("A@EXAMPLE.COM", "1.1.1.3") is not None


def test_retry_after_is_the_longer_of_the_two_blocks(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=1, ip_failures=1, window=600, clock=clock)
    limits.failed("a@example.com", "1.1.1.1")
    clock.t += 100
    limits.failed("b@example.com", "2.2.2.2")
    assert limits.blocked("a@example.com", "2.2.2.2") == 600                 # ip block (newer) is longer


def test_attempts_in_flight_count_so_parallel_requests_cannot_skip_the_limit(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=3, ip_failures=99, window=600, clock=clock)
    for i in range(3):
        assert limits.begin("a@example.com", f"10.0.0.{i}") is None        # three hashes running at once
    assert limits.begin("a@example.com", "10.0.0.9") == 1                  # a fourth must wait for them
    limits.end("a@example.com", "10.0.0.0", "aborted")                     # e.g. the database failed: not counted
    assert limits.begin("a@example.com", "10.0.0.9") is None
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.9"):
        limits.end("a@example.com", ip, "failed")
    assert limits.begin("a@example.com", "10.0.0.5") == 600                # three recorded failures


def test_end_succeeded_releases_and_resets_the_email(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=2, ip_failures=2, window=600, clock=clock)
    limits.failed("a@example.com", "10.0.0.1")
    assert limits.begin("a@example.com", "10.0.0.2") is None
    limits.end("a@example.com", "10.0.0.2", "succeeded")
    assert limits.begin("a@example.com", "10.0.0.3") is None
    limits.end("a@example.com", "10.0.0.3", "failed")
    assert limits.blocked("a@example.com", "10.0.0.4") is None              # 1 email failure since the reset


def test_end_with_an_unknown_outcome_is_an_error(clock: Clock):
    limits = login_limit.LoginLimiter(clock=clock)
    assert limits.begin("a@example.com", "10.0.0.1") is None
    with pytest.raises(ValueError):
        limits.end("a@example.com", "10.0.0.1", "maybe")


# ---- fix round 1 ----------------------------------------------------------------------------

LONE = chr(0xD800) * 12                      # JSON "\ud800" decodes to this; it cannot be encoded as UTF-8


@pytest.mark.parametrize("stored", ["real", "none"])
def test_unencodable_password_is_a_plain_false_after_one_scrypt(monkeypatch: pytest.MonkeyPatch, stored: str):
    encoded = passwords.hash_password(PW) if stored == "real" else None
    calls = _count_scrypt(monkeypatch)
    assert passwords.verify_password(LONE, encoded) is False
    assert calls == [passwords.SCRYPT_N]


def test_policy_refuses_unencodable_passwords():
    with pytest.raises(ScriptError) as e:
        passwords.check_policy(LONE)
    assert e.value.code == "weak_password"


def test_ipv6_clients_are_counted_per_64_and_ipv4_mapped_as_ipv4():
    assert login_limit.ip_key("2001:db8:1:2:aaaa::1") == login_limit.ip_key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert login_limit.ip_key("2001:db8:1:3::1") != login_limit.ip_key("2001:db8:1:2::1")
    assert login_limit.ip_key("::ffff:192.0.2.7") == "192.0.2.7"
    assert login_limit.ip_key("192.0.2.7") == "192.0.2.7"
    assert login_limit.ip_key("testclient") == "testclient"           # not an address: kept as is


def test_one_ipv6_host_rotating_addresses_in_its_64_is_still_blocked(clock: Clock):
    limits = login_limit.LoginLimiter(email_failures=99, ip_failures=3, window=600, clock=clock)
    for i in range(3):
        limits.failed(f"u{i}@example.com", f"2001:db8:9:9::{i + 1}")
    assert limits.blocked("x@example.com", "2001:db8:9:9:ffff::1") is not None
    assert limits.blocked("x@example.com", "2001:db8:9:a::1") is None


def test_flooding_new_keys_does_not_evict_a_victims_failures(clock: Clock):
    w = login_limit.FailureWindow(max_failures=5, window=600, clock=clock, max_keys=100)
    for _ in range(4):
        w.fail("victim@example.com")
    for i in range(1000):                     # attacker floods one-failure keys
        w.fail(f"flood{i}@example.com")
    assert len(w) <= 100
    w.fail("victim@example.com")
    assert w.retry_after("victim@example.com") is not None    # all five failures are still there
