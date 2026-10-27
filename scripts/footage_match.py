"""촬영 영상 ↔ 콘티 컷 맞추기와 판정 저장

흐름: 글자 후보(로컬, 비용 0) → Claude 묶음 호출 → 컷별 판정 → workspace/footage_match/<콘티 stem>.json.

- 대사가 있고 받아쓴 말과 글자가 조금이라도 겹치는 컷은 글자만 보낸다(후보 구간 + 받아쓴 말).
  그 밖의 컷(대사 없음, 받아쓰기 없음, 겹치는 말 없음)은 장면 모음 이미지를 보고 고른다.
- 사진은 장면 모음 이미지 1장 = 1장. 호출당 IMAGES_PER_CALL장, 한 번 처리에 IMAGES_PER_RUN장.
  사진 컷은 한 호출에 같이 묶여 같은 사진들을 보고, 말이 적은 장면부터 보낸다(말 없는 컷은 대개 말 없는 장면).
  상한 때문에 못 본 장면이 남았는데 확신도 높은 후보가 없는 사진 컷은 review + CAP_REASON.
- 판정 kind: auto(확신도 high 후보를 선택), review(후보는 있지만 high 없음 / 상한), missing(후보 없음),
  사용자 pinned(고른 구간 고정) / not_filmed(못 찍음 고정). 사용자 판정(user: true)은 다시 맞춰도 그대로 둔다.
- 저장은 파일 지문(fid)만 쓴다. 원본이 사라졌는지는 화면에 보일 때 연결 폴더로 따로 확인한다(F7).
- 콘티에서 지운 컷의 판정은 지우지 않는다(이력 복원으로 같은 id가 돌아오면 판정도 돌아옴, F6).
  화면·내보내기는 visible_cuts로 지금 콘티에 있는 컷만 쓴다.
- 새 컷 후보(D)는 같은 호출에 함께 묻는다. 숨긴 후보와 겹치는 새 후보는 다시 내놓지 않는다.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, footage, llm, refstore
from scripts.common import ScriptError, workspace_dir
from scripts.features import SHOT_SIZES

IMAGES_PER_CALL = 8
IMAGES_PER_RUN = 120
TEXT_CUTS_PER_CALL = 8       # 글자 호출 하나에 묶는 컷 수
TEXT_K = 5                   # 컷마다 Claude에게 보여 줄 글자 후보 수
MAX_CANDIDATES = 3           # 컷마다 남기는 후보 수
EXTRA_SEGMENTS = 40          # 새 컷 후보를 찾으라고 첫 글자 호출에 같이 보내는 '어느 후보에도 없는 말' 수
MIN_EXTRA_CHARS = 4          # 이보다 짧은 말(공백·문장부호 제외)은 새 컷 후보로 보내지 않는다
CONFIDENCES = ("high", "medium", "low")
CAP_REASON = "사진 상한으로 AI가 보지 못함"
CUT_KEYS = ("id", "n", "section", "line", "guide", "action", "size", "sec")

TEXT_TASK = ("footage-match 규칙대로, 컷마다 candidates 중 맞는 구간을 고르고(최대 3개, 확신도와 이유), "
             "others에서 콘티에 없는 쓸 만한 장면이 있으면 suggestions로 내세요.")
IMAGE_TASK = ("footage-match 규칙대로, 첨부한 장면 모음 사진(scenes의 image 번호 순서)을 보고 컷마다 맞는 장면을 "
              "고르고(최대 3개, 확신도와 이유), 콘티에 없는 쓸 만한 장면이 있으면 suggestions로 내세요.")

_RANGE = {"fid": {"type": "string"}, "start": {"type": "number"}, "end": {"type": "number"}}
MATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "cuts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {**_RANGE, "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                                           "why": {"type": "string"}},
                            "required": ["fid", "start", "end", "confidence", "why"],
                        },
                    },
                },
                "required": ["id", "matches"],
            },
        },
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {**_RANGE, "line": {"type": "string"}, "guide": {"type": "string"},
                               "size": {"type": "string", "enum": list(SHOT_SIZES)}},
                "required": ["fid", "start", "end", "line", "guide", "size"],
            },
        },
    },
    "required": ["cuts", "suggestions"],
}

STATE_LOCK = threading.Lock()   # 맞추기 끝의 병합과 사용자 조작이 서로의 저장을 덮지 않게


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-conti" / "footage-match.md"]


# --- 저장 -----------------------------------------------------------------------------

def state_path(conti_path: str | Path) -> Path:
    return workspace_dir("footage_match", f"{Path(conti_path).stem}.json")


def _empty_state() -> dict[str, Any]:
    return {"folders": [], "cuts": {}, "suggestions": [], "images_used": 0}


def load_state(conti_path: str | Path) -> dict[str, Any]:
    """저장된 판정. 없으면 빈 상태. 깨진 파일은 ScriptError("bad_state")(덮어쓰지 않게 알린다)."""
    path = state_path(conti_path)
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ScriptError("bad_state", f"촬영 영상 판정 파일을 읽지 못했습니다: {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise ScriptError("bad_state", f"촬영 영상 판정 파일의 형식이 맞지 않습니다: {path.name}")
    state = _empty_state()
    state.update(data)
    if not isinstance(state["cuts"], dict) or not isinstance(state["suggestions"], list) \
            or not isinstance(state["folders"], list):
        raise ScriptError("bad_state", f"촬영 영상 판정 파일의 형식이 맞지 않습니다: {path.name}")
    return state


def save_state(conti_path: str | Path, state: dict[str, Any]) -> None:
    """임시 파일에 쓴 뒤 바꿔 끼운다(LF, UTF-8)."""
    path = state_path(conti_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    refstore.write_atomic(path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))


# --- 글자 후보 ------------------------------------------------------------------------

def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return min(a_end, b_end) - max(a_start, b_start)


def text_candidates(cut: dict, transcripts: dict[str, list[dict]], k: int = TEXT_K) -> list[dict]:
    """컷 대사와 받아쓴 말의 글자 유사도(문자 2-gram 자카드)로 고른 후보 k개(점수 높은 순, 같은 파일에서 겹치지 않게).

    transcripts: {fid: [{"start", "end", "text"}]}. 말 한 토막과 이어진 두 토막을 모두 본다(대사가 두 토막에 걸칠 때).
    -> [{"fid", "start", "end", "score", "text"}]. 대사가 없으면 [].
    """
    line = _bigrams(str(cut.get("line") or ""))
    if not line:
        return []
    scored: list[dict] = []
    for fid, segments in transcripts.items():
        segs = sorted((s for s in segments if _norm(str(s.get("text") or ""))), key=lambda s: s["start"])
        for i in range(len(segs)):
            for width in (1, 2):
                part = segs[i:i + width]
                if len(part) < width:
                    break
                text = " ".join(str(s["text"]).strip() for s in part)
                grams = _bigrams(text)
                score = len(line & grams) / len(line | grams)
                if score > 0:
                    scored.append({"fid": fid, "start": float(part[0]["start"]), "end": float(part[-1]["end"]),
                                   "score": round(score, 3), "text": text})
    scored.sort(key=lambda c: -c["score"])
    picked: list[dict] = []
    for c in scored:
        if any(p["fid"] == c["fid"] and _overlap(p["start"], p["end"], c["start"], c["end"]) > 0 for p in picked):
            continue
        picked.append(c)
        if len(picked) == k:
            break
    return picked


# --- Claude 응답 검증 -------------------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_footage_match", message)


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(f"{what}는 숫자여야 합니다")
    return float(value)


def _text(item: dict, key: str, what: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise _invalid(f"{what}의 {key}는 문자열이어야 합니다")
    return value.strip()


@dataclass
class _Offer:
    """한 호출에서 Claude에게 보여 준 구간들. 응답 구간은 이 안으로만 받는다."""

    cut_ids: set[str]
    ranges: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def add(self, fid: str, start: float, end: float) -> None:
        self.ranges.setdefault(fid, []).append((start, end))

    def snap(self, fid: Any, start: float, end: float) -> tuple[float, float] | None:
        """가장 많이 겹치는 보여 준 구간 안으로 자른 범위. 보여 주지 않은 파일·구간이면 None."""
        if not isinstance(fid, str) or fid not in self.ranges:
            return None
        best = max(self.ranges[fid], key=lambda r: _overlap(r[0], r[1], start, end))
        lo, hi = max(best[0], start), min(best[1], end)
        if hi <= lo:
            return None
        return lo, hi


def _parse_answer(data: dict, offer: _Offer) -> dict[str, Any]:
    """-> {"cuts": {id: [{fid,start,end,confidence,why}]}, "suggestions": [{fid,start,end,line,guide,size}]}.
    형식 오류는 invalid_*(한 번 다시 묻는다). 보여 주지 않은 컷·파일·구간은 버린다."""
    if not isinstance(data, dict) or not isinstance(data.get("cuts"), list) \
            or not isinstance(data.get("suggestions", []), list):
        raise _invalid("cuts(배열)와 suggestions(배열)가 있는 JSON 객체여야 합니다")
    cuts: dict[str, list[dict]] = {}
    for item in data["cuts"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("matches"), list):
            raise _invalid("cuts의 항목은 id(문자열)와 matches(배열)를 가져야 합니다")
        found: list[dict] = []
        for match in item["matches"]:
            if not isinstance(match, dict):
                raise _invalid("matches의 항목은 객체여야 합니다")
            if match.get("confidence") not in CONFIDENCES:
                raise _invalid(f"confidence는 {', '.join(CONFIDENCES)} 중 하나여야 합니다")
            span = offer.snap(match.get("fid"), _number(match.get("start"), "start"), _number(match.get("end"), "end"))
            why = _text(match, "why", "matches")
            if span is not None:
                found.append({"fid": match["fid"], "start": span[0], "end": span[1],
                              "confidence": match["confidence"], "why": why})
        if item["id"] in offer.cut_ids:
            cuts.setdefault(item["id"], []).extend(found)
    suggestions: list[dict] = []
    for s in data.get("suggestions", []):
        if not isinstance(s, dict):
            raise _invalid("suggestions의 항목은 객체여야 합니다")
        if s.get("size") not in SHOT_SIZES:
            raise _invalid(f"size는 {', '.join(SHOT_SIZES)} 중 하나여야 합니다")
        span = offer.snap(s.get("fid"), _number(s.get("start"), "start"), _number(s.get("end"), "end"))
        line, guide = _text(s, "line", "suggestions"), _text(s, "guide", "suggestions")
        if span is not None:
            suggestions.append({"fid": s["fid"], "start": span[0], "end": span[1], "line": line, "guide": guide,
                                "size": s["size"]})
    return {"cuts": cuts, "suggestions": suggestions}


# --- 판정 -----------------------------------------------------------------------------

def _missing_entry() -> dict[str, Any]:
    return {"kind": "missing", "choice": None, "candidates": [], "user": False}


def _ai_verdict(entry: dict[str, Any]) -> dict[str, Any]:
    """후보·이유로 AI 판정(kind, choice)을 다시 정한다. user는 False로."""
    cands = entry.get("candidates") or []
    best = next((c for c in cands if c.get("score") == "high"), None)
    if best is not None:
        kind, choice = "auto", {"fid": best["fid"], "start": best["start"], "end": best["end"]}
    else:
        kind, choice = ("review" if cands or entry.get("reason") else "missing"), None
    out = {**entry, "kind": kind, "choice": choice, "user": False}
    if kind == "auto":
        out.pop("reason", None)
    return out


def _speech(transcripts: dict[str, list[dict]], fid: str, start: float, end: float) -> str:
    return " ".join(str(s["text"]).strip() for s in transcripts.get(fid, [])
                    if _overlap(s["start"], s["end"], start, end) > 0 and str(s.get("text") or "").strip())


def _entry_from(matches: list[dict], transcripts: dict[str, list[dict]], reason: str | None) -> dict[str, Any]:
    rank = {c: i for i, c in enumerate(CONFIDENCES)}
    seen: set[tuple[str, float, float]] = set()
    cands: list[dict] = []
    for mt in sorted(matches, key=lambda x: rank[x["confidence"]]):
        key = (mt["fid"], mt["start"], mt["end"])
        if key in seen:
            continue
        seen.add(key)
        cands.append({"fid": mt["fid"], "start": mt["start"], "end": mt["end"], "score": mt["confidence"],
                      "why": mt["why"], "text": _speech(transcripts, mt["fid"], mt["start"], mt["end"])})
        if len(cands) == MAX_CANDIDATES:
            break
    entry: dict[str, Any] = {"candidates": cands}
    if reason:
        entry["reason"] = reason
    return _ai_verdict(entry)


def _cut_view(cut: dict, labels: list[str]) -> dict[str, Any]:
    section = cut.get("section")
    label = labels[section] if isinstance(section, int) and 0 <= section < len(labels) else ""
    return {"id": cut["id"], "n": cut.get("n"), "section": label, "line": cut.get("line") or "",
            "guide": cut.get("guide") or "", "action": cut.get("action") or "", "size": cut.get("size") or ""}


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


@dataclass
class _Scene:
    fid: str
    start: float
    end: float
    text: str
    image: Path


def _scene_pool(usable: list[dict], transcripts: dict[str, list[dict]]) -> list[_Scene]:
    """모음 이미지가 있는 장면, 말이 적은 장면부터(같으면 파일·시간 순)."""
    pool: list[tuple[int, int, _Scene]] = []
    for order, r in enumerate(usable):
        base = footage.footage_dir(r["fid"])
        for s in r.get("scenes") or []:
            image = base / str(s.get("montage") or "")
            if not s.get("montage") or not image.is_file():
                continue
            start, end = float(s["start"]), float(s["end"])
            text = _speech(transcripts, r["fid"], start, end)
            pool.append((len(_norm(text)), order, _Scene(r["fid"], start, end, text, image)))
    pool.sort(key=lambda p: (p[0], p[1], p[2].start))
    return [p[2] for p in pool]


def _extra_segments(transcripts: dict[str, list[dict]], taken: dict[str, list[tuple[float, float]]]) -> list[dict]:
    """어느 글자 후보에도 들지 않은 말(새 컷 후보 재료)."""
    out: list[dict] = []
    for fid, segs in transcripts.items():
        for s in segs:
            text = str(s.get("text") or "").strip()
            if len(_norm(text)) < MIN_EXTRA_CHARS:
                continue
            if any(_overlap(a, b, s["start"], s["end"]) > 0 for a, b in taken.get(fid, [])):
                continue
            out.append({"fid": fid, "start": float(s["start"]), "end": float(s["end"]), "text": text})
            if len(out) == EXTRA_SEGMENTS:
                return out
    return out


def _ask(payload: dict, task: str, offer: _Offer, files: list[Path] | None) -> dict[str, Any]:
    _, parsed = llm.ask_validated(_rules(), payload, task, lambda d: _parse_answer(d, offer),
                                  schema=MATCH_SCHEMA, files=files)
    result: dict[str, Any] = parsed
    return result


def _merge_suggestions(old: list[dict], new: list[dict], chosen: list[dict]) -> list[dict]:
    """숨긴 후보는 남기고, 새 후보는 숨긴 것·선택된 구간·서로와 겹치지 않는 것만."""
    def clash(a: dict, b: dict) -> bool:
        return a["fid"] == b["fid"] and _overlap(a["start"], a["end"], b["start"], b["end"]) > 0

    out = [s for s in old if s.get("hidden")]
    for s in new:
        if any(clash(s, c) for c in chosen) or any(clash(s, o) for o in out):
            continue
        out.append({**s, "hidden": False})
    return out


def match(conti_path: str | Path, files_results: list[dict], now: float | None = None) -> dict[str, Any]:
    """콘티 컷을 처리한 파일들(footage.process_file 결과)과 맞춰 판정을 저장하고 새 상태를 돌려준다.

    사용자 판정 컷(user: true)은 묻지도 바꾸지도 않는다(맞추는 도중에 사용자가 고른 것도). 처리 가능한
    파일이 없으면 Claude를 부르지 않고 모두 missing.
    """
    path = Path(conti_path)
    fm = conti._load_conti(path)
    labels = [str(s.get("label") or "") for s in fm.get("script") or []]
    before = load_state(path)
    todo = [c for c in fm["cuts"] if not (before["cuts"].get(c["id"]) or {}).get("user")]
    usable = [r for r in files_results if r.get("status") == "done" and footage.valid_fid(r.get("fid"))]
    transcripts = {r["fid"]: sorted(r["transcript"], key=lambda s: s["start"])
                   for r in usable if r.get("transcript")}

    matches: dict[str, list[dict]] = {c["id"]: [] for c in todo}
    suggestions: list[dict] = []
    text_cuts: list[tuple[dict, list[dict]]] = []
    image_cuts: list[dict] = []
    for c in todo if usable else []:
        cands = text_candidates(c, transcripts)
        if cands:
            text_cuts.append((c, cands))
        else:
            image_cuts.append(c)

    taken: dict[str, list[tuple[float, float]]] = {}
    for _, cands in text_cuts:
        for cd in cands:
            taken.setdefault(cd["fid"], []).append((cd["start"], cd["end"]))
    extras = _extra_segments(transcripts, taken)
    for group in _chunks(text_cuts, TEXT_CUTS_PER_CALL):
        offer = _Offer({c["id"] for c, _ in group})
        cuts_payload = []
        for c, cands in group:
            for cd in cands:
                offer.add(cd["fid"], cd["start"], cd["end"])
            cuts_payload.append({**_cut_view(c, labels), "candidates": [
                {k: cd[k] for k in ("fid", "start", "end", "text")} for cd in cands]})
        others, extras = extras, []          # 말 후보는 첫 호출에만
        for o in others:
            offer.add(o["fid"], o["start"], o["end"])
        parsed = _ask({"mode": "text", "cuts": cuts_payload, "others": others}, TEXT_TASK, offer, None)
        for cid, found in parsed["cuts"].items():
            matches[cid].extend(found)
        suggestions.extend(parsed["suggestions"])

    images_used = 0
    unseen = False
    if image_cuts:
        pool = _scene_pool(usable, transcripts)
        shown = min(len(pool), IMAGES_PER_RUN)
        unseen = len(pool) > shown
        cuts_payload = [_cut_view(c, labels) for c in image_cuts]
        for chunk in _chunks(pool[:shown], IMAGES_PER_CALL):
            offer = _Offer({c["id"] for c in image_cuts})
            for s in chunk:
                offer.add(s.fid, s.start, s.end)
            scenes = [{"image": i + 1, "fid": s.fid, "start": s.start, "end": s.end, "text": s.text}
                      for i, s in enumerate(chunk)]
            parsed = _ask({"mode": "images", "cuts": cuts_payload, "scenes": scenes}, IMAGE_TASK, offer,
                          [s.image for s in chunk])
            images_used += len(chunk)
            for cid, found in parsed["cuts"].items():
                matches[cid].extend(found)
            suggestions.extend(parsed["suggestions"])

    image_ids = {c["id"] for c in image_cuts}
    entries = {cid: _entry_from(found, transcripts, CAP_REASON if unseen and cid in image_ids else None)
               for cid, found in matches.items()}
    for s in suggestions:
        s["text"] = _speech(transcripts, s["fid"], s["start"], s["end"])

    with STATE_LOCK:
        state = load_state(path)      # 맞추는 동안 사용자가 바꾼 것을 덮지 않게 다시 읽는다
        for cid, entry in entries.items():
            if not (state["cuts"].get(cid) or {}).get("user"):
                state["cuts"][cid] = entry
        chosen = [{"fid": e["choice"]["fid"], "start": e["choice"]["start"], "end": e["choice"]["end"]}
                  for e in state["cuts"].values() if e.get("choice")]
        state["suggestions"] = _merge_suggestions(state["suggestions"], suggestions, chosen)
        state["images_used"] = images_used
        state["matched_at"] = time.time() if now is None else now
        save_state(path, state)
    return state


# --- 사용자 조작 ------------------------------------------------------------------------

def _require_cut(conti_path: Path, cut_id: str) -> None:
    if not any(c["id"] == cut_id for c in conti._load_conti(conti_path)["cuts"]):
        raise ScriptError("cut_not_found", f"컷 {cut_id}이(가) 없습니다")


def _span(start: Any, end: Any) -> tuple[float, float]:
    values = []
    for v in (start, end):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ScriptError("bad_request", "구간 시각은 숫자여야 합니다")
        values.append(float(v))
    if not 0 <= values[0] < values[1]:
        raise ScriptError("bad_request", "구간은 0 이상이고 시작이 끝보다 앞서야 합니다")
    return values[0], values[1]


def _update_cut(conti_path: str | Path, cut_id: str, change: Any) -> dict[str, Any]:
    path = Path(conti_path)
    _require_cut(path, cut_id)
    with STATE_LOCK:
        state = load_state(path)
        entry: dict[str, Any] = change(state["cuts"].get(cut_id) or _missing_entry())
        state["cuts"][cut_id] = entry
        save_state(path, state)
    return entry


def choose(conti_path: str | Path, cut_id: str, fid: str, start: float, end: float) -> dict[str, Any]:
    """사용자가 고른 구간으로 고정(pinned). 다시 맞춰도 바뀌지 않는다."""
    if not footage.valid_fid(fid):
        raise ScriptError("bad_fid", "잘못된 파일 id입니다")
    lo, hi = _span(start, end)
    return _update_cut(conti_path, cut_id, lambda e: {**e, "kind": "pinned", "user": True,
                                                       "choice": {"fid": fid, "start": lo, "end": hi}})


def mark_not_filmed(conti_path: str | Path, cut_id: str) -> dict[str, Any]:
    """못 찍음으로 고정(not_filmed). 후보는 남겨 둔다(reset으로 돌아갈 수 있게)."""
    return _update_cut(conti_path, cut_id, lambda e: {**e, "kind": "not_filmed", "choice": None, "user": True})


def reset(conti_path: str | Path, cut_id: str) -> dict[str, Any]:
    """사용자 고정을 풀고 남아 있는 AI 후보로 판정을 되돌린다."""
    return _update_cut(conti_path, cut_id, _ai_verdict)


def hide_suggestion(conti_path: str | Path, idx: int) -> dict[str, Any]:
    """새 컷 후보를 숨긴다(다시 맞춰도 나오지 않는다). -> 숨긴 후보."""
    with STATE_LOCK:
        state = load_state(conti_path)
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(state["suggestions"]):
            raise ScriptError("bad_request", "새 컷 후보 번호가 올바르지 않습니다")
        state["suggestions"][idx] = {**state["suggestions"][idx], "hidden": True}
        save_state(conti_path, state)
        hidden: dict[str, Any] = state["suggestions"][idx]
    return hidden


def visible_cuts(conti_path: str | Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """지금 콘티에 있는 컷만(콘티 순서), 컷 필드 + "match"(판정, 없으면 missing)."""
    cuts = conti._load_conti(Path(conti_path))["cuts"]
    return [{**{k: c.get(k) for k in CUT_KEYS}, "match": state["cuts"].get(c["id"]) or _missing_entry()}
            for c in cuts]
