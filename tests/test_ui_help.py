"""도움말 조회 API: 허용 목록 안의 docs/help 페이지만 내고, 메뉴 표는 app.js MENU와 같아야 한다."""
import re

import pytest

from scripts.ui.api_help import MENU
from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401 - fixtures


def test_get_help_screen(client):
    status, body = client.json("GET", "/api/help/motif")
    assert status == 200
    assert body["page"] == "motif"
    assert body["title"] == "모티브"
    assert body["markdown"].lstrip().startswith("## 무엇을 하나요")
    assert "title:" not in body["markdown"].split("\n", 1)[0]


@pytest.mark.parametrize("page", ["guide-shorts", "guide-long"])
def test_get_help_guide(client, page):
    status, body = client.json("GET", f"/api/help/{page}")
    assert status == 200
    assert body["page"] == page


@pytest.mark.parametrize("page", ["nope", "_chatbot"])
def test_get_help_unknown(client, page):
    status, body = client.json("GET", f"/api/help/{page}")
    assert status == 400
    assert body["error"] == "help_not_found"


def test_get_help_traversal(client):
    status, _ = client.json("GET", "/api/help/..%2FREADME")
    assert status != 200


def test_menu_matches_app_js():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    found = re.findall(r'\{ name: "([^"]+)", route: "(#/[^"]+)" \}', text)
    assert found == [(m["name"], m["route"]) for m in MENU]
