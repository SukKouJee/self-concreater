"""저장소 이전(S3 단계 11) 특성 테스트: llm·claude_cli·llm_anthropic·library가 쓰고 읽는 바이트·프롬프트 문자열을 못 박는다.

이전 전 코드에서 통과하고, 이전 뒤에도 같은 값이어야 한다(파일 목록은 작업 폴더 기준 상대 경로, cwd는 작업 폴더).
"""
import json
import shutil
import subprocess
import urllib.request

import pytest

from scripts import claude_cli, library, llm
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import tree

PAYLOAD = {"k": "값", "n": [1, 2]}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.delenv("YTC_LLM_ENGINE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def _cli_ok(obj):
    return subprocess.CompletedProcess(
        ["claude"], 0, json.dumps({"is_error": False, "structured_output": obj, "result": ""}), "")


# ---- claude-cli 프롬프트 -------------------------------------------------------------------

def test_claude_cli_prompt_with_files_is_byte_exact_and_cwd_is_work_folder(home, monkeypatch):
    rule = home / "rule.md"
    rule.write_text("규칙본문", encoding="utf-8")
    calls = []
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or _cli_ok({"ok": 1}))
    files = [home / "workspace" / "frames" / "abc" / "0001.jpg", home / "refs" / "ref_a" / "videos" / "한글.md"]

    assert llm.ask([rule], PAYLOAD, "할일ABC", files=files) == {"ok": 1}

    cmd, kw = calls[0]
    body = json.dumps(PAYLOAD, ensure_ascii=False, indent=2)
    assert kw["input"] == (
        "규칙본문\n\n"
        f"## 입력\n```json\n{body}\n```\n\n"
        "## 읽을 파일\n규칙이 요구할 때만 Read 도구로 여세요.\n"
        "- workspace/frames/abc/0001.jpg\n- refs/ref_a/videos/한글.md\n\n"
        "## 할 일\n할일ABC\n\n출력은 JSON 하나만."
    )
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert kw["cwd"] == home


def test_claude_cli_prompt_without_files_is_byte_exact(home, monkeypatch):
    rule = home / "rule.md"
    rule.write_text("규칙본문", encoding="utf-8")
    calls = []
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or _cli_ok({"ok": 1}))

    llm.ask([rule], PAYLOAD, "할일ABC")

    body = json.dumps(PAYLOAD, ensure_ascii=False, indent=2)
    assert calls[0][1]["input"] == f"규칙본문\n\n## 입력\n```json\n{body}\n```\n\n## 할 일\n할일ABC\n\n출력은 JSON 하나만."


# ---- yt-coach.json 읽기 --------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf{}", b"\xff\xfe\x00", b"[]", b'{"llm": 3}'])
def test_llm_config_bad_bytes_are_llm_not_configured(home, raw):
    (home / "yt-coach.json").write_bytes(raw)
    with pytest.raises(ScriptError) as e:
        llm.config()
    assert e.value.code == "llm_not_configured"


def test_llm_config_reads_utf8_json(home):
    (home / "yt-coach.json").write_bytes('{"llm": {"engine": "anthropic-api", "model": " 모델 "}}'.encode("utf-8"))
    assert llm.config() == {"engine": "anthropic-api", "model": "모델"}


# ---- anthropic 첨부 -----------------------------------------------------------------------

