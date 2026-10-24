"""플러그인 뼈대(매니페스트·커맨드·템플릿)가 설계서 v2와 맞는지 확인한다."""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    "yt-init", "yt-add", "yt-typify", "yt-inspire",
    "yt-draft", "yt-polish", "yt-conti", "yt-diagnose", "yt-mission", "yt-review", "yt-ui", "yt-library",
]


def split_frontmatter(text):
    assert text.startswith("---\n"), "프런트매터로 시작해야 한다"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_plugin_manifest():
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text("utf-8"))
    assert manifest["name"] == "yt-coach"
    assert manifest["version"] == "0.1.0"


def test_marketplace_lists_plugin_at_repo_root():
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text("utf-8"))
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("yt-coach", "./")]


@pytest.mark.parametrize("name", COMMANDS)
def test_command_has_frontmatter(name):
    fm, body = split_frontmatter((ROOT / "commands" / f"{name}.md").read_text("utf-8"))
    assert fm["description"]
    assert "argument-hint" in fm
    assert body.strip()


def test_no_extra_commands():
    assert sorted(p.stem for p in (ROOT / "commands").glob("*.md")) == sorted(COMMANDS)


def test_video_template_frontmatter_matches_schema():
    fm, body = split_frontmatter((ROOT / "templates/video.md").read_text("utf-8"))
    for key in [
        "id", "url", "role", "channel", "title", "duration_sec", "format",
        "transcript_source", "sections", "hook_type", "narrative_type",
        "title_pattern", "pacing", "shots", "camera", "reviewed", "schema_version",
    ]:
        assert key in fm, key
    assert fm["schema_version"] == 1
    for heading in ["## 한 줄 요약", "## 시놉시스", "## 섹션", "## 눈에 띄는 기법", "## 내 메모"]:
        assert heading in body


@pytest.mark.parametrize("name, headings", [
    ("index.md", ["## 개요", "## 단계", "## 판정", "## 아키타입", "## 모티브 카드", "## 이상치"]),
    ("profile.md", ["## 관심 주제", "## 포맷", "## 롤모델", "## 하루 시간",
                    "## 피하고 싶은 스타일", "## 강점", "## 약점", "## 성장 단계"]),
    ("techniques.md", ["## 훅", "## 전환", "## 엔딩", "## 촬영"]),
    ("mission.md", ["## 미션", "## 제출", "## 피드백"]),
])
def test_markdown_template_headings(name, headings):
    text = (ROOT / "templates" / name).read_text("utf-8")
    for heading in headings:
        assert heading in text, heading


def test_thresholds_are_ordered():
    t = json.loads((ROOT / "templates/thresholds.json").read_text("utf-8"))
    assert t["stages"] == {"preliminary": 5, "formal": 10}
    assert t["typable"]["silhouette"] > t["weak"]["silhouette"]
    assert t["typable"]["max_cluster_share"] >= t["weak"]["max_cluster_share"]
