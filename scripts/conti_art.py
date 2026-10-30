"""콘티 장면 그림(art) — 근거(basis) 해시, 상태, 표시 규칙, SVG 검사.

Claude가 쓴 SVG는 저장·표시(<img>) 전에 반드시 sanitize_svg를 거친다. 이 모듈이 유일한 관문이다."""
from __future__ import annotations

import contextvars
import hashlib
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
MAX_SCENES = 300                                # 한 번에 그리는 최대 장면 수(안전 상한: 누르면 남은 장면을 끝까지)
CONCURRENCY = 3                                 # 동시에 Claude에 보내는 묶음 수
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
    옛 그림인 장면만. 앞에서부터 MAX_SCENES개(안전 상한일 뿐, 보통 콘티는 한 번에 끝까지 그린다)."""
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
    파일 이름은 `<id>-<basis>-<svg 해시 6자>.svg`: 같은 근거로 다시 그려도 앞 그림을 덮어쓰지 않는다(이력 복원이 앞 그림을
    되살리고, 브라우저가 같은 주소의 옛 그림을 캐시로 보여 주지 않게). 옛 svg 파일은 지우지 않는다."""
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
            data = "\n".join(svg.splitlines()).encode("utf-8")                              # LF로 통일
            rel = Path(*conti.ART_DIR, p.stem, f"{cut['id']}-{drawn_basis}-{hashlib.sha1(data).hexdigest()[:6]}.svg")
            target = work_root() / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            refstore.write_atomic(target, data)
            new_cuts.append({**cut, "art": {"file": rel.as_posix(), "basis": drawn_basis}})
            saved.append(cut["id"])
        skipped += [i for i in drawn if i not in saved and i not in skipped]        # 지워진 장면
        if saved:
            fm = conti.validated({**before, "cuts": new_cuts})
            conti_history.check_external(p, before)
            conti._write_conti_file(current, fm)
            conti_history.record(p, fm, "auto")
    return {"saved": saved, "skipped": skipped}


class _Tally:
    """여러 스레드에서 끝나는 묶음의 결과를 모은다. 진행 알림도 잠금 안에서 보내 done이 줄지 않게 한다."""

    def __init__(self, job: Any, total: int) -> None:
        self.lock = threading.Lock()
        self.job = job
        self.total = total
        self.done = self.drawn = self.skipped = self.failed = self.lost = 0
        self.warnings: list[str] = []
        if job is not None:
            _progress(job, 0, total)

    def batch_done(self, size: int) -> None:
        with self.lock:
            self.done = min(self.done + size, self.total)
            if self.job is not None:
                _progress(self.job, self.done, self.total)


def _progress(job: Any, done: int, total: int) -> None:
    job.update(0, message=f"{done} / {total}장면", done=done, total=total)


def _draw_batch(p: Path, number: int, batch_ids: list[str], tally: _Tally) -> None:
    """묶음 하나: 콘티를 다시 읽어 지금 값으로 그리고 저장한다. Claude 실패는 경고로 남긴다."""
    try:
        # 묶음마다 콘티를 다시 읽는다: 작업 중에 고친 장면은 고친 글로 그리고, 지워졌거나 사진을 고정한 장면은 건너뛴다
        fm = conti.validated(conti.read_conti(p).stored)
        asp = aspect(fm)
        now = {c.get("id"): c for c in fm["cuts"]}
        batch = [now[i] for i in batch_ids if i in now and not now[i].get("ref_frame")]
        with tally.lock:
            tally.skipped += len(batch_ids) - len(batch)
        if not batch:
            return
        bases = {c["id"]: basis(c, asp) for c in batch}
        scene_ids = set(bases)
        got: dict[str, str | None] = {}
        asked = True
        try:
            _, got = llm.ask_validated(rules(), _payload(fm, batch), DRAW_TASK,
                                       lambda d, s=scene_ids: _validated(d, s), max_tokens=MAX_TOKENS)
        except ScriptError as e:
            with tally.lock:
                tally.warnings.append(f"{number}번째 묶음({len(batch)}장면)을 그리지 못했어요: {e.message}")
                tally.failed += len(batch)
            asked = False
        results: dict[str, tuple[str, str]] = {}
        lost = 0
        for cut_id in bases:
            svg = got.get(cut_id)
            if svg is None:
                if asked:                                  # 묶음 호출은 됐는데 이 장면만 못 그림(null·빠짐)
                    lost += 1
                continue
            try:
                results[cut_id] = (sanitize_svg(svg, asp), bases[cut_id])
            except ScriptError:
                lost += 1
        with tally.lock:
            tally.failed += lost
            tally.lost += lost
        if results:
            out = save_art(p, results)                     # 저장은 HISTORY_LOCK이 차례로 세운다
            with tally.lock:
                tally.drawn += len(out["saved"])
                tally.skipped += len(out["skipped"])
    finally:
        tally.batch_done(len(batch_ids))


