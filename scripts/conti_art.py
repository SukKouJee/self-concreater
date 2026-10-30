"""콘티 장면 그림(art) — 근거(basis) 해시, 상태, 표시 규칙, SVG 검사.

Claude가 쓴 SVG는 저장·표시(<img>) 전에 반드시 sanitize_svg를 거친다. 이 모듈이 유일한 관문이다."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, llm, refstore, scene_merge
from scripts.common import ScriptError, work_root

ART_FIELDS = conti.PICK_FIELDS                  # 그림의 근거가 되는 컷 필드: action, guide, size
MAX_SVG_BYTES = 40 * 1024
VIEWBOX = {"16:9": "0 0 1600 900", "9:16": "0 0 900 1600"}

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ALLOWED_TAGS = frozenset({
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "defs",
    "linearGradient", "radialGradient", "stop", "clipPath", "title", "marker", "use",
})
_URL_RE = re.compile(r"url\s*\(", re.IGNORECASE)
_LOCAL_URL_RE = re.compile(r"url\(#", re.IGNORECASE)
_FUNC_RE = re.compile(r"([A-Za-z_-][\w-]*)\s*\(")
# 속성 값에 쓸 수 있는 함수 이름. image-set 등 외부 자원을 가져오는 함수는 모두 막는다.
ALLOWED_FUNCS = frozenset({
    "url", "rgb", "rgba", "hsl", "hsla", "translate", "rotate", "scale", "matrix", "skewx", "skewy",
    "translatex", "translatey",
})
_DOCTYPE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

BATCH = 2                                       # Claude 한 번에 그리는 장면 수(출력 한도 때문)
MAX_SCENES = 80                                 # 한 번에 그리는 최대 장면 수
MAX_TOKENS = 16000
DRAW_TASK = "draw-scenes 규칙대로 장면마다 SVG 그림 하나를 그린 JSON을 만드세요."
MERGE_FIRST_MESSAGE = "반복 컷을 먼저 장면으로 합쳐 주세요"


def aspect(fm: dict) -> str:
    return "9:16" if fm.get("format") == "shorts" else "16:9"


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def basis(cut: dict, aspect: str) -> str:
    """그림이 어떤 내용을 그린 것인지 가리키는 지문(sha1 앞 8자). 대사·자막·효과음·초는 영향 없다."""
    parts = [_norm(cut.get("action")), _norm(cut.get("guide")), str(cut.get("size") or ""), aspect]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:8]


def art_state(cut: dict, aspect: str) -> str | None:
    """art 없음·파일 없음 → None, basis 같음 → fresh, 다름 → stale."""
    art = conti.art_of(cut.get("art"))
    return _state_of(art, cut, aspect)


def _state_of(art: dict | None, cut: dict, aspect: str) -> str | None:
    if art is None or not (work_root() / art["file"]).is_file():
        return None
    return "fresh" if art["basis"] == basis(cut, aspect) else "stale"


def display_art(cut: dict, aspect: str) -> dict | None:
    """보여줄 그림. 사용자가 고정한 참고 프레임(ref_frame)이 있으면 그림은 숨긴다."""
    if cut.get("ref_frame"):
        return None
    art = conti.art_of(cut.get("art"))
    state = _state_of(art, cut, aspect)
    if state is None or art is None:
        return None
    return {"file": art["file"], "state": state}


def _invalid(reason: str) -> ScriptError:
    return ScriptError("invalid_svg", f"SVG를 쓸 수 없습니다: {reason}")


def _local(tag: str) -> tuple[str, str]:
    """'{ns}name' → (ns, name)."""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _attr_ok(key: str, value: str) -> bool:
    ns, name = _local(key)
    if ns not in ("", XLINK_NS) or (ns == XLINK_NS and name != "href"):
        return False
    if name.lower().startswith("on"):
        return False
    if name == "href":
        return value.startswith("#")
    if "\\" in value:                                   # CSS 이스케이프로 url( 을 숨기는 시도
        return False
    if any(f.lower() not in ALLOWED_FUNCS for f in _FUNC_RE.findall(value)):
        return False
    if _URL_RE.search(value) and len(_URL_RE.findall(value)) != len(_LOCAL_URL_RE.findall(value)):
        return False
    return True


def _clean(el: ET.Element) -> None:
    prev: ET.Element | None = None
    for child in list(el):
        ns, name = _local(child.tag)
        if ns not in ("", SVG_NS) or name not in ALLOWED_TAGS:
            el.remove(child)                            # 자식째 제거, 뒤따르는 글자(tail)는 앞으로 붙인다
            if child.tail:
                if prev is None:
                    el.text = (el.text or "") + child.tail
                else:
                    prev.tail = (prev.tail or "") + child.tail
            continue
        prev = child
        child.tag = name                                  # 접두사 없이 직렬화하려고 평범한 이름으로
        _clean_attrs(child)
        _clean(child)


def _clean_attrs(el: ET.Element) -> None:
    for key in list(el.attrib):
        if not _attr_ok(key, el.attrib[key]):
            del el.attrib[key]
        elif key == f"{{{XLINK_NS}}}href":              # 접두사(ns1:)가 안 생기도록 평범한 href로
            el.attrib["href"] = el.attrib.pop(key)


def sanitize_svg(text: str, aspect: str) -> str:
    """허용 목록으로 SVG를 걸러 다시 직렬화한다. 실패하면 ScriptError('invalid_svg')."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SVG_BYTES:
        raise _invalid(f"{MAX_SVG_BYTES // 1024}KB를 넘거나 문자열이 아닙니다")
    if _DOCTYPE_RE.search(text):
        raise _invalid("DOCTYPE·ENTITY는 쓸 수 없습니다")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise _invalid(f"XML이 아닙니다 ({e})") from e
    ns, name = _local(root.tag)
    if name != "svg" or ns not in ("", SVG_NS):
        raise _invalid("뿌리 요소가 svg가 아닙니다")
    root.tag = "svg"
    _clean_attrs(root)
    for key in ("width", "height", "viewBox"):
        root.attrib.pop(key, None)
    root.set("viewBox", VIEWBOX.get(aspect, VIEWBOX["16:9"]))
    root.set("xmlns", SVG_NS)                           # 기본 네임스페이스는 속성으로 직접 붙인다(전역 등록 없음)
    _clean(root)
    out = ET.tostring(root, encoding="unicode")
    if len(out.encode("utf-8")) > MAX_SVG_BYTES:
        raise _invalid(f"걸러낸 결과가 {MAX_SVG_BYTES // 1024}KB를 넘습니다")
    return out


