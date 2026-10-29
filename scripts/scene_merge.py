"""반복 컷을 장면 하나로 합치기: 묶기(순수 함수) → 계획 → Claude 대사 나누기 검증 → 적용(이력·촬영 영상 판정).

- 묶기: 같은 섹션 안에서 정규화한 action이 같은 컷(연속 아니어도)이 한 장면. 장면 id = 첫 컷 id.
- 계획(`plan`): 섹션별 이어진 대사(`text`)와 장면 목록. 반복이 있는 섹션(`split: true`)만 Claude가 장면 순서와
  대사 나눌 자리를 정한다(`split_request` → skills/yt-conti/merge.md). 반복이 없는 섹션은 컷 그대로(대사도 그대로).
- 검증(`validate_split`): 순서는 그 섹션 장면 id의 순열, 장면 수만큼 대사, 이으면 원래 글과 같다(공백만 무시,
  문장부호도 그대로).
- 적용(`apply`): 해시 확인 → 이력 `before_merge` → 콘티 md·html 쓰기(본문 표는 다시 그리고 `## 내 메모`는 그대로)
  → 이력 `auto` → 촬영 영상 판정의 사용자 판정만 남는 장면 id로 옮긴다(파일이 있을 때만).
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from scripts import conti
from scripts.common import ScriptError
from scripts.conti import _norm

NOTHING_MESSAGE = "합칠 장면이 없어요"
CUT_FIELDS = ("id", "section", "size", "angle", "sec", "action", "line", "caption", "sfx", "guide", "ref_frame")
SCENE_BRIEF = ("id", "action", "guide", "size", "sec")


def groups(cuts: list[dict[str, Any]]) -> list[list[int]]:
    """섹션별로 정규화한 action이 같은 컷 번호(n) 묶음. 첫 등장 순서, 단독 컷도 한 묶음."""
    order: list[tuple[Any, str]] = []
    found: dict[tuple[Any, str], list[int]] = {}
    for cut in cuts:
        key = (cut["section"], _norm(cut.get("action", "")))
        if key not in found:
            found[key] = []
            order.append(key)
        found[key].append(cut["n"])
    return [found[key] for key in order]


def has_repeats(cuts: list[dict[str, Any]]) -> bool:
    return any(len(g) > 1 for g in groups(cuts))


def _join_unique(members: list[dict[str, Any]], key: str) -> str:
    seen: list[str] = []
    for member in members:
        value = member.get(key) or ""
        if value and value not in seen:
            seen.append(value)
    return " / ".join(seen)


def merge_fields(members: list[dict[str, Any]]) -> dict[str, Any]:
    """묶음의 대표 컷. line은 비워 두고(호출자가 채움) merged_ids에 모든 id를 담는다."""
    first = members[0]
    merged: dict[str, Any] = {
        "id": first["id"], "section": first["section"], "size": first.get("size"),
        "angle": first.get("angle"), "action": first.get("action"),
        "sec": sum(float(m.get("sec") or 0) for m in members),
        "guide": _join_unique(members, "guide"), "caption": _join_unique(members, "caption"),
        "sfx": _join_unique(members, "sfx"), "line": "",
        "merged_ids": [m["id"] for m in members],
    }
    for member in members:
        if member.get("ref_frame"):
            merged["ref_frame"] = member["ref_frame"]
            break
    return merged


def joined_lines(cuts: list[dict[str, Any]], section: int) -> str:
    return " ".join(c["line"] for c in cuts if c["section"] == section and c.get("line"))


def transfer_footage(state: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """사라질 id의 첫 사용자 판정을 남는 id로 복사하고 AI 판정은 양쪽 모두 지운다. 새 dict를 돌려준다.
    사라질 id의 사용자 판정은 그대로 둔다(화면은 콘티에 없는 id를 보지 않고, '합치기 전'으로 되돌리면 다시 보인다)."""
    out = copy.deepcopy(state)
    cuts: dict[str, Any] = out.setdefault("cuts", {})
    moved: dict[str, Any] = {}
    for gone, keep in id_map.items():
        entry = cuts.get(gone)
        if not (entry and entry.get("user")):
            cuts.pop(gone, None)
        elif keep not in moved:
            moved[keep] = copy.deepcopy(entry)
    for keep in set(id_map.values()):
        if not (cuts.get(keep) or {}).get("user"):
            cuts.pop(keep, None)
            if keep in moved:
                cuts[keep] = moved[keep]
    return out


# --- 계획 · 검증 (id 없는 옛 파일에 id를 붙이는 것 말고는 읽기만) -----------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_merge", f"합친 결과가 맞지 않습니다: {message}")


def _bare(text: str) -> str:
    """공백만 뺀 글. 대사 나누기는 나눈 자리의 띄어쓰기만 바뀔 수 있다(글자·문장부호는 그대로)."""
    return re.sub(r"\s+", "", text)


def _first_ids(cuts: list[dict[str, Any]]) -> dict[str, str]:
    """컷 id → 그 컷이 들어가는 장면의 id(묶음 첫 컷 id)."""
    by_n = {c["n"]: c["id"] for c in cuts}
    return {by_n[n]: by_n[group[0]] for group in groups(cuts) for n in group}


def id_map(cuts: list[dict[str, Any]]) -> dict[str, str]:
    """사라지는 컷 id → 남는 장면 id. 컷 순서대로(촬영 영상 판정은 앞선 사용자 판정이 남는다)."""
    first = _first_ids(cuts)
    return {c["id"]: first[c["id"]] for c in cuts if first[c["id"]] != c["id"]}


def _sections(fm: dict[str, Any]) -> list[dict[str, Any]]:
    """섹션별 장면. 여러 컷이 묶인 장면은 merge_fields(line은 비움), 혼자인 컷은 그 컷 그대로(line 포함).
    반복이 있는 섹션은 split=True(장면 순서·대사를 다시 정한다)."""
    cuts = fm["cuts"]
    by_n = {c["n"]: c for c in cuts}
    out = [{"idx": i, "label": s["label"], "text": joined_lines(cuts, i), "split": False, "scenes": []}
           for i, s in enumerate(fm["script"])]
    for group in groups(cuts):
        members = [by_n[n] for n in group]
        section = out[members[0]["section"]]
        if len(members) > 1:
            section["split"] = True
            section["scenes"].append(merge_fields(members))
        else:
            alone = {k: members[0][k] for k in CUT_FIELDS if k in members[0]}
            section["scenes"].append({**alone, "line": (members[0].get("line") or "").strip(), "merged_ids": [alone["id"]]})
    return out


def _long_scenes(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "scene_too_long", "id": scene["id"], "section": s["idx"], "sec": scene["sec"],
             "message": f"합친 장면 {scene['action']} {scene['sec']:g}초 — 화면에서 고칠 수 있는 길이는 "
                        f"{conti.MAX_CUT_SEC:g}초까지예요"}
            for s in sections for scene in s["scenes"]
            if len(scene["merged_ids"]) > 1 and float(scene["sec"]) > conti.MAX_CUT_SEC]


def plan(conti_path: str | Path) -> dict[str, Any]:
    """합치기 계획. {path, hash, before, after, sections: [{idx, label, text, split, scenes}], warnings}.
    id가 없던 옛 콘티는 먼저 id를 붙여 저장한다(적용할 때 같은 id로 장면을 찾도록)."""
    path = conti._resolve(conti_path)
    with conti.HISTORY_LOCK:
        current, fm, _ = conti.ensure_ids_file(conti.read_conti(path))
    if not has_repeats(fm["cuts"]):
        raise ScriptError("nothing_to_merge", NOTHING_MESSAGE)
    sections = _sections(fm)
    return {"path": conti._rel(path), "hash": conti.bytes_hash(current.raw), "before": len(fm["cuts"]),
            "after": sum(len(s["scenes"]) for s in sections), "sections": sections, "warnings": _long_scenes(sections)}


def split_request(merge_plan: dict[str, Any]) -> dict[str, Any]:
    """Claude에게 보낼 입력: 반복이 있던 섹션만, 장면은 id·action·guide·size·sec만."""
    return {"sections": [{"idx": s["idx"], "label": s["label"], "text": s["text"],
                          "scenes": [{k: scene.get(k) for k in SCENE_BRIEF} for scene in s["scenes"]]}
                         for s in merge_plan["sections"] if s["split"]]}


def _answers(merge_plan: dict[str, Any], data: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise _invalid("sections 목록이 없습니다")
    todo = {s["idx"] for s in merge_plan["sections"] if s["split"]}
    answers: dict[int, dict[str, Any]] = {}
    for entry in data["sections"]:
        idx = entry.get("idx") if isinstance(entry, dict) else None
        if not isinstance(idx, int) or isinstance(idx, bool) or idx not in todo:
            raise _invalid(f"나눌 섹션이 아닙니다: {idx!r}")
        if idx in answers:
            raise _invalid(f"섹션 {idx}이(가) 두 번 나옵니다")
        answers[idx] = entry
    missing = sorted(todo - set(answers))
    if missing:
        raise _invalid(f"섹션 {', '.join(map(str, missing))}이(가) 빠졌습니다")
    return answers


def _ordered(section: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = {scene["id"]: scene for scene in section["scenes"]}
    order, lines = entry.get("order"), entry.get("lines")
    idx = section["idx"]
    if (not isinstance(order, list) or not all(isinstance(x, str) for x in order)
            or len(order) != len(scenes) or set(order) != set(scenes)):
        raise _invalid(f"섹션 {idx}의 order는 장면 id {len(scenes)}개를 한 번씩 담아야 합니다")
    if not isinstance(lines, list) or len(lines) != len(scenes) or not all(isinstance(x, str) for x in lines):
        raise _invalid(f"섹션 {idx}의 lines는 장면 수({len(scenes)})만큼의 글이어야 합니다")
    if _bare("".join(lines)) != _bare(section["text"]):
        raise _invalid(f"섹션 {idx}의 대사를 이으면 원래 대사와 다릅니다(글자·문장부호를 바꾸지 말고 나누기만 하세요)")
    return [{**scenes[scene_id], "line": line.strip()} for scene_id, line in zip(order, lines, strict=True)]


def validate_split(merge_plan: dict[str, Any], data: Any) -> list[dict[str, Any]]:
    """Claude 출력 {"sections": [{"idx", "order": [장면 id], "lines": [글]}]} → 새 컷 목록(n 다시 매김)."""
    answers = _answers(merge_plan, data)
    scenes: list[dict[str, Any]] = []
    for section in merge_plan["sections"]:
        scenes += _ordered(section, answers[section["idx"]]) if section["split"] else section["scenes"]
    return [{"n": n, **{k: v for k, v in scene.items() if k != "merged_ids"}} for n, scene in enumerate(scenes, 1)]


# --- 적용 (이력 · 콘티 md · 촬영 영상 판정) ------------------------------------------------------

def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _checked_cuts(before: dict[str, Any], cuts: Any) -> list[dict[str, Any]]:
    """화면·CLI가 보낸 합친 컷 목록에서 **섹션 안 장면 순서와 대사(line)만** 받는다. 나머지 필드(길이·크기·가이드·
    고정 사진 등)는 지금 파일에서 다시 만든다. 반복이 있던 섹션은 대사를 이으면 원래와 같아야 하고(공백만 무시),
    반복이 없던 섹션은 순서·대사가 파일과 똑같아야 한다."""
    if not isinstance(cuts, list) or not cuts or not all(isinstance(c, dict) for c in cuts):
        raise _invalid("cuts는 컷 목록이어야 합니다")
    sections = _sections(before)
    scenes = {scene["id"]: (s, scene) for s in sections for scene in s["scenes"]}
    ids = [c.get("id") for c in cuts]
    if not all(isinstance(i, str) for i in ids) or len(ids) != len(scenes) or set(ids) != set(scenes):
        raise _invalid("컷 id가 합친 장면과 맞지 않습니다. 미리보기를 다시 만드세요")
    chosen: dict[int, list[tuple[dict[str, Any], str]]] = {s["idx"]: [] for s in sections}
    for cut in cuts:
        section, scene = scenes[cut["id"]]
        if not _int(cut.get("section")) or cut["section"] != section["idx"]:
            raise _invalid(f"장면 {cut['id']}의 섹션이 바뀌었습니다")
        if not isinstance(cut.get("line"), str):
            raise _invalid(f"장면 {cut['id']}의 대사가 글이 아닙니다")
        chosen[section["idx"]].append((scene, cut["line"].strip()))
    out: list[dict[str, Any]] = []
    for section in sections:
        picked = chosen[section["idx"]]
        if section["split"]:
            if _bare("".join(line for _, line in picked)) != _bare(section["text"]):
                raise _invalid(f"섹션 {section['idx']}의 대사를 이으면 원래 대사와 다릅니다")
        elif [(scene["id"], line) for scene, line in picked] != [(sc["id"], sc["line"]) for sc in section["scenes"]]:
            raise _invalid(f"섹션 {section['idx']}은(는) 합칠 것이 없어 순서·대사를 바꿀 수 없습니다")
        out += [{**{k: v for k, v in scene.items() if k != "merged_ids"}, "line": line} for scene, line in picked]
    return out


def apply_fm(conti_path: str | Path, cuts: Any, expected_hash: str, now: float | None = None
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """apply와 같고 쓴 fm(검증·시간 계산 끝)도 돌려준다(화면 응답을 파일을 다시 읽지 않고 만들 때)."""
    from scripts import conti_history, footage_match   # 둘 다 conti를 쓰므로 함수 안에서 불러온다

    path = conti._resolve(conti_path)
    with conti.HISTORY_LOCK:
        cf = conti.read_conti(path)
        if conti.bytes_hash(cf.raw) != expected_hash:
            raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
        current, before, _ = conti.ensure_ids_file(cf)
        if not has_repeats(before["cuts"]):
            raise ScriptError("nothing_to_merge", NOTHING_MESSAGE)
        merged = _checked_cuts(before, cuts)
        by_n = {c["n"]: c for c in before["cuts"]}
        repeated = {by_n[g[0]]["section"] for g in groups(before["cuts"]) if len(g) > 1}
        fm = conti.retime({**before, "cuts": merged}, repeated)     # 합친 섹션만 대사를 이어 다시 만든다
        moves = id_map(before["cuts"])
        warnings = []
        with footage_match.STATE_LOCK:
            has_state = footage_match.state_path(path).is_file()
            state = footage_match.load_state(path) if has_state else None   # 깨진 판정 파일이면 쓰기 전에 멈춘다
            conti_history.record(path, before, "before_merge", now=now)
            written, html_path = conti._write_conti_file(current, fm)
            conti_history.record(path, fm, "auto", now=now)
            if state is not None:
                try:
                    footage_match.save_state(path, transfer_footage(state, moves))
                except OSError as e:                 # 콘티는 이미 합쳤다: 실패를 알리고 판정은 다음 맞추기에 맡긴다
                    warnings.append({"kind": "footage_not_transferred",
                                     "message": f"촬영 영상 판정을 옮기지 못했어요({e}). 촬영 영상을 다시 맞춰 주세요"})
    result = {"status": "merged", "path": conti._rel(path), "html": conti._rel(html_path), "abs_html": str(html_path),
              "hash": conti.bytes_hash(written.raw), "before": len(before["cuts"]), "after": len(fm["cuts"]),
              "warnings": [*conti.warnings(fm["script"], fm["cuts"], fm["speech_cps"]), *warnings]}
    return result, fm


def apply(conti_path: str | Path, cuts: Any, expected_hash: str, now: float | None = None) -> dict[str, Any]:
    """합친 컷 목록을 저장한다. 미리보기 뒤 파일이 바뀌었으면 conti_changed."""
    return apply_fm(conti_path, cuts, expected_hash, now)[0]


def run_cli(conti_path: str, cuts_file: str | None, expected_hash: str | None) -> dict[str, Any]:
    """`conti merge`: 인자가 콘티뿐이면 계획 JSON, --cuts·--hash면 저장.
    --cuts 파일은 합친 컷 목록(또는 {"cuts": [...]}) 또는 Claude 출력({"sections": [...]}, 검증 뒤 저장)."""
    if cuts_file is None and expected_hash is None:
        return plan(conti_path)
    if cuts_file is None or not expected_hash:
        raise ScriptError("bad_request", "--cuts와 --hash는 함께 줘야 합니다")
    try:
        data = json.loads(conti._resolve(cuts_file).read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ScriptError("invalid_merge", f"합친 컷 JSON을 읽을 수 없습니다: {e}") from e
    if isinstance(data, dict) and "sections" in data:
        data = validate_split(plan(conti_path), data)
    elif isinstance(data, dict):
        data = data.get("cuts")
    return apply(conti_path, data, expected_hash)
