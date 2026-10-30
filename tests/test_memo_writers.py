"""'## 내 메모'가 있는 문서를 다시 쓰는 정상 흐름이 메모를 그대로 두는지(S3 설계 §4 메모 가드의 정규화 규칙).

메모 = 프런트매터 뒤 본문에서 `## 내 메모` 줄(줄 끝 공백 무시) 다음부터 다음 `## ` 제목 줄 전까지.
비교는 LF로 맞추고 줄마다 끝 공백을 지운 뒤 앞뒤 빈 줄을 뺀 문자열로 한다. 흐름마다 메모를 옮기는 방식이 조금씩
다르다(앞 빈 줄을 버림, 끝 공백을 버림, CRLF→LF, 메모 뒤 절까지 통째로 옮김). 이 파일의 `memo_of`는 저장소 가드와
따로 쓴 기준이고, 아래 흐름이 모두 이 기준으로 메모를 지켜야 가드가 정상 흐름을 막지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts import cluster, conti, fetch, mission, motif, polish, refstore, similarity, videomd
from scripts import conti_history as ch
from scripts.common import ScriptError
from scripts.filestore import FileDocStore
from scripts.stores import VersionConflict
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures
from tests.test_mission import PROFILE, mission_json
from tests.test_polish import choice
from tests.test_videomd import ANALYSIS, CUTS, RAW, VID
from tests.typify_fixtures import CAT

HEADING = "## 내 메모"
MEMOS = {
    "plain": (HEADING, "훅이 좋아서 따라 해 볼 것 🎬\n- 다음엔 자막 색도 볼 것\n"),
    "spaced": (HEADING, "\n\n  들여 쓴 메모  \n둘째 줄\t\n\n\n"),
    "subsection": (HEADING, "메모 본문\n\n## 내가 붙인 절\n- 링크 메모\n"),
    "heading_spaces": (HEADING + "  ", "제목 줄 끝에 공백\n"),
}
VARIANTS = [(name, crlf) for name in MEMOS for crlf in (False, True)]
IDS = [f"{name}-{'crlf' if crlf else 'lf'}" for name, crlf in VARIANTS]


def memo_of(raw: bytes) -> str | None:
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    m = re.match(r"\A---\n.*?\n---(?:\n|\Z)", text, re.S)
    lines = (text[m.end():] if m else text).split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == HEADING), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(line.rstrip() for line in lines[start + 1:end]).strip("\n")


def set_memo(path: Path, variant: str, crlf: bool) -> str:
    """문서의 메모를 variant로 바꾼다(없으면 끝에 붙인다). 기준 메모 문자열을 돌려준다."""
    heading, memo = MEMOS[variant]
    text = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    at = text.find(f"\n{HEADING}\n")
    text = (text[:at] if at >= 0 else text.rstrip("\n") + "\n") + f"\n{heading}\n{memo}"
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    expected = memo_of(path.read_bytes())
    assert expected                                    # 기준 메모가 비어 있지 않아야 검사가 의미 있다
    return expected


def kept(path: Path, expected: str) -> None:
    assert memo_of(path.read_bytes()) == expected


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tmp_json(home: Path, name: str, data) -> Path:
    path = home / "workspace" / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_memo_of_rule():
    raw = "---\r\na: 1\r\n---\r\n## 요약\r\nx\r\n## 내 메모 \r\n\r\n  메모  \r\n끝\t\r\n\r\n## 다음\r\ny\r\n".encode()
    assert memo_of(raw) == "  메모\n끝"
    assert memo_of(b"---\na: 1\n---\n## x\n") is None
    assert memo_of("﻿---\na: 1\n---\n## 내 메모\n".encode()) == ""


# --- 영상 md(다시 분석), techniques.md·index.md(같은 흐름), 프런트매터만 고치기 ----------------------

@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_video_force_rewrite_keeps_memos_of_video_techniques_and_index(home, variant, crlf):
    fetch.save_raw(RAW)
    (home / "workspace" / "cuts").mkdir(parents=True, exist_ok=True)
    (home / "workspace" / "cuts" / f"{VID}.json").write_text(json.dumps(CUTS), encoding="utf-8")
    videomd.write(VID, CAT, "ref", tmp_json(home, "analysis.json", ANALYSIS))
    cat = home / "refs" / f"ref_{CAT}"
    docs = [cat / "videos" / f"{VID}.md", cat / "techniques.md", cat / "index.md"]
    expected = [set_memo(p, variant, crlf) for p in docs]

    videomd.write(VID, CAT, "ref", tmp_json(home, "analysis.json", {**ANALYSIS, "summary": "새 요약"}), force=True)

    for path, memo in zip(docs, expected):
        kept(path, memo)
    assert "새 요약" in docs[0].read_text("utf-8")


@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_update_frontmatter_keeps_memo(home, variant, crlf):
    key = f"refs/ref_{CAT}/videos/a00.md"
    path = home / key
    expected = set_memo(path, variant, crlf)
    refstore.update_frontmatter(key, {"reviewed": True}, remove=["shots"], expect_version=sha(path))
    kept(path, expected)
    refstore.update_stage(CAT)
    kept(path, expected)


# --- index.md: 유형화 반영, 모티브 카드, 별표 / techniques.md: 기법 합치기 --------------------------

NOTES = {
    "axes": {a: "근거 문장" for a in ["structure", "hook", "narrative", "title", "pacing"]},
    "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
}
CARD = {"type": "technique", "title": "첫 1초 완성품 클로즈업", "idea": "완성된 음식을 첫 1초에 보여 준다.",
        "how": ["완성 직후 탑뷰로 2초 촬영"], "evidence": ["a00", "a01"]}


@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_typify_cards_star_and_merge_keep_memo(home, variant, crlf):
    cat = home / "refs" / f"ref_{CAT}"
    index = cat / "index.md"
    expected = set_memo(index, variant, crlf)
    cluster.run(CAT)
    cluster.apply(CAT, tmp_json(home, "notes.json", NOTES))
    kept(index, expected)
    card = motif.add(CAT, tmp_json(home, "cards.json", [CARD]))["ids"][0]
    kept(index, expected)
    motif.star(CAT, card)
    kept(index, expected)

    tech = cat / "techniques.md"
    fm, body = refstore.read(f"refs/ref_{CAT}/techniques.md")
    body = body.replace("## 훅\n", "## 훅\n- **결과 선공개**: 완성품 먼저 — [a00](videos/a00.md)\n"
                                   "- **완성품 먼저 보여주기**: 첫 장면에 결과 — [a01](videos/a01.md)\n")
    tech.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    expected = set_memo(tech, variant, crlf)
    plan = [{"kind": "훅", "keep": "결과 선공개", "merge": ["완성품 먼저 보여주기"]}]
    assert motif.merge_techniques(CAT, tmp_json(home, "plan.json", plan))["merged"] == 1
    kept(tech, expected)


# --- mine 진단 다시 저장, 의도한 차이 ---------------------------------------------------------

@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_similarity_resave_and_intentional_keep_memo(home, variant, crlf):
    cluster.run(CAT)
    cluster.apply(CAT, tmp_json(home, "notes.json", NOTES))
    target = f"refs/ref_{CAT}/videos/a00.md"
    refstore.update_frontmatter(target, {"role": "mine", "id": "my-1"})
    result = similarity.compare(CAT, Path(target))
    notes = {"summary": "비슷하다", "gaps": {g["key"]: "설명" for g in result["gaps"]}, "actions": ["첫째"]}
    similarity.save(CAT, Path(target), tmp_json(home, "sim.json", notes))
    mine = home / "mine" / "my-1.md"
    expected = set_memo(mine, variant, crlf)
    similarity.save(CAT, Path(target), tmp_json(home, "sim.json", notes))
    kept(mine, expected)
    similarity.intentional(CAT, "my-1", "hook")
    kept(mine, expected)


# --- 콘티: 고치기, 컷 넣기, 되돌리기, 다시 만들기 -----------------------------------------------

@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_conti_edits_restore_and_recreate_keep_memo(home, draft_md, variant, crlf):
    key = conti.save(draft_md, conti_file(home))["path"]
    path = home / key
    expected = set_memo(path, variant, crlf)
    conti.update_cut(key, 1, {"line": "바뀐 대사"})
    kept(path, expected)
    conti.edit_cuts(key, {"op": "insert", "at": 1}, conti.content_hash(key))
    kept(path, expected)
    ch.restore(key, 1, conti.content_hash(key))
    kept(path, expected)
    conti.save(draft_md, conti_file(home), force=True)
    kept(path, expected)


# --- 기획안 다듬기 ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_polish_apply_keeps_memo(home, draft_md, variant, crlf):
    expected = set_memo(draft_md, variant, crlf)
    key = draft_md.relative_to(home).as_posix()
    first = polish.apply(key, choice(), sha(draft_md))
    kept(draft_md, expected)
    polish.apply(key, choice(title="두 번째 제목", hook_script="말: 두 번째 훅"), first["hash"])
    kept(draft_md, expected)


# --- 미션 제출·건너뛰기, 회고 다시 저장, 프로필 반영 ------------------------------------------------

DAY = "2026-10-31"
REVIEW = {"summary": "좋은 주", "repeated_weakness": ["훅"], "next_week": ["쇼츠"],
          "profile_proposal": {"약점": "훅 약함"}}


@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
def test_mission_review_and_profile_keep_memo(home, variant, crlf):
    profile = home / "profile.md"
    profile.write_text(PROFILE, encoding="utf-8")
    mission.save(mission_json(home), DAY)
    day = home / "missions" / f"{DAY}.md"
    expected = set_memo(day, variant, crlf)
    mission.submit("a.mp4", "right", "잘 됨", DAY, "손 촬영")
    kept(day, expected)
    mission.skip("바빠서", DAY)
    kept(day, expected)

    mission.review_save("2026-W44", tmp_json(home, "review.json", REVIEW))
    review = home / "reviews" / "2026-W44.md"
    expected = set_memo(review, variant, crlf)
    mission.review_save("2026-W44", tmp_json(home, "review.json", {**REVIEW, "summary": "수정"}))
    kept(review, expected)
    refstore.update_frontmatter("reviews/2026-W44.md", {"approved": ["약점"]})
    kept(review, expected)

    expected = set_memo(profile, variant, crlf)
    mission.profile_update(tmp_json(home, "upd.json", {"성장 단계": "모방", "약점": "훅"}), DAY)
    kept(profile, expected)


# --- 가드: 저장소가 메모를 잃는 쓰기를 막는다 ---------------------------------------------------

def _guard_doc(home: Path, memo: str = "지우면 안 되는 메모 🏆\n- 둘째 줄\n") -> tuple[FileDocStore, str, bytes]:
    key = "mine/guard.md"
    raw = f"---\nid: g\n---\n# 제목\n\n## 요약\n본문\n\n{HEADING}\n{memo}".encode()
    (home / "mine").mkdir(parents=True, exist_ok=True)
    (home / key).write_bytes(raw)
    return FileDocStore(home), key, raw


@pytest.mark.parametrize("variant,crlf", VARIANTS, ids=IDS)
@pytest.mark.parametrize("bom", [False, True], ids=["nobom", "bom"])
def test_memo_text_matches_the_rule(variant, crlf, bom):
    heading, memo = MEMOS[variant]
    text = f"---\na: 1\n---\n# t\n\n## 요약\nx\n\n{heading}\n{memo}"
    raw = (b"\xef\xbb\xbf" if bom else b"") + (text.replace("\n", "\r\n") if crlf else text).encode()
    assert refstore.memo_text(raw) == memo_of(raw)
    assert refstore.memo_text(b"---\na: 1\n---\n## x\n") is None
    assert refstore.memo_text(f"# no frontmatter\n{HEADING}\n메모\n".encode()) == "메모"


@pytest.mark.parametrize("new_tail", [
    "",                                                   # 메모 절을 통째로 뺌
    f"{HEADING}\n",                                       # 제목만 남김
    f"{HEADING}\n지우면 안 되는 메모 🏆\n",                  # 둘째 줄을 잃음
    f"{HEADING}\n지우면 안 되는 메모 🏆\n- 둘째 줄 고침\n",       # 내용을 바꿈
    f"### 내 메모\n지우면 안 되는 메모 🏆\n- 둘째 줄\n",       # 제목 수준이 바뀜(메모가 아님)
    f"{HEADING}\n## 끼어든 절\n지우면 안 되는 메모 🏆\n- 둘째 줄\n",   # 메모 앞에 제목이 끼어듦
], ids=["dropped", "emptied", "truncated", "altered", "demoted", "split"])
def test_write_that_loses_the_memo_fails_and_leaves_the_file(home, new_tail):
    store, key, raw = _guard_doc(home)
    before = sorted(p.name for p in (home / "mine").iterdir())
    with pytest.raises(ScriptError) as exc:
        store.write(key, f"---\nid: g\n---\n# 새 본문\n\n{new_tail}".encode())
    assert exc.value.code == "memo_lost" and key in exc.value.message
    assert (home / key).read_bytes() == raw
    assert sorted(p.name for p in (home / "mine").iterdir()) == before       # 임시 파일도 없다


def test_memo_lost_through_refstore_and_expect_version(home):
    store, key, raw = _guard_doc(home)
    with pytest.raises(ScriptError) as exc:
        store.write(key, b"---\nid: g\n---\n", expect_version=hashlib.sha256(raw).hexdigest())
    assert exc.value.code == "memo_lost" and (home / key).read_bytes() == raw
    with pytest.raises(VersionConflict):                  # 버전 검사가 먼저다
        store.write(key, b"---\nid: g\n---\n", expect_version="0" * 64)
    assert (home / key).read_bytes() == raw


@pytest.mark.parametrize("new_body", [
    f"## 요약\n바뀐 본문\n\n{HEADING}\n지우면 안 되는 메모 🏆\n- 둘째 줄\n",
    f"{HEADING}\r\n\r\n지우면 안 되는 메모 🏆   \r\n- 둘째 줄\r\n\r\n\r\n## 새로 붙은 절\r\n",
    f"# 앞에 새 절\n\n## 패키징\n- 제목\n\n{HEADING}  \n\n지우면 안 되는 메모 🏆\n- 둘째 줄\t\n",
], ids=["body_changed", "crlf_blank_lines_and_trailing_space", "moved_and_heading_space"])
def test_write_that_keeps_the_memo_passes(home, new_body):
    store, key, _ = _guard_doc(home)
    data = f"---\nid: g\nnew: 1\n---\n{new_body}".encode()
    store.write(key, data)
    assert (home / key).read_bytes() == data


def test_empty_or_missing_memo_is_not_guarded(home):
    store, key, _ = _guard_doc(home, memo="\n  \n")
    store.write(key, b"---\nid: g\n---\nno memo\n")
    store.write(key, b"---\nid: g\n---\nstill none\n")
    store.write("mine/new.md", f"---\nid: n\n---\n{HEADING}\n새 메모\n".encode())
    assert (home / "mine/new.md").is_file()


def test_unreadable_old_bytes_still_guarded(home):
    store, key, _ = _guard_doc(home, memo="메모 \xff 깨짐\n")
    (home / key).write_bytes((home / key).read_bytes().replace("\xff".encode(), b"\xff"))
    raw = (home / key).read_bytes()
    with pytest.raises(ScriptError) as exc:
        store.write(key, b"---\nid: g\n---\n")
    assert exc.value.code == "memo_lost" and (home / key).read_bytes() == raw
    store.write(key, raw + b"\n")                         # 같은 바이트(끝 빈 줄만 다름)는 통과
    assert (home / key).read_bytes() == raw + b"\n"


def test_create_never_replaces_a_doc_with_a_memo(home):
    store, key, raw = _guard_doc(home)
    with pytest.raises(FileExistsError):
        store.create(key, b"---\nid: g\n---\n")
    assert (home / key).read_bytes() == raw


def test_mission_swap_over_a_user_memo_is_refused(home):
    """미션 바꾸기(swap)는 미션 md를 새로 쓴다. 사용자가 메모를 적어 둔 미션이면 memo_lost로 막고 아무것도 바꾸지 않는다."""
    (home / "profile.md").write_text(PROFILE, encoding="utf-8")
    mission.save(mission_json(home), DAY)
    day = home / "missions" / f"{DAY}.md"
    set_memo(day, "plain", False)
    raw = day.read_bytes()
    src = mission_json(home, title="다른 미션")
    with pytest.raises(ScriptError) as exc:
        mission.save(src, DAY, replace=True)
    assert exc.value.code == "memo_lost"
    assert day.read_bytes() == raw and src.exists()


def test_reanalysis_refuses_to_edit_a_technique_line_inside_the_techniques_memo(home):
    """techniques.md 메모에 기법 항목 형식 줄이 있으면 다시 분석이 그 줄을 고치려다 memo_lost로 멈춘다(영상 md는 이미 다시 쓴 뒤)."""
    fetch.save_raw(RAW)
    (home / "workspace" / "cuts").mkdir(parents=True, exist_ok=True)
    (home / "workspace" / "cuts" / f"{VID}.json").write_text(json.dumps(CUTS), encoding="utf-8")
    videomd.write(VID, CAT, "ref", tmp_json(home, "analysis.json", ANALYSIS))
    tech = home / "refs" / f"ref_{CAT}" / "techniques.md"
    tech.write_bytes(tech.read_bytes() + f"\n{HEADING}\n- **내가 본 기법**: 메모 — [{VID}](videos/{VID}.md)\n".encode())
    raw = tech.read_bytes()
    with pytest.raises(ScriptError) as exc:
        videomd.write(VID, CAT, "ref", tmp_json(home, "analysis.json", ANALYSIS), force=True)
    assert exc.value.code == "memo_lost"
    assert tech.read_bytes() == raw