class _Resp:
    def __init__(self, obj):
        self._raw = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_anthropic_attachments_keep_names_order_and_newlines(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TEST-NOT-A-REAL-KEY")
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    (home / "workspace" / "frames" / "v").mkdir(parents=True)
    (home / "workspace" / "frames" / "v" / "0001.JPG").write_bytes(b"\xff\xd8abc")
    (home / "refs" / "ref_a").mkdir(parents=True)
    (home / "refs" / "ref_a" / "ex.md").write_bytes("가\r\n나".encode("utf-8"))
    (home / "n.txt").write_bytes(b"txt")
    (home / "z.bin").write_bytes(b"ignored")
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp({"content": [{"type": "text", "text": '{"a": 1}'}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    files = [home / "workspace" / "frames" / "v" / "0001.JPG", home / "refs" / "ref_a" / "ex.md",
             home / "n.txt", home / "z.bin"]

    assert llm.ask([], {}, "t", files=files) == {"a": 1}

    content = seen["body"]["messages"][0]["content"][1:]
    assert content == [
        {"type": "text", "text": "[이미지: 0001.JPG]"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9hhYmM="}},
        {"type": "text", "text": "[파일: ex.md]\n가\n나"},
        {"type": "text", "text": "[파일: n.txt]\ntxt"},
    ]


def test_anthropic_missing_file_is_llm_failed_with_name(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TEST-NOT-A-REAL-KEY")
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("네트워크를 부르면 안 됩니다"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t", files=[home / "workspace" / "frames" / "x" / "0009.jpg"])
    assert e.value.code == "llm_failed" and "0009.jpg" in e.value.message and "FileNotFoundError" in e.value.message


# ---- library: 설정·미리보기·잠금·clone 위치 ----------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git이 필요합니다")


@needs_git
def test_library_files_land_where_they_did_with_same_bytes(tmp_path, monkeypatch):
    from tests.test_library import CAT, make_home
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)], check=True)
    home = make_home(tmp_path, monkeypatch, "h")

    out = library.connect(str(remote), "Alice")

    cfg_bytes = (home / "library.json").read_bytes()
    expected = {"remote": str(remote), "author": "Alice", "shared": [], "last_pull": json.loads(cfg_bytes)["last_pull"]}
    assert cfg_bytes == (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert b"\r" not in cfg_bytes and out["status"] == "connected"
    assert (home / "workspace" / "library" / ".git").exists()
    assert not (home / "workspace" / "library.lock").exists()

    pv = library.preview_technique(CAT, "훅", "결과 선공개")
    pfile = home / "workspace" / "tmp" / f"library_preview_{pv['preview_id']}.json"
    data = library.load_preview(pv["preview_id"])
    assert pfile.read_bytes() == json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    before = tree(home)
    assert library.discard_preview(pv["preview_id"]) == {"status": "discarded"}
    assert set(before) - set(tree(home)) == {pfile.relative_to(home).as_posix()}
    with pytest.raises(ScriptError) as e:
        library.load_preview(pv["preview_id"])
    assert e.value.code == "preview_not_found"


@needs_git
def test_library_config_errors_and_unconnected_status(tmp_path, monkeypatch):
    from tests.test_library import make_home
    home = make_home(tmp_path, monkeypatch, "h")
    assert library.status() == {"connected": False}
    (home / "library.json").write_bytes(b"{nope")
    with pytest.raises(ScriptError) as e:
        library.status()
    assert e.value.code == "library_config"
    (home / "library.json").write_bytes(b'{"remote": 1}')
    with pytest.raises(ScriptError) as e:
        library.status()
    assert e.value.code == "library_config"
    (home / "library.json").write_bytes(json.dumps({"remote": "r", "author": "a"}).encode())
    assert library.status() == {"connected": False}                         # clone이 없으면 연결 아님


@needs_git
def test_library_preview_template_rejects_dotdot_and_accepts_relative(tmp_path, monkeypatch):
    from tests.test_library import CAT, make_draft, make_home
    home = make_home(tmp_path, monkeypatch, "h")
    draft_path = make_draft(home)
    rel = draft_path.relative_to(home)
    (home / "mine").mkdir(exist_ok=True)
    (home / "mine" / "x.md").write_text("---\n---\n", encoding="utf-8")

    for bad in ("drafts/../mine/x.md", str(home / "drafts" / ".." / "mine" / "x.md"), "../outside.md"):
        with pytest.raises(ScriptError) as e:
            library.preview_template(bad)
        assert e.value.code == "invalid_draft_path", bad
    pv = library.preview_template(rel)                                         # 상대 경로는 작업 폴더 기준
    assert library.load_preview(pv["preview_id"])["source"] == rel.as_posix()
    assert pv["frontmatter"]["category"] == CAT


@needs_git
def test_library_edits_file_relative_to_work_folder(tmp_path, monkeypatch):
    from tests.test_library import make_home
    home = make_home(tmp_path, monkeypatch, "h")
    (home / "e.json").write_text('{"summary": "요약"}', encoding="utf-8")
    assert library._load_edits("@e.json") == {"summary": "요약"}
    assert library._load_edits("@" + str(home / "e.json")) == {"summary": "요약"}
    with pytest.raises(ScriptError) as e:
        library._load_edits("@missing.json")
    assert e.value.code == "invalid_edits"


def test_ask_rejects_files_outside_work_folder_before_any_engine_call(home, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: pytest.fail("엔진을 부르면 안 됩니다"))
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **k: pytest.fail("엔진을 부르면 안 됩니다"))
    outside = tmp_path_factory.mktemp("outside") / "a.jpg"
    for bad in (outside, "../a.jpg", "a" + chr(92) + "b.jpg"):
        with pytest.raises(ScriptError) as e:
            llm.ask([], {}, "t", files=[bad])
        assert e.value.code == "bad_path", bad
