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
    for rel in ["app.css", "app.js", "api.js", "pages/home.js", "pages/placeholder.js", "pages/add.js"]:
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


ADD_OPTION_LABELS = [
    "샷 분류도 함께", "이미 분석한 영상도 다시 분석",
    "자막·컷을 새로 받기(요청이 늘어 차단될 수 있어요)", "추가 시작", "샷 분류만 보강",
]
SECTION_LABELS = ["hook", "intro", "body", "demo", "transition", "cta", "outro"]


def test_add_page_has_option_labels():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    for label in ADD_OPTION_LABELS:
        assert label in text, label


def test_add_page_confirm_card_and_guards():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert ", ".join(f'"{x}"' for x in SECTION_LABELS) in text
    for token in ["isCurrent()", "/api/jobs/current?kind=collect", "encodeURIComponent", "filesUrl",
                  "clearTimeout", "음성 인식(STT)으로", "화면만 보고 분석", "남은 영상 모두 저장", "reviewed: true"]:
        assert token in text, token
    for banned in ["innerHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


def test_add_page_shots_opt_in_and_polling_safety():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert "shots: false" in text and "shots: true" not in text
    for token in ["job_not_found", "POLL_MAX_MS", 'aria-live']:
        assert token in text, token
    assert 'role: "status" }, text' not in text
