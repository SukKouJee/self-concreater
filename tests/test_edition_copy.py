"""판별 문구(파이썬 쪽): `scripts.edition` 한 곳의 문맥, `ed(local, server)`, 기능 문구, 도움말 서버판, 게이트.

- 로컬판 문구는 그대로다: `ed()`의 첫 인자 목록(LOCAL_ED_GOLDEN)은 바꾸기 전 raise 자리의 글자 그대로이고, 로컬판에서
  고른 결과도 그 글자다. 서버판은 내 PC 전용 말(작업 폴더·터미널·슬래시 커맨드 …)이 없다.
- 게이트: scripts/·server/의 문자열 중 서버판에서 보일 수 있는 것(`ed()`의 로컬 인자·docstring 제외)에 그런 말이 있으면
  PY_ALLOWED에 이유와 함께 적어야 한다.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import edition, fetch, mission, similarity, stt
from scripts.common import ScriptError
from scripts.edition import ed
from scripts.ui import api_add, api_help, server
from tests.helpers.copy_gate import allowed, hits, py_strings, scan, stale

ROOT = Path(__file__).resolve().parents[1]

# `ed()`의 로컬판 인자(ast.unparse): 바꾸기 전 raise 자리에 있던 그대로. 바꾸면 로컬판 문구가 바뀐다는 뜻이다.
LOCAL_ED_GOLDEN: list[tuple[str, str]] = sorted([
    ("scripts/conti.py", "f'기획안이 없습니다: {key} (먼저 /yt-draft)'"),
    ("scripts/conti.py", "f'작업 폴더 밖의 파일은 고정할 수 없습니다: {ref}'"),
    ("scripts/draft.py", "f'아키타입이 없습니다: {source} (먼저 /yt-typify)'"),
    ("scripts/fetch.py", "BLOCKED_MESSAGE"),
    ("scripts/fetch.py", "_YTDLP_MESSAGES[code]"),
    ("scripts/llm.py", "message"),
    ("scripts/llm_anthropic.py", "f'{KEY_ENV} 환경 변수가 없습니다. API 키를 환경 변수로 설정하세요'"),
    ("scripts/llm_anthropic.py", "f'Anthropic API 인증에 실패했습니다({detail}). ANTHROPIC_API_KEY를 확인하세요'"),
    ("scripts/llm_anthropic.py", "'응답이 max_tokens에서 잘렸습니다. yt-coach.json의 llm.max_tokens를 늘리세요'"),
    ("scripts/mission.py", "f'{day} 미션이 없습니다. 먼저 /yt-mission today'"),
    ("scripts/mission.py", "'profile.md가 없습니다. 먼저 /yt-init'"),
    ("scripts/polish.py", "'이 기획안의 콘티가 이미 있습니다. 제목·훅 대본이 바뀌었으니 /yt-coach:yt-conti 로 콘티를 다시 만들어 주세요 "
                          "(콘티의 대사는 자동으로 바뀌지 않습니다)'"),
    ("scripts/shots.py", "f'영상 md가 없습니다: {video_id} (먼저 /yt-add)'"),
    ("scripts/shots.py", "f'영상 md가 없습니다: {video_id} (먼저 /yt-add)'"),
    ("scripts/similarity.py", "f'유형화 결과가 없습니다. 먼저 /yt-typify {category}'"),
    ("scripts/similarity.py", "f'유형화 결과를 읽을 수 없습니다. 다시 /yt-typify {category}'"),
    ("scripts/similarity.py", "'ref 영상은 진단 대상이 아닙니다. 내 영상은 /yt-add <cat> mine <링크>로 넣으세요.'"),
    ("scripts/similarity.py", "'아키타입 구성 영상을 찾을 수 없습니다. /yt-typify를 다시 실행하세요.'"),
    ("scripts/stt.py", "'음성 인식 기능이 설치돼 있지 않습니다. 플러그인 폴더에서 `pip install faster-whisper`를 실행한 뒤 다시 시도하세요.'"),
    ("scripts/ui/api_add.py", "MSG_CLAUDE_STOP"),
    ("scripts/ui/api_draft.py", "f'작업 폴더 기준 상대 경로만 쓸 수 있습니다: {raw}'"),
])

# Strings in scripts/ and server/ that keep a local-only word: (file, word, string prefix) -> why the server never shows it.
CLI = "claude CLI engine: the server edition pins the anthropic-api engine (llm.pin)"
LIB = "shared library (git): feature library is off on the server edition"
MCP = "library MCP server for the plugin (stderr log), not part of the server edition"
LAUNCHER = "the `ytc ui` launcher (local CLI)"
ENV_NAME = "an environment variable name, not a message"
STD = "the local stdlib server only (server edition: server/asgi.py)"
PY_ALLOWED: dict[tuple[str, str, str], str] = {
    ("scripts/captions.py", "ytc ", "움직이는 자막을 만들려면"): "caption video setup: feature caption_video is off on the server",
    ("scripts/captions.py", "ytc ", "). Node를 올린 뒤"): "caption video setup: feature caption_video is off on the server",
    ("scripts/claude_cli.py", "터미널", "Claude에 로그인되어 있지 않습니다."): CLI,
    ("scripts/claude_cli.py", "터미널", "Claude Code CLI(claude)를 찾을 수 없습니다."): CLI,
    ("scripts/claude_cli.py", "Claude Code", "Claude Code CLI(claude)를"): CLI,
    ("scripts/common.py", "작업 폴더", "<작업 폴더>"): "PATH_MASK needs a work-folder root (none on the server)",
    ("scripts/common.py", "작업 폴더", "작업 폴더(root)가 없는 실행 문맥"): "RuntimeError for programmers, never a message",
    ("scripts/conti.py", "/yt-", "- 고치려면 위 프런트매터의"): "a line inside the saved conti md (file content, same bytes in both editions)",
    ("scripts/ctx.py", "작업 폴더", "작업 폴더(root)도 저장소 backend도"): "RuntimeError for programmers, never a message",
    ("scripts/cuts.py", "YT_PROXY", "YT_PROXY"): ENV_NAME,
    ("scripts/stt.py", "YT_PROXY", "YT_PROXY"): ENV_NAME,
    ("scripts/fetch.py", "YT_PROXY", "YT_PROXY"): ENV_NAME,
    ("scripts/fetch.py", "환경변수", "YouTube가 이 네트워크의 요청을 막았습니다."):
        "BLOCKED_MESSAGE is the local value; blocked_message() gives BLOCKED_MESSAGE_SERVER on the server",
    ("scripts/fetch.py", "YT_PROXY", "YouTube가 이 네트워크의 요청을 막았습니다."):
        "BLOCKED_MESSAGE is the local value; blocked_message() gives BLOCKED_MESSAGE_SERVER on the server",
    ("scripts/fetch.py", "YT_PROXY", "YT_PROXY에 지정한 프록시에"):
        "local proxy_failed text; _ytdlp_message() gives PROXY_FAILED_SERVER on the server",
    ("scripts/filestore.py", "작업 폴더", "작업 폴더 밖의"): "the local FileStore; the server uses pgstore/s3store/scratch",
    ("scripts/filestore.py", "작업 폴더", "작업 폴더 자체는"): "the local FileStore; the server uses pgstore/s3store/scratch",
    ("scripts/footage_out.py", "pip install", "PyAV가 설치돼 있지 않아"): "footage cut files: feature footage is off on the server",
    ("scripts/keys.py", "yt-coach.json", "yt-coach.json"): "the local config file key, not a message",
    ("scripts/library.py", "/yt-", "# 공용 라이브러리"): LIB,
    ("scripts/library.py", "/yt-", "저장소 주소가 올바르지 않습니다"): LIB,
    ("scripts/library.py", "작업 폴더", "작업 폴더의 drafts/"): LIB,
    ("scripts/library.py", "ytc ", "공용 라이브러리가 연결되지 않았습니다."): LIB,
    ("scripts/library_mcp.py", "플러그인", "pyyaml을 찾을 수 없습니다."): MCP,
    ("scripts/library_mcp.py", "pip install", "pyyaml을 찾을 수 없습니다."): MCP,
    ("scripts/library_mcp.py", "플러그인", "재실행한 Python에도"): MCP,
    ("scripts/library_mcp.py", "pip install", "재실행한 Python에도"): MCP,
    ("scripts/library_mcp.py", "작업 폴더", "시작: 작업 폴더"): MCP,
    ("scripts/ui/__main__.py", "UI 서버", "초 안에 UI 서버가"): LAUNCHER,
    ("scripts/ui/__main__.py", "UI 서버", "UI 서버가 바로 종료"): LAUNCHER,
    ("scripts/ui/__main__.py", "UI 서버", "UI 서버를 끄지 못했습니다"): LAUNCHER,
    ("scripts/ui/__main__.py", "ytc ", "ytc ui"): LAUNCHER,
    ("scripts/ui/server.py", "127.0.0.1", "127.0.0.1"): STD,
    ("scripts/ui/server.py", "localhost", "localhost:"): STD,
    ("scripts/ui/server.py", "127.0.0.1", "이 서버는 127.0.0.1 주소로만"): STD,
    ("scripts/ui/server.py", "터미널", "주소의 key가 맞지 않습니다."): "bad_token: " + STD,
    ("server/admin.py", "터미널", "비밀번호를 물을 터미널이 없습니다."): "the operator CLI (python -m server.admin) runs in a terminal",
    ("server/admin.py", "터미널", "비밀번호 로그인용 비밀번호."): "the operator CLI's --help text",
    ("server/admin.py", "작업 폴더", "작업 폴더가 없습니다:"): "the operator CLI imports a local work folder",
    ("server/scratch.py", "작업 폴더", "claude CLI 엔진(작업 폴더 cwd)"): "NotImplementedError for programmers, never shown",
}


def _ed_calls() -> list[tuple[str, str]]:
    found = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ed":
                found.append((path.relative_to(ROOT).as_posix(), ast.unparse(node.args[0])))
    return sorted(found)


# ---- one source of truth --------------------------------------------------------------------

def test_ed_picks_local_by_default_and_server_inside_the_server_context():
    assert edition.current() == "local" and ed("L", "S") == "L"
    with edition.use("server"):
        assert edition.is_server() and ed("L", "S") == "S"
    assert ed("L", "S") == "L"


def test_ui_edition_and_scripts_edition_are_the_same_context(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    with server.edition("server", ["library"]):
        assert edition.current() == "server" and edition.forced() == frozenset({"library"})
        assert server.current_edition() == "server"
    with edition.use("server"):
        assert server.current_edition() == "server"
    assert server.current_edition() == "local"


# ---- feature notices -------------------------------------------------------------------------

LOCAL_FEATURE_TEXT = {
    "library": "공용 라이브러리는 서버판에서 아직 쓸 수 없습니다",
    "caption_video": "자막 영상 만들기는 내 PC 판에서만 됩니다",
    "srtfit": "자막 시간 맞추기는 내 PC 판에서만 됩니다(영상 파일 필요)",
    "footage": "촬영 영상 처리는 내 PC 판에서 하고, 결과는 가져오기로 옮기세요",
}


def test_local_feature_text_is_unchanged(monkeypatch):
    monkeypatch.setenv(server.DISABLE_ENV, ",".join(server.FEATURE_PREFIXES))
    assert server.FEATURE_TEXT == LOCAL_FEATURE_TEXT
    assert server.disabled_features() == LOCAL_FEATURE_TEXT


def test_server_feature_text_says_not_on_this_server(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    with server.edition("server", list(server.FEATURE_PREFIXES)):
        off = server.disabled_features()
        assert off == server.FEATURE_TEXT_SERVER
        assert server.feature_guard("/api/footage")[1]["message"] == server.FEATURE_TEXT_SERVER["footage"]
    for name, text in server.FEATURE_TEXT_SERVER.items():
        assert "이 서버에서" in text and "PC" not in text and hits(text) == [], name


# ---- raise sites -----------------------------------------------------------------------------

def test_local_ed_arguments_are_the_old_messages():
    assert _ed_calls() == LOCAL_ED_GOLDEN


def test_ed_server_arguments_have_no_local_only_words():
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ed":
                text = "".join(c.value for c in ast.walk(node.args[1])
                               if isinstance(c, ast.Constant) and isinstance(c.value, str))
                assert hits(text) == [], (path.name, text)   # a Name (module constant) is covered by the module scan


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("server_edition", [False, True])
def test_messages_follow_the_edition(home, server_edition):
    with edition.use("server" if server_edition else "local"):
        with pytest.raises(ScriptError) as e:
            similarity._load_typify("food")
        typify = e.value.message
        with pytest.raises(ScriptError) as e:
            mission._require_mission("2026-10-31")
        no_mission = e.value.message
        blocked = fetch.blocked_message()
        stop = api_add._stop_message("claude_missing")
    if server_edition:
        assert typify == "유형화 결과가 없습니다. 먼저 유형 화면에서 유형화를 실행하세요"
        assert no_mission == "2026-10-31 미션이 없습니다. 먼저 오늘 미션을 받으세요"
        assert blocked == fetch.BLOCKED_MESSAGE_SERVER and "YT_PROXY" not in blocked
        assert stop == "AI를 지금 쓸 수 없어 중단"
    else:
        assert typify == "유형화 결과가 없습니다. 먼저 /yt-typify food"
        assert no_mission == "2026-10-31 미션이 없습니다. 먼저 /yt-mission today"
        assert blocked == fetch.BLOCKED_MESSAGE
        assert stop == api_add.MSG_CLAUDE_STOP == "Claude(LLM 엔진)를 쓸 수 없어 중단"


def test_anthropic_missing_key_message_by_edition(monkeypatch):
    from scripts import llm_anthropic

    monkeypatch.delenv(llm_anthropic.KEY_ENV, raising=False)
    with pytest.raises(ScriptError) as e:
        llm_anthropic._api_key()
    assert e.value.message == "ANTHROPIC_API_KEY 환경 변수가 없습니다. API 키를 환경 변수로 설정하세요"
    with edition.use("server"), pytest.raises(ScriptError) as e:
        llm_anthropic._api_key()
    assert e.value.code == "llm_not_configured" and e.value.message == "AI를 지금 쓸 수 없습니다. 관리자에게 문의하세요"


def test_stt_missing_message_by_edition(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_whisper(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_whisper)
    with pytest.raises(ScriptError) as e:
        stt._load_whisper()
    assert "pip install faster-whisper" in e.value.message
    with edition.use("server"), pytest.raises(ScriptError) as e:
        stt._load_whisper()
    assert e.value.message == "이 서버에는 음성 인식 기능이 없습니다."


def test_python_strings_visible_on_the_server_have_no_local_only_words():
    paths = [p for p in sorted((ROOT / "scripts").rglob("*.py"))]
    paths += [p for p in sorted((ROOT / "server").glob("*.py"))]
    found = scan(paths, py_strings, ROOT)
    assert [f for f in found if not allowed(f, PY_ALLOWED)] == []
    assert stale(found, PY_ALLOWED) == []                   # no stale allowlist entries


def test_py_string_scanner_skips_docstrings_and_local_ed_argument():
    src = '"""작업 폴더 doc"""\ndef f():\n    """터미널"""\n    return ed("작업 폴더 L", "서버 S") + "슬래시"\n'
    assert sorted(t for _, t in py_strings(src)) == sorted(["서버 S", "슬래시"])


# ---- help pages ------------------------------------------------------------------------------

SECTIONS = ["무엇을 하나요", "사용 예", "이렇게 나와요", "알아 둘 것", "다음 단계"]
ALL_PAGES = [*api_help.SCREEN_PAGES, *api_help.GUIDE_PAGES]


@pytest.mark.parametrize("page", ALL_PAGES)
def test_local_help_reads_the_shared_file(page):
    assert api_help.help_file(page) == api_help.HELP_DIR / f"{page}.md"
    body = api_help.read_help(page)["markdown"]
    assert body.strip() and body in (api_help.HELP_DIR / f"{page}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ALL_PAGES)
def test_server_help_has_no_local_only_words(page):
    with edition.use("server"):
        text = api_help.read_help(page)["markdown"]
    assert hits(text) == [], page
    for word in ("서버 끄기", "명령줄", "휴지통 폴더", "UI 서버"):
        assert word not in text, (page, word)


def test_server_help_variants_exist_only_for_shared_pages_and_keep_sections():
    names = sorted(p.stem for p in api_help.SERVER_HELP_DIR.glob("*.md"))
    assert set(names) <= {*ALL_PAGES, "_chatbot"}
    for name in names:
        text = (api_help.SERVER_HELP_DIR / f"{name}.md").read_text(encoding="utf-8")
        local = (api_help.HELP_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert text != local, name
        if name in api_help.SCREEN_PAGES:
            assert re.findall(r"^## (.+?)\s*$", text, re.M) == SECTIONS, name


def test_server_help_links_are_server_screens():
    for path in api_help.SERVER_HELP_DIR.glob("*.md"):
        assert "(#/library)" not in path.read_text(encoding="utf-8") or path.stem == "library", path.name


def test_chatbot_rules_follow_the_edition():
    assert api_help._rules() == [api_help.HELP_DIR / "_chatbot.md"]
    with edition.use("server"):
        rules = api_help._rules()
    assert rules == [api_help.SERVER_HELP_DIR / "_chatbot.md"]
    text = rules[0].read_text(encoding="utf-8")
    assert "서버판" in text and "이 서버에서 쓸 수 없는 것" in text and "서버 끄기" not in text
    assert hits(text) == []



# ---- fix round 1 -----------------------------------------------------------------------------

def test_server_chat_menu_drops_disabled_routes(monkeypatch):
    monkeypatch.delenv(server.DISABLE_ENV, raising=False)
    assert api_help.chat_menu() == api_help.MENU
    monkeypatch.setenv(server.DISABLE_ENV, "library")       # local edition (container): menu unchanged
    assert api_help.chat_menu() == api_help.MENU
    with server.edition("server", list(server.FEATURE_PREFIXES)):
        menu = api_help.chat_menu()
    assert [m["route"] for m in menu] == [m["route"] for m in api_help.MENU if m["route"] != "#/library"]


def test_server_scratch_messages():
    from server import scratch

    assert scratch.OUTSIDE_MESSAGE == "허용되지 않은 위치입니다"
    assert scratch.NOT_A_FILE_MESSAGE == "올바른 파일이 아닙니다"


def test_server_trash_failure_message_is_server_wording():
    from server import pgstore

    e = pgstore._trash_failed(OSError("boom"))
    assert e.code == "trash_failed" and e.message == "파일을 옮기지 못했습니다. 잠시 뒤 다시 시도해 주세요."


def test_server_help_trash_is_honest_and_home_has_no_caption_criterion():
    for name in ("refs", "draft", "conti"):
        text = (api_help.SERVER_HELP_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert "되살릴 수 없어요" in text and "관리자에게 문의하세요(지운" not in text, name
    home_md = (api_help.SERVER_HELP_DIR / "home.md").read_text(encoding="utf-8")
    assert "자막 영상은 자막 영상을 만들었을 때" not in home_md


def test_operations_doc_gives_the_admin_a_way_to_make_categories_and_profiles():
    text = (ROOT / "deploy" / "server" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "python -m scripts.refstore init" in text and "profile.md" in text and "admin import /import" in text