def counts(fm: dict) -> dict[str, int]:
    """고정 안 된 장면의 그림 상태 수 {"total", "missing"(없음), "stale"(옛 그림), "fresh"(최신)}."""
    asp = aspect(fm)
    out = {"total": 0, "missing": 0, "stale": 0, "fresh": 0}
    for cut in fm["cuts"]:
        if cut.get("ref_frame"):
            continue
        out["total"] += 1
        out[art_state(cut, asp) or "missing"] += 1
    return out


def draw(path: str | Path, ids: list[str] | None, stale_only: bool, job: Any = None) -> dict:
    """장면 그림을 2장면씩 묶어 CONCURRENCY개까지 동시에 Claude에 보내고 묶음마다 저장한다. 묶음이 실패하면 경고로
    남기고 다른 묶음을 계속한다. 새 묶음을 보내기 전에 job.check_cancelled(): 취소되면 새 묶음은 보내지 않고 이미 보낸
    묶음은 끝나 저장된 뒤 JobCancelled가 그대로 올라간다. 묶음 안에서 뜻밖의 오류가 나도 다른 묶음은 끝까지 가고, 새 묶음은
    보내지 않은 채 그 오류를 올린다.
    반환 {"drawn", "skipped", "failed", "warnings", "remaining"}(remaining: 끝난 뒤 고정 안 된 장면 중 그림이 없거나 옛
    그림인 수). job: update(index, **fields)·check_cancelled()."""
    p = conti._resolve(path)
    with conti.HISTORY_LOCK:
        _, fm, _ = conti.ensure_ids_file(conti.read_conti(p))      # id가 없던 옛 파일은 먼저 저장해 둔다
    if scene_merge.has_repeats(fm["cuts"]):
        raise ScriptError("merge_first", MERGE_FIRST_MESSAGE)
    todo = [c["id"] for c in targets(fm, ids, stale_only)]
    batches = list(enumerate((todo[i:i + BATCH] for i in range(0, len(todo), BATCH)), 1))
    tally = _Tally(job, len(todo))
    pending: set[Future[None]] = set()
    error: BaseException | None = None
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="conti-art")
    try:
        queue = iter(batches)
        while True:
            while error is None and len(pending) < CONCURRENCY:
                nxt = next(queue, None)
                if nxt is None:
                    break
                if job is not None:
                    job.check_cancelled()
                pending.add(pool.submit(contextvars.copy_context().run, _draw_batch, p, nxt[0], nxt[1], tally))   # 묶음마다 새 복사본
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                exc = fut.exception()
                if exc is not None and error is None:
                    error = exc                            # 다른 묶음은 끝까지 두고 새 묶음은 보내지 않는다
    finally:
        pool.shutdown(wait=True, cancel_futures=True)      # 취소돼도 이미 보낸 묶음은 끝나 저장된다
    if error is not None:
        raise error
    if tally.lost:
        tally.warnings.append(f"{tally.lost}장면은 그림을 받지 못했어요")
    left = counts(conti.validated(conti.read_conti(p).stored))
    return {"drawn": tally.drawn, "skipped": tally.skipped, "failed": tally.failed, "warnings": tally.warnings,
            "remaining": left["missing"] + left["stale"]}
