"""도움말 원본 md(docs/help) 형식 검사."""
import re
from pathlib import Path

HELP_DIR = Path(__file__).resolve().parent.parent / "docs" / "help"
SCREEN_PAGES = ["home", "add", "refs", "library", "typify", "report", "motif",
                "draft", "conti", "diagnose", "mission", "guide"]
SECTIONS = ["무엇을 하나요", "사용 예", "이렇게 나와요", "알아 둘 것", "다음 단계"]
LINK_RE = re.compile(r"\(#/([a-z]+)")


def _read(name):
    return (HELP_DIR / f"{name}.md").read_text(encoding="utf-8")


def _title(text):
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    assert m, "프런트매터 없음"
    t = re.search(r"^title:\s*(.+)$", m.group(1), re.M)
    return t.group(1).strip() if t else ""


def test_every_screen_has_help_md():
    for page in SCREEN_PAGES:
        assert (HELP_DIR / f"{page}.md").is_file(), page
        assert _title(_read(page)), page


def test_screen_md_sections_in_order():
    for page in SCREEN_PAGES:
        heads = re.findall(r"^## (.+?)\s*$", _read(page), re.M)
        assert heads == SECTIONS, (page, heads)


def test_guides_exist_and_link_routes():
    allowed = set(SCREEN_PAGES) - {"guide"}
    for name in ("guide-shorts", "guide-long"):
        text = _read(name)
        links = LINK_RE.findall(text)
        assert links, name
        assert set(links) <= allowed, (name, set(links) - allowed)


def test_help_links_are_known_routes():
    for page in SCREEN_PAGES:
        for x in LINK_RE.findall(_read(page)):
            assert x in SCREEN_PAGES, (page, x)