# --- 그리기와 저장 ---------------------------------------------------------------------------

def rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-conti" / "draw-scenes.md"]


def targets(fm: dict, ids: list[str] | None, stale_only: bool) -> list[dict]:
    """그릴 장면. 고정한 사진(ref_frame)이 있는 장면은 뺀다. ids가 있으면 그 장면만, stale_only면 그림이 없거나
    옛 그림인 장면만. 앞에서부터 MAX_SCENES개."""
    asp = aspect(fm)
    wanted = set(ids) if ids is not None else None
    out: list[dict] = []
    for cut in fm["cuts"]:
        if cut.get("ref_frame") or (wanted is not None and cut.get("id") not in wanted):
            continue
        if stale_only and art_state(cut, asp) == "fresh":
            continue
        out.append(cut)
    return out[:MAX_SCENES]


def _invalid_art(message: str) -> ScriptError:
    return ScriptError("invalid_art", f"장면 그림 결과가 형식에 맞지 않습니다: {message}")


def _validated(data: Any, ids: set[str]) -> dict[str, str | None]:
    """{"scenes": [{"id", "svg"}]} → 장면 id → svg 문자열 또는 None. 묶음 밖 id·틀린 형식은 invalid_art."""
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, list):
        raise _invalid_art("scenes 목록이 없습니다")
    out: dict[str, str | None] = {}
    for entry in scenes:
        if not isinstance(entry, dict):
            raise _invalid_art("scenes의 항목은 {id, svg} 객체여야 합니다")
        cut_id, svg = entry.get("id"), entry.get("svg")
        if not isinstance(cut_id, str) or cut_id not in ids:
            raise _invalid_art(f"이번 묶음에 없는 장면 id입니다: {cut_id!r}")
        if svg is not None and not isinstance(svg, str):
            raise _invalid_art("svg는 문자열 또는 null이어야 합니다")
        out[cut_id] = svg
    return out


