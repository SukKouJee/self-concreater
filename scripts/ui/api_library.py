"""공용 라이브러리 API: 연결·받기·목록·상세·내 기법/기획안·미리보기·섹션 요약(작업)·올리기.

원칙
- git을 건드리는 요청(connect, pull, confirm)은 요청 스레드에서 동기로 실행한다(library가 60초 제한을 둔다).
  다른 작업이 라이브러리를 쓰는 중(`library_busy`)이면 HTTP 409, 나머지 ScriptError는 기존대로 400.
- 섹션 요약(Claude 1회)만 작업(kind "library_summary")이다. 작업은 항목을 `state="awaiting", step="preview"`로 두고
  `done`으로 끝난다. 요약은 미리보기 파일에 이미 저장돼 있어서, 화면은 `preview.preview`를 그대로 쓰면 된다.
- 응답에 사용자의 절대 경로를 싣지 않는다(오류 메시지의 작업 폴더 경로는 가리고, 로컬 저장소 주소는 마지막 이름만).
- 올리기는 `confirm` 요청 하나뿐이고, 화면이 인라인 확인을 거친 뒤에만 보낸다. 자동 올리기는 없다.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from scripts import library, llm, refstore
from scripts.common import ScriptError, work_root
from scripts.ui import api_draft
from scripts.ui.api_conti import _rel, _safe_path
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "library_summary"
BUSY_CODE = "library_busy"
PATH_MASK = "<작업 폴더>"
_LOCAL_REMOTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|file:|\.{1,2}[\\/])")
# 남은 절대 경로만 가린다: 윈도 드라이브 경로, 앞이 잘린 역슬래시 경로(두 칸 이상), 유닉스의 홈·임시·마운트 아래 경로.
# `refs/heads/main`이나 URL의 /경로는 걸리지 않는다.
_ABS_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z]:(?:\\|/(?!/))[^\s'\")]*"
    r"|(?<![\w:/.])\\[^\s'\")\\]+\\[^\s'\")]*"
    r"|(?<![\w:/.])/(?:home|Users|tmp|var|mnt|root)(?:/[^\s'\")]*)?"
)
PATH_ANY = "<경로>"


# ---- 도우미 -----------------------------------------------------------------------------

def _scrub(text: Any) -> Any:
    """문자열에서 작업 폴더의 절대 경로를 가린다(슬래시·역슬래시 모두)."""
    if not isinstance(text, str):
        return text
    root = work_root()
    variants = {str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()}
    for v in sorted(variants, key=len, reverse=True):
        if v:
            text = text.replace(v, PATH_MASK)
            text = text.replace(v.replace("/", "\\"), PATH_MASK)
    return _ABS_PATH_RE.sub(PATH_ANY, text)


def _remote_label(remote: Any) -> Any:
    """로컬 경로 저장소는 절대 경로 대신 마지막 이름만 보여 준다."""
    if not isinstance(remote, str) or not _LOCAL_REMOTE_RE.match(remote):
        return remote
    name = re.split(r"[\\/]", remote.rstrip("\\/"))[-1]
    return f"(로컬) {name}"


def _clean(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    if "remote" in out:
        out["remote"] = _remote_label(out["remote"])
    if "warning" in out:
        out["warning"] = _scrub(out["warning"])
    return out


def _respond(fn: Callable[[], dict[str, Any]]) -> dict[str, Any] | tuple[int, dict[str, Any]]:
    try:
        return _clean(fn())
    except OSError as e:               # 파일 권한·디스크 오류: 경로 없이 한국어로
        reason = _scrub(e.strerror or type(e).__name__)
        raise ScriptError("library_io", f"라이브러리 파일을 읽거나 쓰지 못했습니다({reason}). 권한과 디스크 공간을 확인하세요.") from e
    except ScriptError as e:
        details = {k: _scrub(v) for k, v in e.details.items()}
        if e.code == BUSY_CODE:
            return 409, {"error": e.code, "message": _scrub(e.message), **details}
        raise ScriptError(e.code, _scrub(e.message), e.exit_code, **details) from e


def _str(body: dict, key: str, *, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ScriptError("bad_request", f"{key}가 필요합니다")
    return value


def _opt_flag(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ScriptError("bad_request", f"{name}는 true/false여야 합니다")
    return value


def _query_flag(value: str | None) -> bool | None:
    if value in (None, ""):
        return None
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise ScriptError("bad_request", "worked는 1 또는 0이어야 합니다")


def _present(preview_id: str) -> dict[str, Any]:
    return library.present_preview(preview_id)


# ---- 상태 · 연결 · 받기 -------------------------------------------------------------------

@route("GET", "/api/library/status")
def _status(req: Request) -> dict:
    return _respond(library.status)


@route("POST", "/api/library/connect")
def _connect(req: Request) -> dict:
    remote = _str(req.json, "remote")
    author = _str(req.json, "author")
    return _respond(lambda: library.connect(remote or "", author or ""))


@route("POST", "/api/library/pull")
def _pull(req: Request) -> dict:
    return _respond(library.pull)


# ---- 목록 · 상세 --------------------------------------------------------------------------

@route("GET", "/api/library/items")
def _items(req: Request) -> dict:
    q = req.query
    worked = _query_flag(q.get("worked"))
    return _respond(lambda: library.search(
        q.get("query") or None, q.get("category") or None, q.get("kind") or None, q.get("author") or None, worked))


@route("GET", r"/api/library/items/(?P<item_id>[^/]+)")
def _item(req: Request, item_id: str) -> dict:
    return _respond(lambda: library.get(item_id))


# ---- 내 기법 · 내 기획안 ------------------------------------------------------------------

@route("GET", "/api/library/my/techniques")
def _my_techniques(req: Request) -> dict:
    category = _str(req.query, "category")
    return _respond(lambda: {"techniques": library.my_techniques(category or "")})


@route("GET", "/api/library/my/drafts")
def _my_drafts(req: Request) -> dict:
    category = _str(req.query, "category") or ""
    refstore.existing_category_dir(category)              # 없는 카테고리는 category_not_found
    return api_draft.list_drafts(category)


# ---- 미리보기 -----------------------------------------------------------------------------

@route("POST", "/api/library/preview")
def _preview(req: Request) -> dict:
    body = req.json
    kind = body.get("kind")
    if kind == "technique":
        category = _str(body, "category") or ""
        technique_kind = _str(body, "technique_kind") or ""
        name = _str(body, "name") or ""
        desc = body.get("desc")
        if desc is not None and not isinstance(desc, str):
            raise ScriptError("bad_request", "desc는 문자열이어야 합니다")

        def make() -> dict[str, Any]:
            created = library.preview_technique(category, technique_kind, name, desc)
            return _present(created["preview_id"])
    elif kind == "template":
        path = _safe_path(body.get("draft"), conti_file=False)

        def make() -> dict[str, Any]:
            created = library.preview_template(_rel(path))
            return _present(created["preview_id"])
    else:
        raise ScriptError("bad_request", "kind는 technique 또는 template이어야 합니다")
    return _respond(make)


@route("GET", r"/api/library/preview/(?P<preview_id>[^/]+)")
def _preview_get(req: Request, preview_id: str) -> dict:
    return _respond(lambda: _present(preview_id))


@route("POST", "/api/library/discard")
def _discard(req: Request) -> dict:
    preview_id = _str(req.json, "preview_id") or ""
    return _respond(lambda: library.discard_preview(preview_id))


# ---- 섹션 요약(작업) ----------------------------------------------------------------------

def summary_target(preview_id: str) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")
        job.check_cancelled()
        library.summarize_template(preview_id)
        job.update(0, state="awaiting", step="preview", ask="edit", preview={"preview": _present(preview_id)})
        job.finish("done", {"preview_id": preview_id})

    return target


@route("POST", "/api/library/summarize")
def _summarize(req: Request) -> dict:
    preview_id = _str(req.json, "preview_id") or ""

    def start() -> dict[str, Any]:
        data = library.load_preview(preview_id)          # 없거나 만료된 미리보기는 작업을 만들기 전에 알린다
        if data["kind"] != "template":
            raise ScriptError("invalid_preview", "기획안 틀 미리보기만 요약할 수 있습니다")
        llm.check()                                      # claude가 없으면 작업을 만들지 않는다
        items = [new_item(preview_id, title=str(data["fm"].get("title") or ""), step="claude")]
        job = req.app.jobs.start(KIND, summary_target(preview_id), items=items, category=data["category"])
        return {"job": job.snapshot()}

    return _respond(start)


# ---- 올리기 -------------------------------------------------------------------------------

@route("POST", "/api/library/confirm")
def _confirm(req: Request) -> dict:
    body = req.json
    preview_id = _str(body, "preview_id") or ""
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ScriptError("bad_request", "note는 문자열이어야 합니다")
    worked = _opt_flag(body.get("worked"), "worked")
    force_new = _opt_flag(body.get("force_new"), "force_new") or False
    edits = body.get("edits")
    if edits is not None and not isinstance(edits, dict):
        raise ScriptError("bad_request", "edits는 객체여야 합니다")
    return _respond(lambda: library.confirm(preview_id, note, worked, edits, force_new))
