"""password_hash: 이메일+비밀번호 로그인(서버 설계 §4.2, T15).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02

- `users.password_hash text NULL`: `scrypt$N$r$p$salt$hash`(server.passwords) 또는 NULL(그 계정은 비밀번호 로그인 불가).
  CHECK로 형식 접두사만 묶는다(평문이 실수로 들어가지 않게). 값은 관리 CLI(owner 롤)만 쓴다.
- 앱 롤 권한은 그대로다: users에 대한 SELECT(표 전체)로 로그인 때 읽고, UPDATE는 0001의 (google_sub, display_name)뿐이라
  앱은 password_hash를 바꿀 수 없다.
- 되돌리면 열(과 CHECK)만 지운다. 사용자 행은 남는다.
"""
from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN password_hash text "
               "CONSTRAINT users_password_hash_format CHECK (password_hash IS NULL OR password_hash LIKE 'scrypt$%')")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN password_hash")
