"""conti·conti_refs·conti_art·draft.trash의 파일 바이트와 반환 JSON을 고정한다(S3 Task 7 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같아야 한다. 골든은 옛 코드로 한 번 만든 값이다
(`GOLDEN_WRITE=1`로 다시 만들 수 있다. 평소에는 비교만 한다).
- HTML은 `write_text` 기본 줄바꿈(Windows CRLF)으로 쓰인다: LF로 바꾼 바이트를 골든과 비교하고, 원래 바이트가
  `os.linesep` 변환과 같은지 따로 본다.
- SRT는 운영체제와 상관없이 CRLF(수동 치환): 바이트 그대로 비교한다.
- 컷 id(무작위)와 오늘 날짜는 고정한다. 이력 파일은 시각(현지 시간대)이 들어가 골든 트리에서 뺀다.
"""
import base64
import datetime
import hashlib
import itertools
import json
import os
import re
from pathlib import Path

import pytest

from scripts import conti, conti_art, conti_history, conti_refs, draft, llm
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import changes, tree
from tests.typify_fixtures import CAT
from tests.test_conti import conti_file, draft_md, home, make_frames, saved  # noqa: F401 - fixtures

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "conti"
NOW = 1_790_000_000.0
HISTORY = ("workspace/conti_history/",)
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def golden(name: str, value, root: Path | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    if root is not None:
        text = text.replace(json.dumps(str(root))[1:-1], "<ROOT>")
    path = GOLDEN / f"{name}.json"
    if os.environ.get("GOLDEN_WRITE") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    assert text == path.read_bytes().decode("utf-8")


def jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def text_file(raw: bytes) -> str:
    """`write_text` 기본 줄바꿈으로 쓴 파일: 줄바꿈이 모두 os.linesep인지 보고 LF로 바꾼 바이트의 sha를 돌려준다."""
    lf = raw.replace(b"\r\n", b"\n")
    assert raw == lf.replace(b"\n", os.linesep.encode())
    return sha(lf)


class _Today:
    @staticmethod
    def today() -> datetime.date:
        return datetime.date(2026, 10, 31)


@pytest.fixture(autouse=True)
def stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """골든에 날짜·무작위 컷 id가 섞이지 않게 고정한다."""
    counter = itertools.count(1)
    monkeypatch.setattr(conti, "new_cut_id", lambda: f"id{next(counter):06d}")
    monkeypatch.setattr(conti, "date", _Today)
    monkeypatch.setattr(draft, "date", _Today)


def rel(home: Path, path: Path) -> str:
    return path.relative_to(home).as_posix()


def shots(home: Path) -> list[str]:
    """설명 있는 라벨 2개(탑뷰·클로즈업)를 가진 ref 프레임."""
    frames = make_frames(home, "a00")
    labels = [{"frame": frames[0], "size": "탑뷰", "angle": "탑뷰", "section": "hook", "desc": "컵 네 개"},
              {"frame": frames[1], "size": "클로즈업", "angle": "45도", "section": "body", "desc": "뚜껑"}]
    path = home / "workspace" / "shots" / "a00.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "a00", "labels": labels}, ensure_ascii=False), encoding="utf-8")
    return frames


def snapshot(home: Path) -> dict[str, str]:
    return tree(home, skip=HISTORY)


# --- save · context ---------------------------------------------------------------

def test_save_json_md_and_html_bytes(draft_md, home):
    shots(home)
    before = snapshot(home)
    out = conti.save(draft_md, conti_file(home), now=NOW)
    golden("save", jsonable(out), home)
    assert out["abs_html"] == str(home / out["html"])
    md, html = home / out["path"], home / out["html"]
    golden("save_bytes", {"md": sha(md.read_bytes()), "html_lf": text_file(html.read_bytes())})
    golden("save_tree", changes(before, snapshot(home)))
    assert [v["kind"] for v in conti_history.list_versions(md)] == ["created"]


def test_save_force_keeps_memo(draft_md, home):
    first = conti.save(draft_md, conti_file(home), now=NOW)
    md = home / first["path"]
    md.write_bytes(md.read_bytes() + "메모 한 줄 🍜\n".encode("utf-8"))
    with pytest.raises(ScriptError) as e:
        conti.save(draft_md, conti_file(home))
    assert (e.value.code, e.value.message) == (
        "conti_exists", f"이미 콘티가 있습니다: {first['path']} (다시 만들려면 --force, 내 메모는 보존)")
    out = conti.main(["save", rel(home, draft_md), "--conti", rel(home, conti_file(home)), "--force"])
    golden("save_force", jsonable(out), home)
    golden("save_force_bytes", {"md": sha(md.read_bytes())})
    assert "메모 한 줄 🍜" in md.read_text("utf-8")
    assert not (home / "workspace" / "tmp" / "conti.json").exists()


def test_context_and_missing_draft(draft_md, home):
    golden("context", jsonable(conti.context(rel(home, draft_md))))
    golden("context_abs", jsonable(conti.context(draft_md)))
    with pytest.raises(ScriptError) as e:
        conti.context("drafts/none.md")
    assert (e.value.code, e.value.message) == ("draft_not_found", "기획안이 없습니다: drafts/none.md (먼저 /yt-draft)")
    (home / "drafts" / "nosec.md").write_bytes(b"---\ntitle: x\n---\n# x\n")
    with pytest.raises(ScriptError) as e:
        conti.parse_draft(home / "drafts" / "nosec.md")
    assert (e.value.code, e.value.message) == ("invalid_draft", "기획안 프런트매터에 sections가 없습니다: drafts/nosec.md")


# --- render · html ---------------------------------------------------------------

def test_render_and_html_cli(saved, home):
    shots(home)
    key = rel(home, saved)
    out = conti.render(key)
    golden("render", jsonable(out), home)
    html = saved.with_suffix(".html")
    linked = text_file(html.read_bytes())
    one = conti.main(["html", key])
    assert one == {"status": "built", "path": rel(home, html), "bytes": html.stat().st_size}
    assert text_file(html.read_bytes()) == linked
    embedded = conti.main(["html", key, "--embed"])
    golden("html_bytes", {"linked_lf": linked, "embedded_lf": text_file(html.read_bytes()),
                          "linked_bytes": one["bytes"], "embedded_bytes": embedded["bytes"]})
    (home / "drafts" / "a-conti.md").write_text("---\ncuts: nope\n---\n본문\n", encoding="utf-8")
    (home / "drafts" / "b-conti.md").write_bytes(b"---\n\xff\xfe\x80\n---\n")
    golden("html_all", jsonable(conti.main(["html", "--all"])), home)
    with pytest.raises(ScriptError) as e:
        conti.main(["html", "drafts/none-conti.md"])
    assert (e.value.code, e.value.message) == ("conti_not_found", "콘티가 없습니다: drafts/none-conti.md")
    with pytest.raises(ScriptError) as e:
        conti.rebuild_html(None)
    assert e.value.code == "bad_args"


def test_html_beside_conti_md_at_root(saved, home):
    """drafts 밖(작업 폴더 바로 아래) 콘티도 그 옆에 HTML을 쓰고, 사진 URL은 그 폴더 기준이다."""
    frames = shots(home)
    top = home / "top-conti.md"
    top.write_bytes(saved.read_bytes())
    conti.update_cut(top, 1, {"ref_frame": frames[0]})
    page = (home / "top-conti.html").read_text("utf-8")
    assert f'src="{frames[0]}"' in page
    assert conti.html_dir_of(top) == "." and conti.html_dir_of(saved) == "drafts"


# --- 컷 편집 ----------------------------------------------------------------------

def test_update_cut_crlf_bytes_and_json(saved, home):
    frames = shots(home)
    saved.write_bytes(saved.read_bytes().replace(b"\n", b"\r\n"))
    out = conti.update_cut(rel(home, saved), 2, {"line": "새 대사", "ref_frame": str(home / frames[1])}, now=NOW)
    golden("update", jsonable(out), home)
    raw = saved.read_bytes()
    assert raw.startswith(b"---\r\n") and b"\r\n## \xeb\x82\xb4 \xeb\xa9\x94\xeb\xaa\xa8" in raw
    golden("update_bytes", {"md": sha(raw), "html_lf": text_file(saved.with_suffix(".html").read_bytes())})
    assert out["hash"] == conti.content_hash(saved) == sha(raw)


def test_edit_ops_json_and_bytes(saved, home):
    key = rel(home, saved)
    h = conti.content_hash(key)
    outs = []
    for op in ({"op": "insert", "at": 1, "cut": {"action": "새 컷", "guide": "가이드"}},
               {"op": "duplicate", "n": 1}, {"op": "move", "n": 1, "to": 2}, {"op": "delete", "n": 3}):
        out = conti.edit_cuts(key, op, h, now=NOW)
        h = out["hash"]
        outs.append(out)
    golden("edit_ops", jsonable(outs), home)
    golden("edit_ops_bytes", {"md": sha(saved.read_bytes()), "html_lf": text_file(saved.with_suffix(".html").read_bytes())})
    with pytest.raises(ScriptError) as e:
        conti.edit_cuts(key, {"op": "delete", "n": 1}, "0" * 64)
    assert e.value.code == "conti_changed"
    with pytest.raises(ScriptError) as e:
        conti.edit_cuts("drafts/none-conti.md", {"op": "delete", "n": 1}, None)
    assert (e.value.code, e.value.message) == ("conti_not_found", "콘티가 없습니다: drafts/none-conti.md")


def test_scenes_cut_context_and_refs(saved, home):
    shots(home)
    key = rel(home, saved)
    golden("scenes", jsonable(conti.scenes(key, 2)))
    golden("cut_context", jsonable(conti.cut_context(key, 2)))
    golden("cut_refs", jsonable(conti.cut_refs(conti._load_conti(saved))))
    golden("frame_pool", jsonable(conti.frame_pool(CAT)))


# --- SRT --------------------------------------------------------------------------

def test_export_srt_crlf_bytes(saved, home):
    key = rel(home, saved)
    cap = conti.export_srt(key)
    line = conti.main(["srt", key, "--track", "line"])
    golden("srt", {"caption": cap, "line": line,
                   "caption_text": (home / cap["path"]).read_bytes().decode("utf-8"),
                   "line_text": (home / line["path"]).read_bytes().decode("utf-8")})
    raw = (home / cap["path"]).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


# --- 그림 · 사진 고르기 ---------------------------------------------------------------

def test_save_art_bytes(saved, home):
    key = rel(home, saved)
    fm = conti._load_conti(saved)
    cut = fm["cuts"][0]
    svg = conti_art.sanitize_svg(SVG, conti_art.aspect(fm))
    out = conti_art.save_art(key, {cut["id"]: (svg, conti_art.basis(cut, conti_art.aspect(fm)))})
    golden("save_art", jsonable(out))
    after = conti._load_conti(saved)
    art = after["cuts"][0]["art"]
    golden("save_art_bytes", {"art": art, "svg": sha((home / art["file"]).read_bytes()), "md": sha(saved.read_bytes()),
                              "html_lf": text_file(saved.with_suffix(".html").read_bytes())})
    asp = conti_art.aspect(after)
    assert conti_art.art_state(after["cuts"][0], asp) == "fresh"
    assert conti_art.display_art(after["cuts"][0], asp) == {"file": art["file"], "state": "fresh"}
    assert conti_art.counts(after) == {"total": 4, "missing": 3, "stale": 0, "fresh": 1}
    (home / art["file"]).unlink()
    assert conti_art.art_state(after["cuts"][0], asp) is None


def test_draw_saves_through_worker_threads(saved, home, monkeypatch):
    def answer(rules, payload, task, validate, **kw):
        data = {"scenes": [{"id": s["id"], "svg": SVG} for s in payload["scenes"]]}
        return data, validate(data)

    monkeypatch.setattr(llm, "ask_validated", answer)
    out = conti_art.draw(rel(home, saved), None, False)
    golden("draw", jsonable(out))
    golden("draw_bytes", {"md": sha(saved.read_bytes()),
                          "art": sorted(p.relative_to(home).as_posix() for p in (home / "workspace" / "conti-art").rglob("*.svg"))})


def test_save_picks_json(saved, home):
    frames = shots(home)
    fm = conti._load_conti(saved)
    c1, c2 = fm["cuts"][0], fm["cuts"][1]
    basis = {c["id"]: {f: c.get(f) for f in conti.PICK_FIELDS} for c in (c1, c2)}
    out = conti_refs.save_picks(rel(home, saved), {c1["id"]: frames[0], c2["id"]: frames[1]}, basis, now=NOW)
    golden("save_picks", jsonable(out), home)
    golden("save_picks_bytes", {"md": sha(saved.read_bytes()), "html_lf": text_file(saved.with_suffix(".html").read_bytes())})


# --- 경로 문자열 · URL ------------------------------------------------------------------

def test_asset_url_and_data_uri(home):
    for name, data in (("workspace/frames/x/a b.jpg", b"\xff\xd8jpg"), ("workspace/conti-art/s/c.svg", b"<svg/>"),
                       ("workspace/frames/x/p.PNG", b"png")):
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_bytes(data)
    assert conti.asset_url("workspace/frames/x/a b.jpg") == "../workspace/frames/x/a%20b.jpg"
    assert conti.asset_url("workspace/frames/x/a b.jpg", "drafts/sub") == "../../workspace/frames/x/a%20b.jpg"
    assert conti.asset_url("workspace/frames/x/a b.jpg", "") == "workspace/frames/x/a%20b.jpg"
    assert conti.asset_url("workspace/conti-art/s/c.svg", embed=True) == (
        "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode())
    assert conti.asset_url("workspace/frames/x/p.PNG", embed=True) == "data:image/png;base64," + base64.b64encode(b"png").decode()
    assert conti.asset_url("workspace/frames/x/a b.jpg", embed=True).startswith("data:image/jpeg;base64,")
    assert conti.asset_url("workspace/frames/x/none.jpg") is None
    assert conti.asset_url("workspace/frames/x/none.jpg", embed=True) is None
    assert conti.asset_url("workspace/frames/x") is None                   # 폴더는 파일이 아니다


def test_rel_keeps_outside_paths_unchanged(home, tmp_path):
    outside = tmp_path.parent / "elsewhere" / "x-conti.md"
    assert conti._rel(outside) == outside.as_posix()
    assert conti._rel(home / "drafts" / "a-conti.md") == "drafts/a-conti.md"
    assert conti._rel(home / "drafts" / ".." / "drafts" / "a-conti.md") == "drafts/a-conti.md"


# --- 탈출 검사 회귀 ----------------------------------------------------------------------

def _symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except OSError as e:                       # Windows에서 개발자 모드·관리자 권한이 없으면 만들 수 없다
        pytest.skip(f"심볼릭 링크를 만들 수 없습니다: {e}")


@pytest.mark.parametrize("value, flag", [
    ("drafts/../profile.md", None), ("drafts/../../x/drafts/a.md", None), ("profile.md", None),
    ("drafts/a.txt", None), ("drafts", None), (".", None), ("drafts/.md", None),
    ("drafts/a-conti.md", False), ("drafts/a.md", True), ("drafts/a\x00.md", None),
])
def test_safe_draft_path_rejects(home, value, flag):
    with pytest.raises(ScriptError) as e:
        conti.safe_draft_path(value, conti_file=flag)
    assert e.value.code == "bad_path"
    golden_messages = {
        None: f"drafts 폴더 안의 .md 파일만 쓸 수 있습니다: {value}",
        False: f"기획안 파일이 아닙니다: {value}", True: f"콘티 파일이 아닙니다: {value}",
    }
    if "\x00" in value:
        assert e.value.message == f"잘못된 경로입니다: {value}"
    elif value in ("drafts/a-conti.md", "drafts/a.md"):
        assert e.value.message == golden_messages[flag]
    else:
        assert e.value.message == golden_messages[None]


def test_safe_draft_path_rejects_absolute_outside_and_empty(home, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-x" / "drafts" / "a.md"
    for value in (str(outside), outside.as_posix()):
        with pytest.raises(ScriptError) as e:
            conti.safe_draft_path(value)
        assert e.value.code == "bad_path"
    for value in ("", None, 3):
        with pytest.raises(ScriptError) as e:
            conti.safe_draft_path(value)
        assert (e.value.code, e.value.message) == ("bad_request", "path가 필요합니다")


def test_safe_draft_path_accepts_and_resolves(home):
    (home / "drafts" / "sub").mkdir(parents=True)
    got = conti.safe_draft_path("drafts/sub/../a-conti.md", conti_file=True)
    assert got == (home / "drafts" / "a-conti.md").resolve()
    assert conti.safe_draft_path(str(home / "drafts" / "b.MD"), conti_file=False) == (home / "drafts" / "b.MD").resolve()


def test_safe_draft_path_symlink_escape(home, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-out"
    (outside).mkdir()
    (outside / "secret.md").write_bytes(b"---\nsections: []\n---\n")
    _symlink(home / "drafts" / "link.md", outside / "secret.md")
    with pytest.raises(ScriptError) as e:
        conti.safe_draft_path("drafts/link.md")
    assert e.value.code == "bad_path"
    _symlink(home / "drafts" / "dir", outside, is_dir=True)
    with pytest.raises(ScriptError) as e:
        conti.safe_draft_path("drafts/dir/secret.md")
    assert e.value.code == "bad_path"


def test_ref_frame_escape_checks(saved, home, tmp_path):
    key = rel(home, saved)
    outside = tmp_path.parent / f"{tmp_path.name}-o" / "x.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"\xff\xd8")
    before = saved.read_bytes()
    for ref in (str(outside), "../" + tmp_path.name + "-o/x.jpg", "workspace/../../" + tmp_path.name + "-o/x.jpg"):
        with pytest.raises(ScriptError) as e:
            conti.update_cut(key, 1, {"ref_frame": ref})
        assert (e.value.code, e.value.message) == ("invalid_edit", f"작업 폴더 밖의 파일은 고정할 수 없습니다: {ref}")
    with pytest.raises(ScriptError) as e:
        conti.update_cut(key, 1, {"ref_frame": "workspace/frames/none.jpg"})
    assert (e.value.code, e.value.message) == ("invalid_edit", "참고 프레임 파일이 없습니다: workspace/frames/none.jpg")
    assert saved.read_bytes() == before
    inside = home / "workspace" / "frames" / "in.jpg"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"\xff\xd8")
    out = conti.update_cut(key, 1, {"ref_frame": "workspace/frames/../frames/in.jpg"})
    assert out["cut"]["ref_frame"] == "workspace/frames/in.jpg"
    _symlink(home / "workspace" / "frames" / "link.jpg", outside)
    with pytest.raises(ScriptError) as e:
        conti.update_cut(key, 1, {"ref_frame": "workspace/frames/link.jpg"})
    assert e.value.code == "invalid_edit"


# --- 휴지통 ----------------------------------------------------------------------------

STAMP = re.compile(r"workspace/trash/\d{8}-\d{6}(-\d+)?")


def outputs(home: Path, draft_md: Path, saved: Path) -> None:
    stem = draft_md.stem
    for suffix in ("-remaining.html", "-footage.csv", "-captions.mp4"):
        draft_md.with_name(stem + suffix).write_bytes(b"x")
    cuts_dir = draft_md.with_name(f"{stem}-cuts")
    cuts_dir.mkdir()
    (cuts_dir / "01_a.mp4").write_bytes(b"x")
    state = home / "workspace" / "footage_match" / f"{saved.stem}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")
    conti.export_srt(saved)
    conti.export_srt(saved, "line")


def trash_view(home: Path, result: dict, before: dict[str, str]) -> dict:
    after = tree(home)
    norm = lambda paths: sorted(STAMP.sub("workspace/trash/<STAMP>", p) for p in paths)  # noqa: E731
    diff = changes(before, after)
    return {"trash": STAMP.sub("workspace/trash/<STAMP>", result["trash"]), "moved": result["moved"],
            "added": norm(diff["added"]), "removed": norm(diff["removed"]), "modified": diff["modified"]}


def test_conti_trash_moves_outputs(saved, draft_md, home):
    outputs(home, draft_md, saved)
    before = tree(home)
    result = conti.main(["trash", rel(home, saved)])
    golden("trash_conti", trash_view(home, result, before))
    assert draft_md.is_file()
    with pytest.raises(ScriptError) as e:
        conti.trash(rel(home, saved))
    assert (e.value.code, e.value.message) == ("not_found", f"콘티 파일이 없습니다: {rel(home, saved)}")


def test_conti_trash_refusals(saved, draft_md, home):
    fake = home / "drafts" / "x-conti.md"
    fake.write_bytes(b"---\ntitle: x\n---\n")
    with pytest.raises(ScriptError) as e:
        conti.trash("drafts/x-conti.md")
    assert (e.value.code, e.value.message) == ("not_conti", "콘티 파일이 아닙니다(프런트매터에 script·cuts가 없음): drafts/x-conti.md")
    for value in (rel(home, draft_md), "profile.md", "drafts/../profile.md"):
        with pytest.raises(ScriptError) as e:
            conti.trash(value)
        assert e.value.code == "bad_path"


def test_draft_trash_moves_draft_and_conti(saved, draft_md, home):
    outputs(home, draft_md, saved)
    before = tree(home)
    result = draft.main(["trash", rel(home, draft_md)])
    golden("trash_draft", trash_view(home, result, before))
    with pytest.raises(ScriptError) as e:
        draft.trash(rel(home, draft_md))
    assert (e.value.code, e.value.message) == ("draft_not_found", f"기획안을 찾을 수 없습니다: {rel(home, draft_md)}")
    with pytest.raises(ScriptError) as e:
        draft.trash("drafts/../profile.md")
    assert e.value.code == "bad_path"


def test_draft_trash_without_conti(draft_md, home):
    before = tree(home)
    result = draft.trash(str(draft_md))
    view = trash_view(home, result, before)
    assert view["moved"] == [rel(home, draft_md)] and view["removed"] == [rel(home, draft_md)]
