import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import refstore
from scripts.common import ScriptError

ROOT = Path(__file__).resolve().parents[1]

VIDEO = """---
id: abc123
role: ref
sections:
  - {label: hook, start: 0, end: 3}
hook_type: question
schema_version: 1
---
## 한 줄 요약
요약 문장

## 섹션
| 구간 | 라벨 | 내용 | 촬영 |
|---|---|---|---|
| 0-3 | hook | 질문 | 클로즈업 |

## 내 메모
내가 쓴 메모 — 절대 지우면 안 됨
"""


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def write_video(home, category, vid, role="ref"):
    path = home / "refs" / f"ref_{category}" / "videos" / f"{vid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VIDEO.replace("abc123", vid).replace("role: ref", f"role: {role}"),
                    encoding="utf-8", newline="")
    return path


# --- 프런트매터 ---------------------------------------------------------------

def test_split_and_join_roundtrip_keeps_body():
    fm, body = refstore.split_frontmatter(VIDEO)
    assert fm["id"] == "abc123"
    assert fm["sections"] == [{"label": "hook", "start": 0, "end": 3}]
    assert body.startswith("## 한 줄 요약")
    assert "내가 쓴 메모" in body


def test_split_without_frontmatter_raises():
    with pytest.raises(ScriptError) as exc:
        refstore.split_frontmatter("## 제목만\n")
    assert exc.value.code == "no_frontmatter"


def test_join_keeps_top_level_block_style_and_inner_flow_style():
    fm = {
        "id": "abc", "schema_version": 1, "shots": None,
        "sections": [{"label": "hook", "start": 0, "end": 3}],
        "title_pattern": ["number", "bracket"],
        "camera": {"static": 0.7, "pan": 0.3},
    }
    head = refstore.join_frontmatter(fm, "").split("---\n")[1]
    assert head.splitlines() == [
        "id: abc",
        "schema_version: 1",
        "shots: null",
        "sections:",
        "- {label: hook, start: 0, end: 3}",
        "title_pattern: [number, bracket]",
        "camera: {static: 0.7, pan: 0.3}",
    ]


def test_update_frontmatter_preserves_body_byte_for_byte(tmp_path):
    path = tmp_path / "v.md"
    path.write_text(VIDEO, encoding="utf-8", newline="")
    _, body_before = refstore.split_frontmatter(VIDEO)

    refstore.update_frontmatter(path, {"hook_type": "result_first", "shots": None})

    fm, body_after = refstore.read_md(path)
    assert fm["hook_type"] == "result_first"
    assert fm["shots"] is None
    assert fm["id"] == "abc123"          # 건드리지 않은 키 유지
    assert list(fm)[0] == "id"           # 키 순서 유지
    assert body_after == body_before


def test_update_frontmatter_keeps_crlf_body(tmp_path):
    path = tmp_path / "v.md"
    crlf = VIDEO.replace("\n", "\r\n")
    path.write_bytes(crlf.encode("utf-8"))

    refstore.update_frontmatter(path, {"hook_type": "situation"})

    raw = path.read_bytes().decode("utf-8")
    assert "내가 쓴 메모 — 절대 지우면 안 됨\r\n" in raw
    assert "\n" not in raw.replace("\r\n", "")  # 줄바꿈 섞이지 않음
    assert refstore.read_md(path)[0]["hook_type"] == "situation"


# --- 섹션 교체 ----------------------------------------------------------------

def test_replace_section_only_touches_target():
    body = "## 개요\n개요 본문\n\n## 단계\n옛 내용\n\n## 판정\n판정 본문\n"
    out = refstore.replace_section(body, "## 단계", "새 내용")
    assert out == "## 개요\n개요 본문\n\n## 단계\n새 내용\n\n## 판정\n판정 본문\n"


def test_replace_section_last_section():
    body = "## 개요\n개요\n\n## 단계\n옛 내용\n"
    assert refstore.replace_section(body, "## 단계", "새") == "## 개요\n개요\n\n## 단계\n새\n"


def test_replace_section_missing_heading_raises():
    with pytest.raises(ScriptError) as exc:
        refstore.replace_section("## 개요\n", "## 단계", "x")
    assert exc.value.code == "section_not_found"


def test_replace_section_refuses_user_memo():
    with pytest.raises(ScriptError) as exc:
        refstore.replace_section(VIDEO, "## 내 메모", "덮어쓰기")
    assert exc.value.code == "protected_section"


# --- 단계 ---------------------------------------------------------------------

