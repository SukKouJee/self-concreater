import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import common

ROOT = Path(__file__).resolve().parents[1]


def run_module(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )


def test_selftest_prints_ok():
    result = run_module("scripts.common", "--selftest")
    assert result.returncode == 0
    assert json.loads(result.stdout.decode("utf-8")) == {"ok": True}


def test_emit_writes_utf8_json_without_escaping(capsys):
    common.emit({"title": "한글 제목", "n": 1})
    out = capsys.readouterr().out
    assert "한글 제목" in out
    assert json.loads(out) == {"title": "한글 제목", "n": 1}


def test_run_reports_script_error_as_json_with_nonzero_exit(capsys):
    def main(argv):
        raise common.ScriptError("invalid_url", "링크 형식이 아닙니다")

    with pytest.raises(SystemExit) as exc:
        common.run(main, [])
    assert exc.value.code != 0
    assert json.loads(capsys.readouterr().out) == {
        "error": "invalid_url",
        "message": "링크 형식이 아닙니다",
    }


def test_run_includes_error_details(capsys):
    def main(argv):
        raise common.ScriptError("no_transcript", "자막 없음", raw="workspace/raw/x.json")

    with pytest.raises(SystemExit):
        common.run(main, [])
    assert json.loads(capsys.readouterr().out) == {
        "error": "no_transcript", "message": "자막 없음", "raw": "workspace/raw/x.json",
    }


def test_run_reports_unexpected_exception_as_internal_error(capsys):
    def main(argv):
        raise RuntimeError("boom")

    with pytest.raises(SystemExit) as exc:
        common.run(main, [])
    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "internal"
    assert "boom" in payload["message"]


def test_run_emits_returned_value(capsys):
    common.run(lambda argv: {"ok": True, "argv": argv}, ["a"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "argv": ["a"]}


def test_argparser_accepts_video_ids_starting_with_dash():
    parser = common.ArgParser()
    parser.add_argument("video_id")
    parser.add_argument("--category")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(["-C8U1fizcms", "--category", "mukbang", "--force"])
    assert args.video_id == "-C8U1fizcms"
    assert args.category == "mukbang" and args.force is True


def test_argparser_keeps_real_options_shaped_like_ids():
    parser = common.ArgParser()
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("video_id")
    p.add_argument("--reanalyze", action="store_true")   # 11자: ID 모양과 같다
    args = parser.parse_args(["run", "-C8U1fizcms", "--reanalyze"])
    assert args.video_id == "-C8U1fizcms" and args.reanalyze is True


def test_argparser_still_rejects_unknown_options():
    parser = common.ArgParser()
    parser.add_argument("video_id")
    with pytest.raises(common.ScriptError) as exc:
        parser.parse_args(["abc", "--bogus"])
    assert exc.value.code == "usage"


def test_work_root_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert common.work_root() == tmp_path


def test_work_root_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("YT_COACH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert common.work_root() == tmp_path


def test_workspace_paths_live_under_work_root(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert common.workspace_dir("raw") == tmp_path / "workspace" / "raw"
    assert common.category_dir("cooking") == tmp_path / "refs" / "ref_cooking"


@pytest.mark.parametrize("bad", ["", "a b", "../x", "x/y", "한글"])
def test_category_name_is_validated(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    with pytest.raises(common.ScriptError) as exc:
        common.category_dir(bad)
    assert exc.value.code == "invalid_category"


def test_scrub_masks_credentials_and_absolute_paths(tmp_path, monkeypatch):
    from scripts.common import mask_credentials, scrub
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert mask_credentials("https://user:tok@github.com/x.git") == "https://***@github.com/x.git"
    out = scrub(f"실패 {tmp_path}\\workspace https://u:t@h.com/x.git C:\\Users\\kim\\a.md refs/heads/main")
    assert "tok" not in out and "u:t" not in out and str(tmp_path) not in out and "kim" not in out
    assert "<작업 폴더>" in out and "refs/heads/main" in out
