"""도움말 조회 API: docs/help의 허용된 페이지만 읽어 준다.

- 허용 목록은 `SCREEN_PAGES`(화면별 12개)와 `GUIDE_PAGES`(가이드 2개)뿐이다. `_chatbot.md` 같은 파일은 목록 밖이라 내지 않는다.
- 페이지 이름이 목록에 없으면 `help_not_found`. 서버는 모든 ScriptError를 400으로 낸다.
"""
from __future__ import annotations

from pathlib import Path

from scripts import refstore
from scripts.common import PLUGIN_ROOT, ScriptError
from scripts.ui.server import Request, route

HELP_DIR: Path = PLUGIN_ROOT / "docs" / "help"
SCREEN_PAGES: tuple[str, ...] = ("home", "add", "refs", "library", "typify", "report", "motif", "draft", "conti",
                                 "diagnose", "mission", "guide")
GUIDE_PAGES: tuple[str, ...] = ("guide-shorts", "guide-long")

# app.js MENU와 같은 순서·내용(tests가 대조한다).
MENU: list[dict[str, str]] = [
    {"name": "홈", "route": "#/home"},
    {"name": "레퍼런스 추가", "route": "#/add"},
    {"name": "레퍼런스 목록", "route": "#/refs"},
    {"name": "공용 라이브러리", "route": "#/library"},
    {"name": "유형화", "route": "#/typify"},
    {"name": "리포트", "route": "#/report"},
    {"name": "모티브", "route": "#/motif"},
    {"name": "기획안", "route": "#/draft"},
    {"name": "콘티", "route": "#/conti"},
    {"name": "진단", "route": "#/diagnose"},
    {"name": "미션", "route": "#/mission"},
    {"name": "가이드", "route": "#/guide"},
]


def read_help(page: str) -> dict[str, str]:
    """허용 목록의 도움말 한 장을 `{"page", "title", "markdown"}`로 돌려준다(markdown은 프런트매터를 뗀 본문)."""
    if page not in SCREEN_PAGES and page not in GUIDE_PAGES:
        raise ScriptError("help_not_found", f"도움말 페이지가 없습니다: {page}")
    path = HELP_DIR / f"{page}.md"
    if not path.is_file():
        raise ScriptError("help_not_found", f"도움말 파일이 없습니다: {page}")
    fm, body = refstore.read_md(path)
    return {"page": page, "title": str(fm.get("title", page)), "markdown": body}


@route("GET", r"/api/help/(?P<page>[a-z_-]+)")
def _get_help(req: Request, page: str) -> dict[str, str]:
    return read_help(page)
