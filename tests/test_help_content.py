"""도움말 원본 md(docs/help) 형식 검사."""
import re
from pathlib import Path

HELP_DIR = Path(__file__).resolve().parent.parent / "docs" / "help"
SCREEN_PAGES = ["home", "refs", "library", "typify", "report", "motif",
                "draft", "conti", "diagnose", "mission", "guide"]
SECTIONS = ["무엇을 하나요", "사용 예", "이렇게 나와요", "알아 둘 것", "다음 단계"]
LINK_RE = re.compile(r"\(#/([a-z]+)")
ROUTE_ALIASES = {"add", "videos"}  # #/add opens the 레퍼런스 screen (help page: refs); #/videos is 내 영상 (help: draft)


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
    allowed = (set(SCREEN_PAGES) - {"guide"}) | ROUTE_ALIASES
    for name in ("guide-shorts", "guide-long"):
        text = _read(name)
        links = LINK_RE.findall(text)
        assert links, name
        assert set(links) <= allowed, (name, set(links) - allowed)


def test_help_links_are_known_routes():
    for page in SCREEN_PAGES:
        for x in LINK_RE.findall(_read(page)):
            assert x in SCREEN_PAGES or x in ROUTE_ALIASES, (page, x)


# Help pages are shown on screen (help panel, guide). They name places the way the screens do: no file names,
# folder paths or terminal commands. `_chatbot.md` is the chatbot's own rule file (never shown, not served by
# /api/help) but it is held to the same words so answers do not echo them.
HIDDEN_IN_HELP = (".md", "workspace/", "drafts/", "reviews/", "ytc ", "pip ", "gh ", "/yt-coach:")
HELP_ALLOWED: set[tuple[str, str]] = set()   # (file, token) pairs that must stay; empty: nothing needs to


def test_help_pages_hide_file_names_paths_and_commands():
    bad = []
    for path in sorted(HELP_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for token in HIDDEN_IN_HELP:
            if token in text and (path.name, token) not in HELP_ALLOWED:
                bad.append((path.name, token))
    assert bad == []
