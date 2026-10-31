"""ServerBackend·export_tree(서버 설계 §2·§3·§10-2). PG 16 + MinIO가 없으면 skip(`tests/helpers/pg.py`)."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from scripts import ctx as ctx_mod
from scripts import keys
from scripts.common import ScriptError
from scripts.ctx import Ctx
from tests.helpers.pg import Stack, TestDb, pg_ctx, pg_user

VID = "Q8PSsikq8PA"


@pytest.fixture
def backend(pg_backend: Any) -> Any:
    return pg_backend


@pytest.mark.parametrize("bad", [None, "", "not-a-uuid", "../x"])
def test_missing_or_malformed_user_id_is_login_required(backend: Any, bad: str | None) -> None:
    for make in (backend.store, backend.objects, backend.shared, backend.scratch, backend.trash, backend.usage):
        with pytest.raises(ScriptError) as e:
            make(bad)
        assert e.value.code == "login_required"


def test_unknown_user_store_is_login_required(backend: Any) -> None:
    with pytest.raises(ScriptError) as e:
        backend.store(str(uuid.uuid4()))
    assert e.value.code == "login_required"


def test_ctx_stores_are_the_server_implementations_for_that_user(request: pytest.FixtureRequest) -> None:
    from server.pgstore import PgDocStore, PgTrash
    from server.s3store import S3ObjectStore, S3SharedCache
    from server.scratch import ServerScratch
    from server.usage import PgUsageMeter

    c = pg_ctx(request, "a@example.com")
    assert isinstance(c.store, PgDocStore) and c.store.label() == "ytc:a@example.com"
    assert isinstance(c.objects, S3ObjectStore) and isinstance(c.shared, S3SharedCache)
    assert isinstance(c.scratch, ServerScratch) and isinstance(c.trash, PgTrash)
    assert isinstance(c.usage, PgUsageMeter)
    assert c.scratch.tmp_path("x.json").parent.name == c.user_id


def test_two_users_through_ctx_are_isolated(request: pytest.FixtureRequest) -> None:
    a, b = pg_ctx(request, "a@example.com"), pg_ctx(request, "b@example.com")
    with ctx_mod.use(a):
        ctx_mod.here().store.write(keys.PROFILE, b"---\nx: 1\n---\n")
        ctx_mod.here().objects.put(keys.CONFIG, b"{}")
    with ctx_mod.use(b):
        assert not ctx_mod.here().store.exists(keys.PROFILE)
        assert not ctx_mod.here().objects.exists(keys.CONFIG)
        assert ctx_mod.here().store.label() == "ytc:b@example.com"


def test_open_from_settings_uses_pool_sizes_and_clears_scratch(migrated_db: TestDb, pg_stack: Stack,
                                                              s3_bucket: Any, tmp_path: Path) -> None:
    from server.backend import ServerBackend
    from server.config import StorageSettings

    stale = tmp_path / "tmp" / str(uuid.uuid4())
    stale.mkdir(parents=True)
    (stale / "left.json").write_text("{}", encoding="utf-8")
    settings = StorageSettings.from_env({
        "YTC_DB_DSN": migrated_db.app_dsn, "YTC_S3_ENDPOINT": pg_stack.s3_endpoint,
        "YTC_S3_ACCESS_KEY": pg_stack.s3_access_key, "YTC_S3_SECRET_KEY": pg_stack.s3_secret_key,
        "YTC_S3_BUCKET": s3_bucket.name, "YTC_DB_POOL_MAX": "3", "YTC_S3_MAX_POOL_CONNECTIONS": "7",
        "YTC_SCRATCH_DIR": str(tmp_path / "tmp"), "YTC_STATE_DIR": str(tmp_path / "state")})
    backend = ServerBackend.open(settings)
    try:
        assert not stale.exists()
        assert backend.bucket.client.meta.config.max_pool_connections == 7
        assert backend.db._pool.max_size == 3
        uid = pg_user(migrated_db.owner_dsn, "c@example.com")
        c = Ctx(user_id=str(uid), backend=backend)
        c.objects.put(keys.CONFIG, b"{}")
        assert c.objects.get(keys.CONFIG) == b"{}"
    finally:
        backend.close()


# --- export_tree ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_export_tree_dumps_docs_objects_shared_refs_and_trash(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    from server.backend import export_tree

    backend = request.getfixturevalue("pg_backend")
    a, b = pg_ctx(request, "a@example.com"), pg_ctx(request, "b@example.com")
    video = keys.video_md("travel", VID)
    with ctx_mod.use(a):
        a.store.ensure_folder(keys.MINE)
        a.store.write(video, "---\nid: x\n---\n# 🏆\r\n".encode())
        a.store.write(keys.draft_md("gone"), b"---\nx: 1\n---\n")
        a.objects.put(keys.CONFIG, b"{\r\n}")
        a.shared.put(keys.raw(VID), b"raw")
        a.shared.put(keys.frames_dir(VID) + "0001.jpg", b"jpg")
        a.shared.put(keys.raw("other_vid12"), b"not referenced")
        moved = a.trash.move([keys.draft_md("gone")], last=keys.draft_md("gone"))
    with ctx_mod.use(b):
        b.store.write(keys.PROFILE, b"---\nb: 1\n---\n")

    out = export_tree(backend, a.user_id)
    stamp = moved["trash"].removeprefix(keys.TRASH)
    assert out.hashes() == dict(sorted({
        video: _sha("---\nid: x\n---\n# 🏆\r\n".encode()),
        keys.CONFIG: _sha(b"{\r\n}"),
        keys.raw(VID): _sha(b"raw"),
        keys.frames_dir(VID) + "0001.jpg": _sha(b"jpg"),
        f"{keys.TRASH}{stamp}/{keys.draft_md('gone')}": _sha(b"---\nx: 1\n---\n"),
    }.items()))
    assert keys.MINE in out.folders and keys.DRAFTS in out.folders and keys.videos_dir("travel") in out.folders
    assert not [f for f in out.folders if f.startswith(keys.SHARED_PREFIXES)]
    assert keys.PROFILE not in out.files                     # B's doc is not A's

    dest = tmp_path / "out"
    out.write(dest)
    assert (dest / "refs" / "ref_travel" / "videos" / f"{VID}.md").read_bytes() == "---\nid: x\n---\n# 🏆\r\n".encode()
    assert (dest / "mine").is_dir()
    no_trash = export_tree(backend, a.user_id, trash=False)
    assert f"{keys.TRASH}{stamp}/{keys.draft_md('gone')}" not in no_trash.files
