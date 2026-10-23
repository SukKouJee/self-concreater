import re

from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401  (fixture 재사용)

MENU_NAMES = ["홈", "레퍼런스 추가", "레퍼런스 목록", "유형화", "리포트", "모티브", "기획안", "콘티", "진단", "미션"]
COMMANDS = ["/yt-coach:yt-typify", "/yt-coach:yt-review", "/yt-coach:yt-inspire",
            "/yt-coach:yt-draft", "/yt-coach:yt-diagnose", "/yt-coach:yt-mission"]


def test_index_loads_modules(client):
    status, _, raw = client.call("GET", f"/?key={client.app.token}", token=False)
    assert status == 200
    html = raw.decode("utf-8")
    assert re.search(r'<script[^>]*type="module"[^>]*src="/static/app\.js"', html) or \
        re.search(r'<script[^>]*src="/static/app\.js"[^>]*type="module"', html)
    assert "/static/app.css" in html


def test_static_files_served(client):
    for rel in ["app.css", "app.js", "api.js", "pages/home.js", "pages/placeholder.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200, rel
    status, headers, _ = client.call("GET", "/static/app.js")
    assert "javascript" in headers["Content-Type"]


def test_menu_definition_has_all_items():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in MENU_NAMES:
        assert f'"{name}"' in text, name
    for cmd in COMMANDS:
        assert cmd in text, cmd


def test_no_external_resources():
    for path in STATIC_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"https?://", text), path.name


def test_page_contract_guards_present():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in ["isCurrent", "signal", "onCategory", 'startsWith("#/")', "preventDefault"]:
        assert token in app, token
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert home.count("isCurrent()") >= 4
    assert "renderTopbar" not in app
