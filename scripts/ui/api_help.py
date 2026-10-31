"""도움말 조회 API: docs/help의 허용된 페이지만 읽어 준다.

- 허용 목록은 `SCREEN_PAGES`(화면별 11개)와 `GUIDE_PAGES`(가이드 2개)뿐이다. `_chatbot.md` 같은 파일은 목록 밖이라 내지 않는다.
- 페이지 이름이 목록에 없으면 `help_not_found`. 서버는 모든 ScriptError를 400으로 낸다.

도움말 챗봇 작업(kind "help_chat")
- 질문 하나 = 항목 하나(id "chat"). 답은 항목 `preview={"answer": md}`로 두고 항목·작업 모두 `done`으로 끝낸다. 저장 단계는 없다.
- Claude에게는 도움말 md, 메뉴, 줄인 상황(`situation`, 카테고리마다 화면별 실제 문턱 `gates`), 단계 규칙만 준다. md 본문·대본·`## 내 메모`·작업 폴더 경로·라이브러리 원격은 보내지 않는다.
- 규칙 파일 `_chatbot.md`는 허용 목록 밖이라 조회 API로는 나가지 않는다.
- 서버판은 `docs/help/server/<이름>.md`가 있으면 그것을 쓴다(작업 폴더·터미널 같은 내 PC 전용 설명을 뺀 판). 없으면 공통 파일.
  로컬판은 언제나 `docs/help/<이름>.md` 그대로다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from scripts import ctx, draft, keys, library, llm, motif, refstore, similarity, status
from scripts.common import PLUGIN_ROOT, ScriptError
from scripts.edition import is_server
from scripts.ui.api_draft import _library_templates
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, disabled_features, route

HELP_DIR: Path = PLUGIN_ROOT / "docs" / "help"
SERVER_HELP_DIR: Path = HELP_DIR / "server"
SCREEN_PAGES: tuple[str, ...] = ("home", "refs", "library", "typify", "report", "motif", "draft", "conti",
                                 "diagnose", "mission", "guide")
GUIDE_PAGES: tuple[str, ...] = ("guide-shorts", "guide-long")

# app.js MENU와 같은 순서·내용(tests가 대조한다).
MENU: list[dict[str, str]] = [
    {"name": "홈", "route": "#/home"},
    {"name": "레퍼런스", "route": "#/refs"},
    {"name": "공용 라이브러리", "route": "#/library"},
    {"name": "유형", "route": "#/typify"},
    {"name": "모티브", "route": "#/motif"},
    {"name": "내 영상", "route": "#/videos"},
    {"name": "기획안", "route": "#/draft"},
    {"name": "콘티", "route": "#/conti"},
    {"name": "진단", "route": "#/diagnose"},
    {"name": "오늘 미션", "route": "#/mission"},
    {"name": "주간 리포트", "route": "#/report"},
    {"name": "가이드", "route": "#/guide"},
]


# 메뉴 route → 그 화면 전체를 끄는 기능(app.js PAGE_FEATURES와 같은 뜻)
MENU_FEATURES: dict[str, str] = {"#/library": "library"}


def chat_menu() -> list[dict[str, str]]:
    """챗봇에 주는 메뉴. 서버판에서는 꺼진 기능의 화면을 뺀다(없는 화면으로 안내하지 않게). 로컬판은 MENU 그대로."""
    if not is_server():
        return MENU
    off = disabled_features()
    return [item for item in MENU if MENU_FEATURES.get(item["route"]) not in off]


def help_file(name: str) -> Path:
    """판에 맞는 도움말 파일: 서버판은 `server/<name>.md`가 있으면 그것, 아니면(로컬판 포함) `<name>.md`."""
    if is_server():
        variant = SERVER_HELP_DIR / f"{name}.md"
        if variant.is_file():
            return variant
    return HELP_DIR / f"{name}.md"


def read_help(page: str) -> dict[str, str]:
    """허용 목록의 도움말 한 장을 `{"page", "title", "markdown"}`로 돌려준다(markdown은 프런트매터를 뗀 본문)."""
    if page not in SCREEN_PAGES and page not in GUIDE_PAGES:
        raise ScriptError("help_not_found", f"도움말 페이지가 없습니다: {page}")
    path = help_file(page)
    if not path.is_file():
        raise ScriptError("help_not_found", f"도움말 파일이 없습니다: {page}")
    fm, body = refstore.parse_md(path.read_bytes(), path)      # 플러그인 폴더의 파일이라 저장소 밖
    return {"page": page, "title": str(fm.get("title", page)), "markdown": body}


@route("GET", r"/api/help/(?P<page>[a-z_-]+)")
def _get_help(req: Request, page: str) -> dict[str, str]:
    return read_help(page)


# ---- 챗봇 -------------------------------------------------------------------------------

KIND = "help_chat"
MAX_QUESTION = 500
MAX_HISTORY = 6
MAX_HISTORY_TEXT = 2000
MAX_ANSWER = 4000
CHAT_TIMEOUT = 120
CHAT_TASK = "도움말 챗봇 규칙대로 question에 답해 {\"answer\": \"...\"} 형태로 출력하세요."
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}
CATEGORY_KEYS = ("category", "ref", "mine", "stage", "stage_text", "steps", "current", "next")


def _rules() -> list[Path]:
    return [help_file("_chatbot")]             # 허용 목록 밖이라 읽기 API로는 나가지 않는다


# 사용자가 손으로 고치던 index.md·유형화 json이 깨져 있어도 챗봇은 답해야 한다(화면 API는 그대로 오류를 낸다).
BROKEN_DATA = (ScriptError, ValueError, KeyError, TypeError, AttributeError, OSError)


def _warn(what: str, category: str, e: Exception) -> None:
    print(f"[help] {category} {what}를 읽지 못해 0개로 봅니다: {type(e).__name__}", file=sys.stderr)


def _draft_counts(category: str) -> dict[str, int]:
    """`draft.sources`의 개수. 유형화 json이 깨졌으면 아키타입 0개, index.md 카드까지 못 읽으면 카드도 0개로 본다."""
    try:
        sources = draft.sources(category)
        return {k: len(sources[k]) for k in ("stars", "cards", "archetypes")}
    except BROKEN_DATA as e:
        _warn("기획안 출처", category, e)
    try:
        details = motif.card_details(category)
    except BROKEN_DATA as e:
        _warn("모티브 카드", category, e)
        details = []
    stars = sum(1 for c in details if c.get("starred"))
    return {"stars": stars, "cards": len(details) - stars, "archetypes": 0}


def gates(category: str, page: str) -> dict[str, Any]:
    """화면이 실제로 쓰는 문턱. 각 화면 API와 같은 함수로 계산한다(개수·참거짓·한국어 type 이름만).

    - motif_allowed: 모티브 화면의 허용 카드(`motif.allowed_types`).
    - typify_result: 유형화 결과(json)가 있는지. 저장(typified_at)과는 별개다.
    - diagnose: 진단 화면의 `similarity.typify_gate`(available, preliminary).
    - draft_sources: 기획안 화면 출처 개수(`draft.sources`). 기획안 화면이면 지인 틀 개수(연결 전 None)도 넣는다.
      파일이 깨져 읽지 못한 개수는 0이다(`_draft_counts`).
    """
    draft_sources: dict[str, Any] = dict(_draft_counts(category))
    if page == "draft":
        templates = _library_templates()
        draft_sources["library_templates"] = None if templates is None else len(templates)
    return {
        "has_profile": ctx.here().store.exists(keys.PROFILE),
        "typify_result": motif.has_typify(category),
        "motif_allowed": [motif.TYPE_KO[t] for t in motif.allowed_types(category)],
        "diagnose": similarity.typify_gate(category),
        "draft_sources": draft_sources,
    }


def situation(category: str | None, page: str) -> dict[str, Any]:
    """`status.overview`를 챗봇용으로 줄이고 카테고리마다 `gates`를 붙인다. 경로·산출물 이름·라이브러리 원격은 넣지 않는다."""
    try:
        overview = status.overview(category)
    except ScriptError as e:
        if e.code != "category_not_found":
            raise
        overview = status.overview(None)          # 없는 카테고리는 전체로 본다
    mission = overview["mission"]
    result: dict[str, Any] = {
        "initialized": overview["initialized"],
        "categories": [{**{k: c[k] for k in CATEGORY_KEYS}, "gates": gates(c["category"], page)}
                       for c in overview["categories"]],
        "mission": {k: mission[k] for k in ("date", "exists", "status", "kind")},
    }
    if page == "library" and "library" in disabled_features():
        result["library"] = {"connected": False}          # 꺼진 기능: 라이브러리를 건드리지 않는다
    elif page == "library":
        lib = library.status()
        result["library"] = ({"connected": True, "shared_count": lib["shared_count"], "last_pull": lib["last_pull"]}
                             if lib.get("connected") else {"connected": False})
    return result


def _stage_rules() -> dict[str, Any]:
    preliminary, formal = refstore._stage_limits()
    return {
        "allowed_by_stage": {str(stage): list(types) for stage, types in motif.ALLOWED_BY_STAGE.items()},
        "type_ko": dict(motif.TYPE_KO),
        "preliminary_refs": preliminary,
        "formal_refs": formal,
    }


def validate_answer(data: Any) -> str:
    answer = data.get("answer") if isinstance(data, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise ScriptError("invalid_answer", "answer는 비어 있지 않은 문자열이어야 합니다")
    if len(answer) > MAX_ANSWER:
        raise ScriptError("invalid_answer", f"answer는 {MAX_ANSWER}자 이하여야 합니다 (지금 {len(answer)}자)")
    return answer


def _question(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_QUESTION:
        raise ScriptError("bad_request", f"질문은 1~{MAX_QUESTION}자로 써 주세요")
    return text


def _history(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScriptError("bad_request", "history는 목록이어야 합니다")
    turns: list[dict[str, str]] = []
    for turn in value:
        if (not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant")
                or not isinstance(turn.get("text"), str)):
            raise ScriptError("bad_request", "history 항목은 {role: user|assistant, text: 문자열}이어야 합니다")
        turns.append({"role": turn["role"], "text": turn["text"][:MAX_HISTORY_TEXT]})
    return turns[-MAX_HISTORY:]


def _chat_category(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScriptError("bad_request", "category는 문자열이어야 합니다")
    return value or None


def chat_target(payload: dict[str, Any]) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")
        job.check_cancelled()
        _, answer = llm.ask_validated(_rules(), payload, CHAT_TASK, validate_answer,
                                      schema=ANSWER_SCHEMA, timeout=CHAT_TIMEOUT)
        job.check_cancelled()
        job.update(0, state="done", preview={"answer": answer})
        job.finish("done")

    return target


@route("POST", "/api/help/chat")
def _chat(req: Request) -> dict[str, Any]:
    body = req.json
    page = body.get("page")
    if not isinstance(page, str):
        raise ScriptError("bad_request", "page가 필요합니다")
    screen = read_help(page)                      # 목록에 없으면 help_not_found
    question = _question(body.get("question"))
    history = _history(body.get("history"))
    category = _chat_category(body.get("category"))
    llm.check()                                   # 엔진이 없으면 상황을 모으지도, 작업을 만들지도 않는다
    payload = {
        "screen": {"page": screen["page"], "title": screen["title"], "markdown": screen["markdown"]},
        "guide": {name: read_help(name)["markdown"] for name in GUIDE_PAGES},
        "menu": chat_menu(),
        "situation": situation(category, page),
        "stage_rules": _stage_rules(),
        "history": history,
        "question": question,
    }
    job = req.app.jobs.start(KIND, chat_target(payload), items=[new_item("chat", title=page, step="claude")])
    return {"job": job.snapshot()}
