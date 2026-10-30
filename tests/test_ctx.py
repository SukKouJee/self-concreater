"""실행 문맥(Ctx): work_root()가 문맥을 먼저 보고, 없으면 YT_COACH_HOME·현재 폴더를 본다."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scripts import common, count_work_root, ctx, filestore, keys
from scripts.ui.jobs import Job, JobManager


@pytest.fixture(autouse=True)
def _no_default():
    """set_default는 프로세스 전역이라 테스트마다 되돌린다."""
    saved = ctx._default
    ctx._default = None
    yield
    ctx._default = saved


def test_work_root_prefers_ctx(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "env"))
    with ctx.use(ctx.Ctx(root=tmp_path / "a")):
        assert common.work_root() == tmp_path / "a"
        assert common.workspace_dir("raw") == tmp_path / "a" / "workspace" / "raw"
        assert common.category_dir("travel") == tmp_path / "a" / "refs" / "ref_travel"


def test_work_root_falls_back_to_env_and_cwd(monkeypatch, tmp_path: Path) -> None:
    assert ctx.current() is None
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert common.work_root() == tmp_path
    monkeypatch.delenv("YT_COACH_HOME")
    monkeypatch.chdir(tmp_path)
    assert common.work_root() == Path.cwd()


def test_use_restores_previous(tmp_path: Path) -> None:
    a, b = ctx.Ctx(root=tmp_path / "a"), ctx.Ctx(root=tmp_path / "b", user_id="u1")
    with ctx.use(a):
        with ctx.use(b):
            assert ctx.current() == b
        assert ctx.current() == a
    assert ctx.current() is None


def test_use_restores_previous_on_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), ctx.use(ctx.Ctx(root=tmp_path)):
        raise RuntimeError("boom")
    assert ctx.current() is None


def test_set_default_is_seen_when_no_ctx_and_use_overrides_it(tmp_path: Path) -> None:
    ctx.set_default(ctx.Ctx(root=tmp_path / "d"))
    assert common.work_root() == tmp_path / "d"
    seen: list[Path] = []
    t = threading.Thread(target=lambda: seen.append(common.work_root()))   # 새 스레드에서도 기본값
    t.start()
    t.join(5)
    assert seen == [tmp_path / "d"]
    with ctx.use(ctx.Ctx(root=tmp_path / "a")):
        assert common.work_root() == tmp_path / "a"


def test_ctx_is_frozen(tmp_path: Path) -> None:
    c = ctx.Ctx(root=tmp_path)
    assert c.user_id is None
    with pytest.raises(AttributeError):
        c.root = tmp_path / "x"  # type: ignore[misc]


def test_run_sets_ctx_only_for_the_call(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "first"))
    common.run(lambda argv: {"root": str(ctx.current().root)}, [])
    assert json.loads(capsys.readouterr().out) == {"root": str(tmp_path / "first")}
    assert ctx.current() is None                      # 다음 호출의 환경 변수 변경을 막지 않는다
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "second"))
    assert common.work_root() == tmp_path / "second"


def test_job_thread_sees_ctx(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "env"))
    seen: dict[str, Path] = {}

    def target(job: Job) -> None:
        seen["job"] = common.work_root()

    jobs = JobManager()
    with ctx.use(ctx.Ctx(root=tmp_path / "a")):
        job = jobs.start("typify", target)
    job.join(5)
    other: list[Path] = []
    t = threading.Thread(target=lambda: other.append(common.work_root()))
    t.start()
    t.join(5)
    assert seen["job"] == tmp_path / "a"
    assert other == [tmp_path / "env"]                 # 다른 스레드는 문맥 없이 환경 변수를 본다
    assert common.work_root() == tmp_path / "env"


def test_job_thread_without_ctx_follows_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    seen: list[Path] = []
    job = JobManager().start("typify", lambda job: seen.append(common.work_root()))
    job.join(5)
    assert seen == [tmp_path]


def test_two_ctx_isolated_in_threads(tmp_path: Path) -> None:
    barrier = threading.Barrier(2, timeout=5)
    seen: dict[str, list[Path]] = {"a": [], "b": []}

    def work(name: str) -> None:
        with ctx.use(ctx.Ctx(root=tmp_path / name, user_id=name)):
            for _ in range(50):
                barrier.wait()                         # 두 스레드가 번갈아 같은 순간에 읽는다
                seen[name].append(common.work_root())

    threads = [threading.Thread(target=work, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert seen["a"] == [tmp_path / "a"] * 50
    assert seen["b"] == [tmp_path / "b"] * 50


def test_count_work_root_outputs_modules() -> None:
    out = count_work_root.main([])
    assert set(out) == {"modules", "total"}
    assert set(out["total"]) == {"work_root", "workspace_dir", "category_dir", "all"}
    assert out["modules"], "scripts/ 아래에 호출이 하나 이상 있다"
    for module, counts in out["modules"].items():
        assert module.startswith("scripts/") and module.endswith(".py")
        assert set(counts) == {"work_root", "workspace_dir", "category_dir", "all"}
        assert counts["all"] == counts["work_root"] + counts["workspace_dir"] + counts["category_dir"] > 0
    assert out["total"]["all"] == sum(c["all"] for c in out["modules"].values())
    assert "scripts/count_work_root.py" not in out["modules"]


def test_count_calls_skips_definitions_and_lookalikes() -> None:
    text = ("def work_root() -> Path:\n"
            "    x = work_root() / common.workspace_dir('a')\n"
            "    y = category_dir(c), my_work_root(), self.workspace_dir_x()\n")
    assert count_work_root.count_calls(text) == {"work_root": 1, "workspace_dir": 1, "category_dir": 1, "all": 3}


# --- 저장소 속성 · here() (S3 §3.2, ADR-7) ---------------------------------------------

def test_here_returns_current_ctx_or_a_fresh_one_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "one"))
    first = ctx.here()
    assert first.root == tmp_path / "one" and ctx.current() is None      # 문맥을 설정하지 않는다
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "two"))
    assert ctx.here().root == tmp_path / "two"                           # 캐시하지 않는다: 바뀐 환경 변수를 본다
    assert ctx.here().store.label() == str(tmp_path / "two")
    monkeypatch.delenv("YT_COACH_HOME")
    monkeypatch.chdir(tmp_path)
    assert ctx.here().root == Path.cwd()
    c = ctx.Ctx(root=tmp_path / "a")
    with ctx.use(c):
        assert ctx.here() is c
    ctx.set_default(ctx.Ctx(root=tmp_path / "d"))
    assert ctx.here().root == tmp_path / "d"


def test_work_root_is_here_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert common.work_root() == ctx.here().root == tmp_path


def test_store_properties_are_file_stores_built_once(tmp_path: Path) -> None:
    c = ctx.Ctx(root=tmp_path)
    assert isinstance(c.store, filestore.FileDocStore)
    assert isinstance(c.objects, filestore.FileObjectStore)
    assert isinstance(c.shared, filestore.FileSharedCache)
    assert isinstance(c.scratch, filestore.FileScratch)
    assert isinstance(c.trash, filestore.FileTrash)
    assert c.store is c.store and c.objects is c.objects and c.trash is c.trash
    assert c.store.label() == str(tmp_path)
    assert c == ctx.Ctx(root=tmp_path)                                   # 저장소는 비교·해시에 들어가지 않는다
    assert hash(c) == hash(ctx.Ctx(root=tmp_path))
    with pytest.raises(AttributeError):
        c.root = tmp_path / "x"  # type: ignore[misc]


def test_ctx_without_root_or_backend_has_no_stores() -> None:
    c = ctx.Ctx(user_id="u1")
    assert c.root is None
    with pytest.raises(RuntimeError):
        c.store
    with ctx.use(c), pytest.raises(RuntimeError):
        common.work_root()


def test_backend_supplies_stores(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []

    class Backend:
        def _make(self, name: str, user_id: str | None) -> object:
            calls.append((name, user_id))
            return f"{name}:{user_id}"

        def store(self, user_id: str | None):
            return self._make("store", user_id)

        def objects(self, user_id: str | None):
            return self._make("objects", user_id)

        def shared(self, user_id: str | None):
            return self._make("shared", user_id)

        def scratch(self, user_id: str | None):
            return self._make("scratch", user_id)

        def trash(self, user_id: str | None):
            return self._make("trash", user_id)

    c = ctx.Ctx(user_id="u1", backend=Backend())
    assert (c.store, c.objects, c.shared, c.scratch, c.trash) == (
        "store:u1", "objects:u1", "shared:u1", "scratch:u1", "trash:u1")
    assert c.store == "store:u1" and len(calls) == 5
    assert c == ctx.Ctx(user_id="u1")


def test_local_file_picks_store_by_key_kind(tmp_path: Path) -> None:
    c = ctx.Ctx(root=tmp_path)
    for key in [keys.video_md("travel", "abc"), keys.raw("abc"), keys.conti_html("drafts/a-conti.md")]:
        with c.local_file(key) as p:
            assert p == tmp_path / key
    with pytest.raises(ValueError):
        with filestore.FileSharedCache(tmp_path).local_path("drafts/a-conti.html"):
            pass
