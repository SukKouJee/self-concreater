"""콘티 장면 그림(art) — 근거(basis) 해시, 상태, 표시 규칙, SVG 검사.

Claude가 쓴 SVG는 저장·표시(<img>) 전에 반드시 sanitize_svg를 거친다. 이 모듈이 유일한 관문이다."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from scripts import conti
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