def _payload(fm: dict, batch: list[dict]) -> dict:
    cuts = fm["cuts"]
    index = {c["id"]: i for i, c in enumerate(cuts)}
    labels = [s["label"] for s in fm["script"]]

    def action_at(i: int) -> str | None:
        return cuts[i].get("action") if 0 <= i < len(cuts) else None

    scenes = []
    for c in batch:
        i = index[c["id"]]
        scenes.append({"id": c["id"], "action": c.get("action"), "guide": c.get("guide"), "size": c.get("size"),
                       "angle": c.get("angle"), "section": labels[c["section"]],
                       "prev": action_at(i - 1), "next": action_at(i + 1)})
    return {"scenes": scenes, "format": aspect(fm), "title": fm.get("title")}


def save_art(path: str | Path, drawn: dict[str, tuple[str, str]]) -> dict:
    """그린 그림을 파일로 쓰고 컷의 art를 갱신한다. drawn: 컷 id → (sanitize한 svg, 그릴 때 basis).
    다시 읽은 콘티에서 그 장면이 지금도 같은 basis이고 고정되지 않았을 때만 저장한다(고치는 사이 끝난 옛 그림 방지).
    옛 svg 파일은 지우지 않는다."""
    from scripts import conti_history                  # conti_history가 conti를 쓰므로 함수 안에서 불러온다

    p = conti._resolve(path)
    saved: list[str] = []
    skipped: list[str] = []
    with conti.HISTORY_LOCK:
        current, before, _ = conti.ensure_ids_file(conti.read_conti(p))
        asp = aspect(before)
        new_cuts = []
        for cut in before["cuts"]:
            hit = drawn.get(cut.get("id"))
            if hit is None:
                new_cuts.append(cut)
                continue
            svg, drawn_basis = hit
            if cut.get("ref_frame") or basis(cut, asp) != drawn_basis:
                skipped.append(cut["id"])
                new_cuts.append(cut)
                continue
            rel = Path(*conti.ART_DIR, p.stem, f"{cut['id']}-{drawn_basis}.svg")
            target = work_root() / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            refstore.write_atomic(target, "\n".join(svg.splitlines()).encode("utf-8"))      # LF로 통일
            new_cuts.append({**cut, "art": {"file": rel.as_posix(), "basis": drawn_basis}})
            saved.append(cut["id"])
        skipped += [i for i in drawn if i not in saved and i not in skipped]        # 지워진 장면
        if saved:
            fm = conti.validated({**before, "cuts": new_cuts})
            conti_history.check_external(p, before)
            conti._write_conti_file(current, fm)
            conti_history.record(p, fm, "auto")
    return {"saved": saved, "skipped": skipped}


def draw(path: str | Path, ids: list[str] | None, stale_only: bool, job: Any = None) -> dict:
    """장면 그림을 2장면씩 그려 묶음마다 저장한다. 묶음이 실패하면 경고로 남기고 다음 묶음을 계속한다.
    반환 {"drawn", "skipped", "failed", "warnings"}. job: update(index, **fields)·check_cancelled()."""
    p = conti._resolve(path)
    fm = conti.validated(conti.read_conti(p).stored)
    if scene_merge.has_repeats(fm["cuts"]):
        raise ScriptError("merge_first", MERGE_FIRST_MESSAGE)
    asp = aspect(fm)
    todo = targets(fm, ids, stale_only)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    drawn_n = skipped = failed = 0
    warnings: list[str] = []
    for number, batch in enumerate(batches, 1):
        if job is not None:
            job.check_cancelled()
        bases = {c["id"]: basis(c, asp) for c in batch}
        scene_ids = set(bases)
        got: dict[str, str | None] = {}
        asked = True
        try:
            _, got = llm.ask_validated(rules(), _payload(fm, batch), DRAW_TASK,
                                       lambda d, s=scene_ids: _validated(d, s), max_tokens=MAX_TOKENS)
        except ScriptError as e:
            warnings.append(f"{number}번째 묶음({len(batch)}장면)을 그리지 못했어요: {e.message}")
            failed += len(batch)
            asked = False
        results: dict[str, tuple[str, str]] = {}
        for cut_id in bases:
            svg = got.get(cut_id)
            if svg is None:
                if asked:                                  # 묶음 호출은 됐는데 이 장면만 못 그림(null·빠짐)
                    failed += 1
                continue
            try:
                results[cut_id] = (sanitize_svg(svg, asp), bases[cut_id])
            except ScriptError:
                failed += 1
        if results:
            out = save_art(p, results)
            drawn_n += len(out["saved"])
            skipped += len(out["skipped"])
        if job is not None:
            job.update(0, done=min(number * BATCH, len(todo)), total=len(todo))
    return {"drawn": drawn_n, "skipped": skipped, "failed": failed, "warnings": warnings}
