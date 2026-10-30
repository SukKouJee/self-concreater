"""장면에 사진 고르기: 레퍼런스 프레임의 한 줄 설명(desc)과 콘티 장면을 글로만 맞춰 컷에 `ref_auto`로 저장한다.

- 후보(`candidates`): 같은 카테고리 ref 영상의 대표 프레임 중 설명이 있고 파일이 있는 것만(`conti.frame_pool`).
- 고르기(`pick`): Claude에 장면(id, action, guide, size)과 후보를 짧은 키 → 한 줄(`{"c17": "desc | size | angle"}`)로
  주고 장면마다 키 하나 또는 null을 받아 프레임으로 되돌린다(skills/yt-conti/pick-refs.md, 이미지 없음). 장면은
  PICK_BATCH개씩 나눠 부르고, 앞 묶음에서 쓴 사진(키)을 알려 준다. 모르는 키·없는 장면 id는 버린다.
  뒤 묶음이 실패해도 앞 묶음의 결과는 남긴다(`PickResult.error`). 반복 컷이 남은 콘티는 먼저 합친다(`check_merged`).
- 적용(`apply_picks`, `save_picks`): 고른 장면은 `ref_auto = {"frame", "match": "content"}`, 못 고른 장면은 지운다.
  저장할 때는 고른 묶음의 장면 중 아직 있고 고를 때와 action·guide·size가 같은 컷만 바꾼다(파일 전체 해시는 보지 않는다).
  렌더할 때는 저장된 값만 쓴다(`conti.cut_refs`, Claude 안 부름). 사용자가 고정한 사진(ref_frame)이 늘 먼저다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from scripts import claude_cli, conti, ctx, llm, scene_merge
from scripts.common import ScriptError

PICK_BATCH = 120
PICK_TASK = ("pick-refs 규칙대로 장면마다 내용이 가장 잘 맞는 레퍼런스 사진 하나의 키(맞는 게 없으면 null)를 고른 "
             "JSON을 만드세요.")
SCENE_FIELDS = ("id", "action", "guide", "size")
MERGE_FIRST_MESSAGE = "반복 컷을 먼저 장면으로 합쳐 주세요"


@dataclass
class PickResult:
    """고른 결과. picks: 컷 id → 프레임(고르지 못한 장면은 빠진다). basis: 끝난 묶음의 컷 id → 고를 때의
    action·guide·size(저장 때 그대로인 컷만 바꾼다). error: 뒤 묶음이 실패했으면 그 오류(앞 묶음 결과는 남는다)."""
    picks: dict[str, str] = field(default_factory=dict)
    basis: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: ScriptError | None = None


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


def check_merged(cuts: list[dict[str, Any]]) -> None:
    """반복 컷(같은 섹션의 같은 동작)이 남아 있으면 merge_first. 합치기 전에는 장면이 많아 고르기가 비싸다."""
    if scene_merge.has_repeats(cuts):
        raise ScriptError("merge_first", MERGE_FIRST_MESSAGE)


def _line(cand: dict[str, Any]) -> str:
    return " | ".join(str(cand[k]) for k in ("desc", "size", "angle") if cand.get(k))


def _validated(data: Any, ids: set[str], keys: set[str]) -> dict[str, str]:
    """{"picks": [{"id", "frame": 키|null}]} → 컷 id → 키. 형식이 틀리면 invalid_refs(한 번 다시 묻는다),
    없는 장면·모르는 키·null은 조용히 버린다."""
    picks = data.get("picks") if isinstance(data, dict) else None
    if not isinstance(picks, list):
        raise _invalid("picks 목록이 없습니다")
    out: dict[str, str] = {}
    for entry in picks:
        if not isinstance(entry, dict):
            raise _invalid("picks의 항목은 {id, frame} 객체여야 합니다")
        cut_id, key = entry.get("id"), entry.get("frame")
        if isinstance(cut_id, str) and cut_id in ids and isinstance(key, str) and key in keys and cut_id not in out:
            out[cut_id] = key
    return out


def pick(cuts: list[dict[str, Any]], category: str | None,
         on_batch: Callable[[int, int], None] | None = None) -> PickResult:
    """장면마다 사진을 고른다. 후보가 없으면 Claude를 부르지 않고 빈 결과. 첫 묶음이 실패하면 그 오류를 그대로 내고,
    뒤 묶음이 실패하면 거기서 멈추고 앞 묶음 결과와 오류를 돌려준다.
    on_batch(done, total): 묶음을 부르기 전마다 불린다(진행 표시·멈추기 확인용)."""
    cands = candidates(category)
    if not cands or not cuts:
        return PickResult()
    keyed = {f"c{i}": c for i, c in enumerate(cands, 1)}
    listing = {key: _line(c) for key, c in keyed.items()}
    batches = [cuts[i:i + PICK_BATCH] for i in range(0, len(cuts), PICK_BATCH)]
    result = PickResult()
    pinned = {c.get("id") for c in cuts if c.get("ref_frame")}     # 고정한 장면의 사진은 화면에 안 보여 반복으로 치지 않는다
    for done, batch in enumerate(batches):
        if on_batch:
            on_batch(done, len(batches))
        scenes = [{k: c.get(k) for k in SCENE_FIELDS} for c in batch]
        ids = {s["id"] for s in scenes}
        used_frames = {frame for cut_id, frame in result.picks.items() if cut_id not in pinned}
        used = [key for key, c in keyed.items() if c["frame"] in used_frames]
        payload = {"scenes": scenes, "candidates": listing, "used": used}
        try:
            _, got = llm.ask_validated(rules(), payload, PICK_TASK,
                                       lambda d, ids=ids: _validated(d, ids, set(keyed)))
        except ScriptError as e:
            if not result.basis:
                raise
            result.error = e
            return result
        result.picks.update({cut_id: keyed[key]["frame"] for cut_id, key in got.items()})
        result.basis.update({c["id"]: {f: c.get(f) for f in conti.PICK_FIELDS} for c in batch})
    return result


def apply_picks(cuts: list[dict[str, Any]], picks: dict[str, str],
                basis: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """고른 컷은 ref_auto를 새로 두고, 고르지 못한 컷은 ref_auto를 지운 새 컷 목록(원본은 그대로).
    basis를 주면 그 안에 있고 action·guide·size가 고를 때와 같은 컷만 바꾸고 나머지는 그대로 둔다."""
    out = []
    for c in cuts:
        if basis is not None and not _unchanged(c, basis):
            out.append(dict(c))
            continue
        cut = {k: v for k, v in c.items() if k != "ref_auto"}
        frame = picks.get(c.get("id"))
        if frame:
            cut["ref_auto"] = {"frame": frame, "match": "content"}
        out.append(cut)
    return out


def _unchanged(cut: dict[str, Any], basis: dict[str, dict[str, Any]]) -> bool:
    seen = basis.get(cut.get("id"))
    return seen is not None and not conti.picked_changed(seen, {f: cut.get(f) for f in conti.PICK_FIELDS})


def save_picks(conti_path: str | Path, picks: dict[str, str], basis: dict[str, dict[str, Any]],
               now: float | None = None) -> dict[str, Any]:
    """콘티 컷의 ref_auto만 바꿔 저장한다(본문 표·HTML 다시 그림, '## 내 메모' 보존). 고르는 사이 다른 곳이 바뀌어도
    저장하고, 고른 장면 중 지워졌거나 action·guide·size가 바뀐 컷만 건너뛴다(`skipped`). 손으로 고친 상태는
    먼저 이력에 남기고, 저장 뒤 auto 이력을 남긴다."""
    from scripts import conti_history                  # conti_history가 conti를 쓰므로 함수 안에서 불러온다

    key = ctx.here().scratch.key_of(conti_path)
    with conti.HISTORY_LOCK:
        current, before, _ = conti.ensure_ids_file(conti.read_conti(key))
        kept = {c["id"] for c in before["cuts"] if _unchanged(c, basis)}
        fm = conti.validated({**before, "cuts": apply_picks(before["cuts"], picks, basis)})
        conti_history.check_external(key, before, now)
        written, html_key = conti._write_conti_file(current, fm)
        conti_history.record(key, fm, "auto", now=now)
    return {"status": "picked", "path": key, "html": html_key, "abs_html": conti.abs_path(html_key),
            "hash": conti.bytes_hash(written.raw), "picked": sum(1 for c in fm["cuts"] if c.get("ref_auto")),
            "scenes": len(fm["cuts"]), "skipped": len(set(basis) - kept)}
