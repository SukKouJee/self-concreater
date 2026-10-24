"""relabel: 사용자가 고친 섹션 라벨·유형을 프런트매터에만 저장한다."""
import hashlib
import json

import pytest

from scripts import refstore, videomd
from scripts.common import ScriptError
from tests.test_conti import home  # noqa: F401 - fixture
from tests.typify_fixtures import CAT


def vpath(home, vid="a00"):  # noqa: F811
    return home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def body_of(path):
    return path.read_bytes().split(b"---", 2)[2]


def test_relabel_updates_frontmatter_only_and_marks_reviewed(home):  # noqa: F811
    path = vpath(home)
    before_body = body_of(path)
    h0 = sha(path)
    changes = {"sections": [{"label": "hook"}, {"label": "body", "sub": "list_item"}, {"label": "outro"}],
               "hook_type": "question"}
    r = videomd.relabel("a00", CAT, changes, h0)
    fm, _ = refstore.read_md(path)
    assert [s["label"] for s in fm["sections"]] == ["hook", "body", "outro"]
    assert fm["sections"][1]["sub"] == "list_item"
    assert fm["sections"][2]["start"] == 52.0
    assert fm["hook_type"] == "question" and fm["reviewed"] is True
    assert body_of(path) == before_body
    assert set(r["changed"]) == {"sections", "hook_type"} and r["hash"] == sha(path)
    assert r["id"] == "a00" and r["reviewed"] is True and r["md"].endswith("a00.md")


def test_relabel_rejects_stale_hash_and_leaves_file(home):  # noqa: F811
    path = vpath(home)
    before = path.read_bytes()
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, {"hook_type": "question"}, "0" * 64)
    assert e.value.code == "ref_changed"
    assert path.read_bytes() == before


GOOD = [{"label": "hook"}, {"label": "body", "sub": "story"}, {"label": "cta"}]


def _with(i, **kw):
    s = [dict(x) for x in GOOD]
    s[i].update(kw)
    return {"sections": s}


@pytest.mark.parametrize("changes", [
    _with(0, label="nope"),
    _with(0, sub="story"),
    _with(1, sub="nope"),
    {"hook_type": "x"},
    {"narrative_type": "x"},
    {"sections": GOOD[:2]},
    {"sections": "hook"},
    {"sections": [{"label": "hook"}, "body", {"label": "cta"}]},
])
def test_relabel_rejects_bad_values(home, changes):  # noqa: F811
    path = vpath(home)
    before = path.read_bytes()
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, changes, sha(path))
    assert e.value.code == "invalid_labels"
    assert path.read_bytes() == before


def test_relabel_drops_sub_when_leaving_body(home):  # noqa: F811
    path = vpath(home)
    videomd.relabel("a00", CAT, {"sections": [{"label": "hook"}, {"label": "cta"}, {"label": "cta"}]}, sha(path))
    fm, _ = refstore.read_md(path)
    assert "sub" not in fm["sections"][1]


def test_relabel_keeps_crlf_and_memo_and_drops_bom_on_bom_input(home):  # noqa: F811
    path = vpath(home)
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n").rstrip("\n")
    text = text + "\n내용\n\n## 내 메모\n내 생각\n"
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    before_body = body_of(path)
    videomd.relabel("a00", CAT, {"hook_type": "question"}, sha(path))
    raw = path.read_bytes()
    assert body_of(path) == before_body
    head = raw.split(b"---", 2)[1]
    assert b"\n" not in head.replace(b"\r\n", b"")
    assert "## 내 메모\r\n내 생각\r\n".encode() in raw
    assert refstore.read_md(path)[0]["hook_type"] == "question"
    assert not raw.startswith("﻿".encode())   # current behavior: BOM is not preserved


def test_relabel_noop_does_not_write(home):  # noqa: F811
    path = vpath(home)
    mtime, h0 = path.stat().st_mtime_ns, sha(path)
    same = {"hook_type": "result_first",
            "sections": [{"label": "hook"}, {"label": "body", "sub": "story"}, {"label": "cta"}]}
    r = videomd.relabel("a00", CAT, same, h0)
    assert r["changed"] == [] and r["hash"] == h0 and r["reviewed"] is False
    assert path.stat().st_mtime_ns == mtime
    assert refstore.read_md(path)[0]["reviewed"] is False


def _write_cuts(home, text):  # noqa: F811
    p = home / "workspace" / "cuts" / "a00.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


OUTRO = {"sections": [{"label": "hook"}, {"label": "body", "sub": "story"}, {"label": "outro"}]}


def test_relabel_recomputes_pacing_when_cuts_exist(home):  # noqa: F811
    _write_cuts(home, json.dumps({"cuts": [10, 20, 55], "avg_cut_sec": 15}))
    path = vpath(home)
    videomd.relabel("a00", CAT, OUTRO, sha(path))
    by = refstore.read_md(path)[0]["pacing"]["by_section"]
    assert "outro" in by and "cta" not in by


def test_relabel_keeps_pacing_without_cuts(home):  # noqa: F811
    path = vpath(home)
    old = refstore.read_md(path)[0]["pacing"]
    videomd.relabel("a00", CAT, OUTRO, sha(path))
    assert refstore.read_md(path)[0]["pacing"] == old


