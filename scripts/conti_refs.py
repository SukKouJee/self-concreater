"""장면에 사진 고르기: 레퍼런스 프레임의 한 줄 설명(desc)과 콘티 장면을 글로만 맞춰 컷에 `ref_auto`로 저장한다.

- 후보(`candidates`): 같은 카테고리 ref 영상의 대표 프레임 중 설명이 있고 파일이 있는 것만(`conti.frame_pool`).
- 고르기(`pick`): Claude에 장면(id, action, guide, size)과 후보 목록을 주고 장면마다 프레임 하나 또는 null을 받는다
  (skills/yt-conti/pick-refs.md, 이미지 없음). 장면은 PICK_BATCH개씩 나눠 부르고, 앞 묶음에서 쓴 사진을 알려 준다.
  후보에 없는 프레임·없는 장면 id는 버린다.
- 적용(`apply_picks`, `save_picks`): 고른 장면은 `ref_auto = {"frame", "match": "content"}`, 못 고른 장면은 지운다.
  렌더할 때는 저장된 값만 쓴다(`conti.cut_refs`, Claude 안 부름). 사용자가 고정한 사진(ref_frame)이 늘 먼저다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from scripts import claude_cli, conti, llm
from scripts.common import ScriptError

PICK_BATCH = 40
PICK_TASK = ("pick-refs 규칙대로 장면마다 내용이 가장 잘 맞는 레퍼런스 사진 하나(맞는 게 없으면 null)를 고른 "
             "JSON을 만드세요.")
SCENE_FIELDS = ("id", "action", "guide", "size")


def rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-conti" / "pick-refs.md"]


def candidates(category: str | None) -> list[dict[str, Any]]:
    """설명(desc)이 있는 같은 카테고리 레퍼런스 프레임 {frame, desc, size, angle}. 프레임 경로가 겹치면 하나만."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in conti.frame_pool(category):
        if not p.get("desc") or p["frame"] in seen:
            continue
        seen.add(p["frame"])
        out.append({"frame": p["frame"], "desc": p["desc"], "size": p.get("size"), "angle": p.get("angle")})
    return out


def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_refs", f"사진 고르기 결과가 형식에 맞지 않습니다: {message}")


def _validated(data: Any, ids: set[str], known: set[str]) -> dict[str, str]:
    """{"picks": [{"id", "frame"}]} → 컷 id → frame. 형식이 틀리면 invalid_refs(한 번 다시 묻는다),
    없는 장면·후보에 없는 프레임·null은 조용히 버린다."""
    picks = data.get("picks") if isinstance(data, dict) else None
    if not isinstance(picks, list):
        raise _invalid("picks 목록이 없습니다")
    out: dict[str, str] = {}
    for entry in picks:
        if not isinstance(entry, dict):
            raise _invalid("picks의 항목은 {id, frame} 객체여야 합니다")
        cut_id, frame = entry.get("id"), entry.get("frame")
        if isinstance(cut_id, str) and cut_id in ids and isinstance(frame, str) and frame in known and cut_id not in out:
            out[cut_id] = frame
    return out


def pick(cuts: list[dict[str, Any]], category: str | None,
         on_batch: Callable[[int, int], None] | None = None) -> dict[str, str]:
    """컷 id → 고른 프레임(고르지 못한 장면은 빠진다). 후보가 없으면 Claude를 부르지 않고 {}.
    on_batch(done, total): 묶음을 부르기 전마다 불린다(진행 표시·멈추기 확인용)."""
    cands = candidates(category)
    if not cands or not cuts:
        return {}
    known = {c["frame"] for c in cands}
    batches = [cuts[i:i + PICK_BATCH] for i in range(0, len(cuts), PICK_BATCH)]
    picks: dict[str, str] = {}
    pinned = {c.get("id") for c in cuts if c.get("ref_frame")}     # 고정한 장면의 사진은 화면에 안 보여 반복으로 치지 않는다
    for done, batch in enumerate(batches):
        if on_batch:
            on_batch(done, len(batches))
        scenes = [{k: c.get(k) for k in SCENE_FIELDS} for c in batch]
        ids = {s["id"] for s in scenes}
        used = sorted({frame for cut_id, frame in picks.items() if cut_id not in pinned})
        payload = {"scenes": scenes, "candidates": cands, "used": used}
        _, got = llm.ask_validated(rules(), payload, PICK_TASK, lambda d, ids=ids: _validated(d, ids, known))
        picks.update(got)
    return picks


def apply_picks(cuts: list[dict[str, Any]], picks: dict[str, str]) -> list[dict[str, Any]]:
    """고른 컷은 ref_auto를 새로 두고, 고르지 못한 컷은 ref_auto를 지운 새 컷 목록(원본은 그대로)."""
    out = []
    for c in cuts:
        cut = {k: v for k, v in c.items() if k != "ref_auto"}
        frame = picks.get(c.get("id"))
        if frame:
            cut["ref_auto"] = {"frame": frame, "match": "content"}
        out.append(cut)
    return out


def save_picks(conti_path: str | Path, picks: dict[str, str], expected_hash: str,
               now: float | None = None) -> dict[str, Any]:
    """콘티 컷의 ref_auto만 바꿔 저장한다(본문 표·HTML 다시 그림, '## 내 메모' 보존). 고르는 사이 파일이 바뀌었으면
    conti_changed. 손으로 고친 상태는 먼저 이력에 남기고, 저장 뒤 auto 이력을 남긴다."""
    from scripts import conti_history                  # conti_history가 conti를 쓰므로 함수 안에서 불러온다

    path = conti._resolve(conti_path)
    with conti.HISTORY_LOCK:
        cf = conti.read_conti(path)
        if conti.bytes_hash(cf.raw) != expected_hash:
            raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
        current, before, _ = conti.ensure_ids_file(cf)
        fm = conti.validated({**before, "cuts": apply_picks(before["cuts"], picks)})
        conti_history.check_external(path, before, now)
        written, html_path = conti._write_conti_file(current, fm)
        conti_history.record(path, fm, "auto", now=now)
    return {"status": "picked", "path": conti._rel(path), "html": conti._rel(html_path), "abs_html": str(html_path),
            "hash": conti.bytes_hash(written.raw), "picked": sum(1 for c in fm["cuts"] if c.get("ref_auto")),
            "scenes": len(fm["cuts"])}
