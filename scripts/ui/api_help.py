"""도움말 조회 API: docs/help의 허용된 페이지만 읽어 준다.

- 허용 목록은 `SCREEN_PAGES`(화면별 12개)와 `GUIDE_PAGES`(가이드 2개)뿐이다. `_chatbot.md` 같은 파일은 목록 밖이라 내지 않는다.
- 페이지 이름이 목록에 없으면 `help_not_found`. 서버는 모든 ScriptError를 400으로 낸다.

도움말 챗봇 작업(kind "help_chat")
- 질문 하나 = 항목 하나(id "chat"). 답은 항목 `preview={"answer": md}`로 두고 항목·작업 모두 `done`으로 끝낸다. 저장 단계는 없다.
- Claude에게는 도움말 md, 메뉴, 줄인 상황(`situation`), 단계 규칙만 준다. md 본문·대본·`## 내 메모`·작업 폴더 경로·라이브러리 원격은 보내지 않는다.
- 규칙 파일 `_chatbot.md`는 허용 목록 밖이라 조회 API로는 나가지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import library, llm, motif, refstore, status
from scripts.common import PLUGIN_ROOT, ScriptError
from scripts.ui.jobs import Job, new_item
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
    return [HELP_DIR / "_chatbot.md"]          # 허용 목록 밖이라 읽기 API로는 나가지 않는다


def situation(category: str | None, page: str) -> dict[str, Any]:
    """`status.overview`를 챗봇용으로 줄인다. 경로·최근 산출물 이름·라이브러리 원격은 넣지 않는다."""
    try:
        overview = status.overview(category)
    except ScriptError as e:
        if e.code != "category_not_found":
            raise
        overview = status.overview(None)          # 없는 카테고리는 전체로 본다
    mission = overview["mission"]
    counts: dict[str, int] = {}
    for item in overview["recent"]:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    result: dict[str, Any] = {
        "initialized": overview["initialized"],
        "categories": [{k: c[k] for k in CATEGORY_KEYS} for c in overview["categories"]],
        "mission": {k: mission[k] for k in ("date", "exists", "status", "kind")},
        "recent_counts": counts,
    }
    if page == "library":
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
    payload = {
        "screen": {"page": screen["page"], "title": screen["title"], "markdown": screen["markdown"]},
        "guide": {name: read_help(name)["markdown"] for name in GUIDE_PAGES},
        "menu": MENU,
        "situation": situation(category, page),
        "stage_rules": _stage_rules(),
        "history": history,
        "question": question,
    }
    llm.check()                                   # 엔진이 없으면 작업을 만들지 않는다
    job = req.app.jobs.start(KIND, chat_target(payload), items=[new_item("chat", title=page, step="claude")])
    return {"job": job.snapshot()}