def test_relabel_keeps_pacing_with_broken_cuts(home):  # noqa: F811
    _write_cuts(home, "{broken")
    path = vpath(home)
    old = refstore.read_md(path)[0]["pacing"]
    videomd.relabel("a00", CAT, OUTRO, sha(path))
    assert refstore.read_md(path)[0]["pacing"] == old


def test_relabel_missing_video_and_bad_id(home):  # noqa: F811
    with pytest.raises(ScriptError) as e:
        videomd.relabel("zzz", CAT, {}, "0" * 64)
    assert e.value.code == "video_not_found"
    with pytest.raises(ScriptError) as e:
        videomd.relabel("../a00", CAT, {}, "0" * 64)
    assert e.value.code == "bad_request"


def test_cli_relabel(home, tmp_path):  # noqa: F811
    f = tmp_path / "changes.json"
    f.write_text(json.dumps({"narrative_type": "list"}), encoding="utf-8")
    result = videomd.main(["relabel", "a00", "--category", CAT, "--changes", str(f), "--hash", sha(vpath(home))])
    assert result["changed"] == ["narrative_type"]


def test_relabel_bad_md_when_section_time_not_numeric_and_cuts_exist(home):  # noqa: F811
    path = vpath(home)
    fm, _ = refstore.read_md(path)
    sections = [dict(s) for s in fm["sections"]]
    sections[1]["start"] = "x"
    refstore.update_frontmatter(path, {"sections": sections})
    _write_cuts(home, json.dumps({"cuts": [10, 20, 55], "avg_cut_sec": 15}))
    before = path.read_bytes()
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, OUTRO, sha(path))
    assert e.value.code == "bad_md"
    assert path.read_bytes() == before


def test_relabel_bad_md_when_cuts_items_not_numeric(home):  # noqa: F811
    _write_cuts(home, json.dumps({"cuts": ["a", None], "avg_cut_sec": 15}))
    path = vpath(home)
    before = path.read_bytes()
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, OUTRO, sha(path))
    assert e.value.code == "bad_md"
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["notalist", [1, 2, 3], [{"start": 0}, {"label": "body"}, {"label": "cta"}]])
def test_relabel_bad_md_when_stored_sections_malformed(home, bad):  # noqa: F811
    path = vpath(home)
    refstore.update_frontmatter(path, {"sections": bad})
    before = path.read_bytes()
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, {"sections": GOOD}, sha(path))
    assert e.value.code == "bad_md"
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    _with(0, label=None),
    {"hook_type": None},
    {"narrative_type": None},
])
def test_relabel_rejects_none_values(home, changes):  # noqa: F811
    path = vpath(home)
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, changes, sha(path))
    assert e.value.code == "invalid_labels"


def test_relabel_ignores_extra_keys_in_section_items(home):  # noqa: F811
    path = vpath(home)
    videomd.relabel("a00", CAT, _with(2, label="outro", start=999), sha(path))
    fm, _ = refstore.read_md(path)
    assert fm["sections"][2]["label"] == "outro" and fm["sections"][2]["start"] == 52.0


def test_relabel_omitted_sub_clears_existing_sub(home):  # noqa: F811
    path = vpath(home)
    videomd.relabel("a00", CAT, {"sections": [{"label": "hook"}, {"label": "body"}, {"label": "cta"}]}, sha(path))
    assert "sub" not in refstore.read_md(path)[0]["sections"][1]


def test_frame_pool_exact_section_follows_relabel(home):  # noqa: F811
    from scripts import conti
    from tests.test_conti import make_frames
    frames = make_frames(home, "a00")
    shots = home / "workspace" / "shots" / "a00.json"
    shots.parent.mkdir(parents=True, exist_ok=True)
    shots.write_text(json.dumps({"labels": [{"frame": frames[3], "size": "탑뷰", "section": "cta"}]}), encoding="utf-8")
    assert conti.frame_pool(CAT)[0]["section"] == "cta"
    videomd.relabel("a00", CAT, OUTRO, sha(vpath(home)))
    assert conti.frame_pool(CAT)[0]["section"] == "outro"


def test_relabel_reports_pacing_stale_without_cuts(home):  # noqa: F811
    r = videomd.relabel("a00", CAT, OUTRO, sha(vpath(home)))
    assert r["pacing_stale"] is True


def test_relabel_reports_pacing_stale_with_broken_cuts(home):  # noqa: F811
    _write_cuts(home, "{broken")
    assert videomd.relabel("a00", CAT, OUTRO, sha(vpath(home)))["pacing_stale"] is True


def test_relabel_pacing_not_stale_with_cuts_or_without_section_change(home):  # noqa: F811
    assert videomd.relabel("a00", CAT, {"hook_type": "question"}, sha(vpath(home)))["pacing_stale"] is False
    _write_cuts(home, json.dumps({"cuts": [10, 20, 55], "avg_cut_sec": 15}))
    assert videomd.relabel("a00", CAT, OUTRO, sha(vpath(home)))["pacing_stale"] is False


def test_relabel_invalid_label_message_includes_time_range(home):  # noqa: F811
    with pytest.raises(ScriptError) as e:
        videomd.relabel("a00", CAT, _with(1, label="nope"), sha(vpath(home)))
    assert "0:03–0:52" in e.value.message
