"""서버판 이음매(T10): 판(edition) 문맥, JSON 본문 파싱 함수, 라이브러리를 못 쓰는 판에서 라이브러리 출처가 500을 내지 않음.

- `server.edition(name, disabled)`: 블록 동안(그 스레드 문맥에서) 판 이름과 항상 끄는 기능을 바꾼다. 기본은 로컬판 그대로.
- `server.parse_json_body(raw)`: stdlib 핸들러의 `_read_json`과 같은 규칙(서버 어댑터가 같은 함수를 쓴다).
- `library.clone_dir()`: 저장소가 라이브러리 폴더를 줄 수 없으면(서버판 scratch) ScriptError `library_unavailable`.
"""
import contextvars
import threading

import pytest

from scripts import ctx, library
from scripts.common import ScriptError
from scripts.ui import api_draft, api_help, server
from tests.test_ui_server import client, no_browser, temp_routes  # noqa: F401  (fixture 재사용)


# ---- edition --------------------------------------------------------------------

def test_default_edition_is_local_with_nothing_forced(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    assert server.current_edition() == "local"
    assert server.disabled_features() == {}


def test_edition_scope_forces_features_and_restores(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    with server.edition("server", ["library", "footage"]):
        assert server.current_edition() == "server"
        assert set(server.disabled_features()) == {"library", "footage"}
        assert server.feature_guard("/api/footage/process")[0] == 403
    assert server.current_edition() == "local"
    assert server.disabled_features() == {}
    assert server.feature_guard("/api/footage/process") is None


def test_edition_scope_unions_env(monkeypatch):
    monkeypatch.setenv(server.DISABLE_ENV, "srtfit")
    with server.edition("server", ["library"]):
        assert set(server.disabled_features()) == {"library", "srtfit"}


def test_edition_scope_rejects_unknown_feature():
    with pytest.raises(ValueError):
        with server.edition("server", ["nope"]):
            pass


def test_edition_scope_is_per_thread_context(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    seen: list[str] = []
    with server.edition("server", ["library"]):
        other = threading.Thread(target=lambda: seen.append(server.current_edition()))
        other.start()
        other.join()
        inherited = threading.Thread(target=contextvars.copy_context().run,
                                     args=(lambda: seen.append(server.current_edition()),))
        inherited.start()
        inherited.join()
    assert seen == ["local", "server"]   # plain threads do not see it; copied contexts (job threads) do


def test_features_route_reports_edition(client, monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    status, body = client.json("GET", "/api/features")
    assert status == 200 and body == {"edition": "local", "disabled": {}}


# ---- parse_json_body -------------------------------------------------------------

def test_parse_json_body_rules():
    assert server.parse_json_body(b"") == {}
    assert server.parse_json_body('{"a": "한"}'.encode()) == {"a": "한"}
    for raw in (b"[1]", b"nope", b"\xff\xfe"):
        with pytest.raises(ScriptError) as e:
            server.parse_json_body(raw)
        assert e.value.code == "bad_json"


# ---- library on an edition without a library folder --------------------------------

def test_scrub_without_work_folder_masks_paths_only():
    # server edition: Ctx(root=None). scrub must not need a work folder (llm_anthropic / library error paths use it)
    from scripts.common import PATH_ANY, scrub
    with ctx.use(ctx.Ctx(user_id="u", backend=object())):
        out = scrub("https://u:pw@example.com/x 실패 /var/lib/ytc/state/a.json")
    assert "pw@" not in out and "/var/lib" not in out and PATH_ANY in out


class _NoLibraryScratch:
    def local_path(self, *parts: str):
        raise NotImplementedError("서버판에는 라이브러리 폴더가 없다")


class _Ctx:
    root = None
    scratch = _NoLibraryScratch()


def test_clone_dir_without_library_folder_is_a_script_error(monkeypatch):
    monkeypatch.setattr(ctx, "here", lambda: _Ctx())
    with pytest.raises(ScriptError) as e:
        library.clone_dir()
    assert e.value.code == "library_unavailable"


def test_library_templates_skip_the_library_when_disabled(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("library must not be touched when the feature is off")

    monkeypatch.setattr(library, "status", boom)
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    with server.edition("server", ["library"]):
        assert api_draft._library_templates() is None


def test_help_library_situation_when_disabled(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("library must not be touched when the feature is off")

    monkeypatch.setattr(library, "status", boom)
    monkeypatch.setattr(api_help.status, "overview", lambda category=None: {
        "initialized": False, "categories": [], "mission": {"date": "d", "exists": False, "status": None, "kind": None}})
    with server.edition("server", ["library"]):
        assert api_help.situation(None, "library")["library"] == {"connected": False}
