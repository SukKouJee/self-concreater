"""비밀번호 해시·검증·규칙(서버 설계 §4.2 이메일+비밀번호 로그인, T15). 표준 라이브러리만 쓴다(`hashlib.scrypt`).

- 저장 형식(스스로 매개변수를 말하는 문자열): `scrypt$N$r$p$<salt b64>$<hash b64>`(base64 표준 알파벳, `=` 패딩 없음).
  기본값 N=2^15·r=8·p=1(메모리 약 32MB, 작은 VM에서 한 번에 수십~100ms), 사용자마다 16바이트 무작위 salt, 키 32바이트.
  나중에 매개변수를 올려도 예전 해시는 자기 매개변수로 검증된다.
- 검증(`verify_password`)은 상수 시간 비교(`hmac.compare_digest`). 저장된 해시가 없으면(모르는 이메일·비밀번호 없는 계정)
  같은 비용의 더미 해시로 한 번 계산하고 False: 응답 시간으로 계정이 있는지 알 수 없게 한다(호출부는 비활성 계정도 검증을 끝낸 뒤 거부).
- 저장된 값이 형식에 맞지 않으면 `BadHash`(조용히 False로 바꾸지 않는다: 운영자가 고쳐야 하는 데이터 오류).
- 규칙(`check_policy`): 10자 이상(공백만은 안 됨), 1024자 이하. 위반은 `ScriptError("weak_password")`, 메시지에 비밀번호를 넣지 않는다.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import secrets
from typing import Final

from scripts.common import ScriptError

SCHEME: Final = "scrypt"
SCRYPT_N: Final = 2 ** 15
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SALT_BYTES: Final = 16
KEY_BYTES: Final = 32
MAXMEM: Final = 64 * 1024 * 1024           # > 128*N*r*p (32 MiB) for the defaults; stdlib default 32 MiB is too small
MIN_LENGTH: Final = 10
MAX_LENGTH: Final = 1024                   # characters; longer input is refused before hashing
# Accepted parameter ranges when reading a stored hash (a corrupt row must not make one login allocate gigabytes)
_N_RANGE: Final = (2 ** 10, 2 ** 20)
_R_RANGE: Final = (1, 32)
_P_RANGE: Final = (1, 16)
_MAX_COST_BYTES: Final = 256 * 1024 * 1024


class BadHash(ValueError):
    """저장된 비밀번호 해시가 `scrypt$N$r$p$salt$hash` 형식이 아니다(데이터 오류)."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError) as e:
        raise BadHash("not base64") from e


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    maxmem = max(MAXMEM, 2 * 128 * n * r * p)
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=maxmem, dklen=KEY_BYTES)


def hash_password(password: str) -> str:
    """새 해시(무작위 salt). 규칙 검사는 호출부(`check_policy`)가 먼저 한다."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def _int_in(text: str, bounds: tuple[int, int]) -> int:
    if not text.isdigit():
        raise BadHash("parameter is not a number")
    value = int(text)
    if not bounds[0] <= value <= bounds[1]:
        raise BadHash("parameter out of range")
    return value


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise BadHash("not a scrypt hash")
    n, r, p = _int_in(parts[1], _N_RANGE), _int_in(parts[2], _R_RANGE), _int_in(parts[3], _P_RANGE)
    if n & (n - 1):
        raise BadHash("N must be a power of two")
    if 128 * n * r * p > _MAX_COST_BYTES:
        raise BadHash("parameters need too much memory")
    salt, digest = _unb64(parts[4]), _unb64(parts[5])
    if len(salt) < 8 or len(digest) != KEY_BYTES:
        raise BadHash("salt or hash has the wrong length")
    return n, r, p, salt, digest


@functools.cache
def dummy_hash() -> str:
    """모르는 이메일·비밀번호 없는 계정에 쓰는 더미 해시(프로세스마다 한 번 만든다, 같은 매개변수)."""
    return hash_password(secrets.token_urlsafe(24))


def encodable(text: str) -> bool:
    """UTF-8로 바꿀 수 있는가(JSON 이스케이프로 들어온 짝 없는 서로게이트 U+D800~U+DFFF는 안 된다)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def verify_password(password: str, encoded: str | None) -> bool:
    """맞으면 True. encoded가 None이거나 비밀번호가 너무 길거나 UTF-8로 못 바꾸면 같은 비용의 계산을 하고 False.
    저장된 값이 깨졌으면 BadHash."""
    target = dummy_hash() if encoded is None else encoded
    n, r, p, salt, digest = _parse(target)
    usable = encoded is not None and len(password) <= MAX_LENGTH and encodable(password)
    candidate = _derive(password if usable else "", salt, n, r, p)
    same = hmac.compare_digest(candidate, digest)
    return usable and same


def check_policy(password: str) -> str:
    """새 비밀번호 규칙. 통과하면 그대로 돌려준다(앞뒤 공백도 비밀번호의 일부)."""
    if not password.strip() or len(password) < MIN_LENGTH:
        raise ScriptError("weak_password", f"비밀번호는 {MIN_LENGTH}자 이상이어야 합니다(공백만으로는 안 됩니다).")
    if len(password) > MAX_LENGTH:
        raise ScriptError("weak_password", f"비밀번호는 {MAX_LENGTH}자 이하여야 합니다.")
    if not encodable(password):
        raise ScriptError("weak_password", "비밀번호에 쓸 수 없는 문자가 있습니다(깨진 유니코드 문자).")
    return password