@pytest.mark.parametrize("n, stage", [(0, 1), (4, 1), (5, 2), (9, 2), (10, 3), (30, 3)])
def test_stage_for(n, stage):
    assert refstore.stage_for(n) == stage


# --- 작업 폴더 / 카테고리 ------------------------------------------------------

def test_scaffold_creates_folders_and_gitignore(home):
    out = refstore.scaffold()
    for d in ["refs", "mine", "drafts", "missions", "reviews", "workspace"]:
        assert (home / d).is_dir()
    assert "workspace/" in (home / ".gitignore").read_text("utf-8").splitlines()
    assert out["profile_exists"] is False


def test_scaffold_is_idempotent_and_keeps_existing_gitignore(home):
    (home / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    refstore.scaffold()
    refstore.scaffold()
    lines = (home / ".gitignore").read_text("utf-8").splitlines()
    assert lines.count("workspace/") == 1
    assert "secret.txt" in lines


def test_init_category_creates_index_and_techniques(home):
    out = refstore.init_category("cooking")
    cat = home / "refs" / "ref_cooking"
    assert (cat / "videos").is_dir()
    fm, body = refstore.read_md(cat / "index.md")
    assert fm["category"] == "cooking"
    assert fm["created"]
    assert "# cooking 레퍼런스" in body
    assert "현재 단계: 1" in body
    assert refstore.read_md(cat / "techniques.md")[0]["category"] == "cooking"
    assert sorted(out["created"]) == ["refs/ref_cooking/index.md", "refs/ref_cooking/techniques.md"]


def test_init_category_never_overwrites(home):
    refstore.init_category("cooking")
    index = home / "refs" / "ref_cooking" / "index.md"
    index.write_text(index.read_text("utf-8") + "\n사용자 추가 내용\n", encoding="utf-8")
    out = refstore.init_category("cooking")
    assert "사용자 추가 내용" in index.read_text("utf-8")
    assert out["created"] == []
    assert len(out["skipped"]) == 2


def test_count_empty_category(home):
    refstore.init_category("cooking")
    assert refstore.count("cooking") == {"ref": 0, "mine": 0, "stage": 1}


def test_count_by_role(home):
    refstore.init_category("cooking")
    for i in range(5):
        write_video(home, "cooking", f"r{i}")
    write_video(home, "cooking", "m0", role="mine")
    assert refstore.count("cooking") == {"ref": 5, "mine": 1, "stage": 2}


def test_count_unknown_category(home):
    with pytest.raises(ScriptError) as exc:
        refstore.count("nope")
    assert exc.value.code == "category_not_found"


def test_list_categories(home):
    refstore.init_category("cooking")
    refstore.init_category("vlog")
    write_video(home, "vlog", "v1")
    assert refstore.list_categories() == [
        {"category": "cooking", "ref": 0, "mine": 0, "stage": 1},
        {"category": "vlog", "ref": 1, "mine": 0, "stage": 1},
    ]


def test_update_stage_rewrites_only_stage_section(home):
    refstore.init_category("cooking")
    index = home / "refs" / "ref_cooking" / "index.md"
    fm, body = refstore.read_md(index)
    index.write_text(index.read_text("utf-8").replace("## 개요\n", "## 개요\n내가 쓴 개요\n"),
                     encoding="utf-8")
    for i in range(7):
        write_video(home, "cooking", f"r{i}")

    out = refstore.update_stage("cooking")

    _, body = refstore.read_md(index)
    assert out["stage"] == 2
    assert "현재 단계: 2" in body
    assert "ref 3개 더" in body
    assert "내가 쓴 개요" in body
    assert body.count("## 단계") == 1


# --- CLI ----------------------------------------------------------------------

def cli(home, *args):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.refstore", *args],
        cwd=ROOT, capture_output=True, env={**os.environ, "YT_COACH_HOME": str(home)},
    )
    return result.returncode, json.loads(result.stdout.decode("utf-8"))


def test_cli_init_then_count(home):
    code, _ = cli(home, "init", "cooking")
    assert code == 0
    assert cli(home, "count", "cooking") == (0, {"ref": 0, "mine": 0, "stage": 1})


def test_cli_bad_usage_is_json_error(home):
    code, payload = cli(home, "unknown-subcommand")
    assert code != 0
    assert payload["error"] == "usage"


def test_cli_invalid_category(home):
    code, payload = cli(home, "count", "../etc")
    assert code != 0
    assert payload["error"] == "invalid_category"
