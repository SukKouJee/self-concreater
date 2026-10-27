"""콘티 이력: 한 줄짜리 버전 목록(자동 묶음 · 이름 · md 직접 수정 감지), 비교, 되돌리기.

저장: workspace/conti_history/<콘티 파일 stem>/NNNN.json (4자리, 0001부터). 모든 쓰기는 임시 파일 + 교체(원자적).
버전 = {"n", "kind", "name", "at", "last_touch", "state", "summary"}.
state = {"script": [{label, text, start, end}], "cuts": [{id, section, size, angle, sec, action, line, caption, sfx,
guide, (ref_frame)}], "target_sec"}. n·start·end 같은 파생 값과 본문·메모는 넣지 않는다(프런트매터가 유일한 입력).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import conti, refstore
from scripts.common import ScriptError, workspace_dir

KINDS = ("created", "auto", "named", "before_recreate", "external", "restored")
GROUP_SEC = 300                      # 마지막 손길에서 5분 안의 편집은 같은 auto 버전에 묶는다
NAME_MAX = 30
CUT_FIELDS = ("section", "size", "angle", "sec", "action", "line", "caption", "sfx", "guide", "ref_frame")
_VERSION_RE = re.compile(r"^(\d{4})\.json$")


# --- 상태 ---------------------------------------------------------------------------

def history_dir(conti_path: str | Path) -> Path:
    return workspace_dir("conti_history", Path(conti_path).stem)


def _num(value: Any) -> float:
    return round(float(value), 2)


def state_of(fm: dict) -> dict:
    """프런트매터 → 이력 상태(사용자가 바꿀 수 있는 값만)."""
    cuts = []
    for c in fm["cuts"]:
        cut: dict[str, Any] = {"id": c.get("id"), "section": c["section"], "size": c["size"], "angle": c.get("angle") or None,
                               "sec": _num(c["sec"]), **{f: c.get(f) or "" for f in ("action", "line", "caption", "sfx", "guide")}}
        if c.get("ref_frame"):
            cut["ref_frame"] = c["ref_frame"]
        cuts.append(cut)
    script = [{"label": s["label"], "text": s.get("text") or "", "start": _num(s["start"]), "end": _num(s["end"])}
              for s in fm["script"]]
    return {"script": script, "cuts": cuts, "target_sec": _num(fm["target_sec"])}


def _clean(cut: dict) -> dict:
    out = {f: cut.get(f) for f in CUT_FIELDS}
    out["ref_frame"] = out["ref_frame"] or None
    out["angle"] = out["angle"] or None
    out["sec"] = _num(cut["sec"])
    return out


def same_state(a: dict, b: dict) -> bool:
    """컷 id와 파생 값(n·start·end)을 빼고 사용자가 바꾼 값이 같은지."""
    def key(state: dict) -> dict:
        return {"script": [{"label": s["label"], "text": s["text"], "start": _num(s["start"]), "end": _num(s["end"])}
                           for s in state["script"]],
                "cuts": [_clean(c) for c in state["cuts"]], "target_sec": _num(state["target_sec"])}
    return key(a) == key(b)


def _total(state: dict) -> float:
    return round(sum(_num(c["sec"]) for c in state["cuts"]), 2)


# --- 비교 ---------------------------------------------------------------------------

def _lis_members(order: list[int]) -> set[int]:
    """가장 긴 증가 부분 수열에 속한 값들(제자리에 있는 컷). 나머지는 옮겨진 컷."""
    best: list[list[int]] = []
    for i, v in enumerate(order):
        chain = max((best[j] for j in range(i) if order[j] < v), key=len, default=[])
        best.append([*chain, v])
    return set(max(best, key=len, default=[]))


def diff(a_state: dict, b_state: dict) -> dict:
    """a → b에서 바뀐 점. id가 모두 있으면 id로, 아니면 위치로 컷을 맞춘다."""
    a_cuts = [{**c, "n": i} for i, c in enumerate(a_state["cuts"], 1)]
    b_cuts = [{**c, "n": i} for i, c in enumerate(b_state["cuts"], 1)]
    by_id = all(c.get("id") for c in a_cuts + b_cuts)
    changes: list[dict] = []

    def entry(kind: str, a: dict | None, b: dict | None, field: str | None = None, old: Any = None, new: Any = None) -> dict:
        ref = b or a
        return {"kind": kind, "cut": ref.get("id") if ref and by_id else None, "n_from": a["n"] if a else None,
                "n_to": b["n"] if b else None, "field": field, "from": old, "to": new}

    pairs: list[tuple[dict, dict]] = []
    if by_id:
        b_by_id = {c["id"]: c for c in b_cuts}
        a_ids = {c["id"] for c in a_cuts}
        for a in a_cuts:
            if a["id"] in b_by_id:
                pairs.append((a, b_by_id[a["id"]]))
            else:
                changes.append(entry("removed", a, None))
        changes += [entry("added", None, b) for b in b_cuts if b["id"] not in a_ids]
        rank_in_b = {b["id"]: i for i, (_, b) in enumerate(sorted(pairs, key=lambda p: p[1]["n"]))}
        in_place = _lis_members([rank_in_b[a["id"]] for a, _ in pairs])
        for a, b in pairs:
            if rank_in_b[a["id"]] not in in_place:
                changes.append(entry("moved", a, b))
    else:
        pairs = list(zip(a_cuts, b_cuts))
        changes += [entry("removed", a, None) for a in a_cuts[len(b_cuts):]]
        changes += [entry("added", None, b) for b in b_cuts[len(a_cuts):]]
    for a, b in pairs:
        for field in CUT_FIELDS:
            if _clean(a)[field] != _clean(b)[field]:
                changes.append(entry("changed", a, b, field, a.get(field), b.get(field)))
    ta, tb = _total(a_state), _total(b_state)
    if ta != tb:
        changes.append({"kind": "total", "cut": None, "n_from": None, "n_to": None, "field": "total_sec", "from": ta, "to": tb})
    for i, (sa, sb) in enumerate(zip(a_state["script"], b_state["script"])):
        if (sa["text"], _num(sa["start"]), _num(sa["end"])) != (sb["text"], _num(sb["start"]), _num(sb["end"])):
            changes.append({"kind": "changed", "cut": None, "n_from": None, "n_to": None, "field": "script", "section": i,
                            "from": sa["text"], "to": sb["text"]})
    return {"changes": changes, "left": a_cuts, "right": b_cuts}


def _changed_cuts(result: dict) -> int:
    return len({(c["n_from"], c["n_to"]) for c in result["changes"] if c["kind"] in ("changed", "added", "removed", "moved")
                and c["field"] != "script"})


# --- 저장소 -------------------------------------------------------------------------

def _paths(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if _VERSION_RE.match(p.name))


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("history_corrupt", f"이력 파일을 읽을 수 없습니다: {path.name} ({e})") from e


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _summary(version: dict) -> dict:
    return {"n": version["n"], "kind": version["kind"], "name": version["name"], "at": version["at"],
            "summary": version["summary"]}


def record(conti_path: str | Path, fm: dict, kind: str, name: str | None = None, now: float | None = None) -> dict:
    """지금 상태를 이력에 남긴다. 최신이 auto이고 5분 안이면 auto는 그 버전을 덮어쓴다. 버전 요약을 돌려준다."""
    if kind not in KINDS:
        raise ScriptError("bad_request", f"알 수 없는 이력 종류입니다: {kind}")
    stamp = time.time() if now is None else now
    folder = history_dir(conti_path)
    folder.mkdir(parents=True, exist_ok=True)
    paths = _paths(folder)
    latest = _read(paths[-1]) if paths else None
    state = state_of(fm)
    at = datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
    if kind == "auto" and latest and latest["kind"] == "auto" and stamp - latest["last_touch"] <= GROUP_SEC:
        n, at = latest["n"], latest["at"]
        before = _read(paths[-2]) if len(paths) > 1 else None
    else:
        n, before = (latest["n"] + 1 if latest else 1), latest
    changed = _changed_cuts(diff(before["state"], state)) if before else 0
    version = {"n": n, "kind": kind, "name": name, "at": at, "last_touch": stamp, "state": state,
               "summary": {"cuts": len(state["cuts"]), "total_sec": _total(state), "changed": changed}}
    _write_atomic(folder / f"{n:04d}.json", version)
    return _summary(version)


def list_versions(conti_path: str | Path) -> list[dict]:
    """최신순 버전 목록(state 없이)."""
    return [_summary(_read(p)) for p in reversed(_paths(history_dir(conti_path)))]


def get_version(conti_path: str | Path, n: int) -> dict:
    path = history_dir(conti_path) / f"{int(n):04d}.json"
    if not path.is_file():
        raise ScriptError("version_not_found", f"이력 {n}번이 없습니다")
    return _read(path)


def check_external(conti_path: str | Path, fm: dict, now: float | None = None) -> dict | None:
    """파일 상태가 최신 버전과 다르면 external로 남긴다. 이력이 없으면 created를 만들고 None."""
    versions = _paths(history_dir(conti_path))
    if not versions:
        record(conti_path, fm, "created", now=now)
        return None
    if same_state(_read(versions[-1])["state"], state_of(fm)):
        return None
    return record(conti_path, fm, "external", now=now)


# --- 이름 · 되돌리기 -------------------------------------------------------------------

def _guard(path: Path, expected_hash: str | None) -> None:
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {conti._rel(path)}")
    if expected_hash is not None and conti.content_hash(path) != expected_hash:
        raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")


def name(conti_path: str | Path, label: str, expected_hash: str | None, now: float | None = None) -> dict:
    path = conti._resolve(conti_path)
    _guard(path, expected_hash)
    text = str(label or "").strip()
    if not 1 <= len(text) <= NAME_MAX:
        raise ScriptError("invalid_name", f"이름은 1~{NAME_MAX}자여야 합니다")
    return record(path, conti._load_conti(path), "named", name=text, now=now)


def restore(conti_path: str | Path, n: int, expected_hash: str | None, now: float | None = None) -> dict:
    """그 버전의 상태로 프런트매터를 쓰고(메모·줄바꿈 보존, 시간 다시 계산, HTML) restored로 남긴다."""
    path = conti._resolve(conti_path)
    _guard(path, expected_hash)
    version = get_version(path, n)
    raw = path.read_bytes()
    state = version["state"]
    fm = conti.retime({**conti._load_conti(path), "script": [dict(s) for s in state["script"]],
                       "cuts": [dict(c) for c in state["cuts"]], "target_sec": state["target_sec"]})
    html_path = conti._write_conti(path, raw, fm)
    label = version["name"] or datetime.fromisoformat(version["at"]).strftime("%Y-%m-%d %H:%M")
    recorded = record(path, fm, "restored", name=f"되돌림: {label}", now=now)
    return {"status": "restored", "version": recorded, "path": conti._rel(path), "html": conti._rel(html_path),
            "hash": conti.content_hash(path)}


def current_state(conti_path: str | Path) -> dict:
    """파일의 지금 상태(비교용)."""
    return state_of(conti._load_conti(conti._resolve(conti_path)))


def diff_versions(conti_path: str | Path, a: int, b: int | None = None) -> dict:
    """a번 버전 → b번 버전(생략하면 지금 파일)."""
    left = get_version(conti_path, a)["state"]
    right = get_version(conti_path, b)["state"] if b is not None else current_state(conti_path)
    return diff(left, right)


def baseline(conti_path: str | Path, now: float | None = None) -> dict | None:
    """불러올 때: id를 먼저 붙이고(있으면 그대로) 이력 기준선·external을 확인한다."""
    path = conti._resolve(conti_path)
    conti.ensure_ids(path)
    fm, _ = refstore.read_md(path)
    if not fm.get("cuts") or not fm.get("script"):
        raise ScriptError("invalid_conti", "콘티 프런트매터에 script·cuts가 없습니다")
    return check_external(path, conti._load_conti(path), now)
