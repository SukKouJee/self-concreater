"""동등성 시나리오(서버 설계 §10-2): 같은 코어 작업 순서를 File Ctx(작업 폴더)와 서버 Ctx(ServerBackend)에서 돌리고
작업 폴더 트리 바이트·폴더·반환 JSON을 비교한다. PG 16 + MinIO가 없으면 skip.

시나리오: scaffold·init_category·프로필 → 수집 캐시(raw·cuts) → 영상 md 쓰기(ref 7 + mine 1)·relabel → 유형화 run·apply
→ 모티브 카드 → 기획안 저장 → 콘티 저장·컷 수정·이름 붙이기·되돌리기·이력 → 미션 give·submit → 리포트 → 현황
→ 휴지통(영상 하나, 기획안+콘티+이력 폴더).

허용하는 차이(문서화된 것만, 이 파일의 `_normalize`·`_local_view`가 그것만 지운다):
1. 표시 문자열: 작업 폴더 라벨(`scaffold.root`, `status.work_root`)은 File = 폴더 절대 경로, 서버 = `ytc:{email}`.
   `abs_html`·`abs_path`(display_path)는 File = 절대 경로, 서버 = 키(§3.3). 둘 다 "작업 폴더 + 키" 꼴로 맞춰 비교한다.
2. 시각: `videomd.write`의 `written_at`(반환값만, 파일에는 없다)은 지운다. 휴지통 스탬프(`workspace/trash/<stamp>`)는
   순서 번호로 바꿔 비교한다. 파일 안의 시각은 고정 시계(cluster `datetime`, conti `now`)로 두 쪽이 같다.
3. 휴지통의 공유 캐시: 서버는 `workspace/{raw,cuts,frames,shots}/` 키를 옮기지 않는다(§3.3, 다른 사용자 데이터).
   File의 휴지통에서 그 키를 빼고 비교하고, 서버에는 그 캐시가 남아 있는지 따로 본다.
4. 임시 폴더 `workspace/tmp/`(scratch)는 저장소가 아니다: File 트리에서 뺀다.
   공유 캐시 폴더도 사용자 폴더 목록에서 뺀다.
"""
from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import cluster, conti, conti_history, draft, fetch, keys, mission, motif, refstore, report, status, videomd
from scripts import ctx as ctx_mod
from scripts.ctx import Ctx
from tests.helpers.pg import pg_ctx
from tests.helpers.tree_snapshot import tree

CAT = "travel"
DAY = "2026-11-01"
T0 = 1_800_000_000.0
PROFILE = ("---\ncreated: 2026-10-01\nupdated: 2026-10-01\nschema_version: 1\n---\n# 크리에이터 프로필\n\n"
           "## 관심 주제\n- 제주 여행 🏝️\n\n## 포맷\n쇼츠\n\n## 롤모델\n\n## 하루 시간\n20분\n\n"
           "## 피하고 싶은 스타일\n- 얼굴 출연\n\n## 강점\n\n## 약점\n\n## 성장 단계\n관찰\n")

SKELETON_A = [("hook", None, 0, 3), ("body", "story", 3, 52), ("cta", None, 52, 60)]
SKELETON_B = [("hook", None, 0, 5), ("intro", None, 5, 15), ("body", "list_item", 15, 50), ("outro", None, 50, 60)]
NARRATIVES = ["problem_solution", "list", "journey", "compare", "reaction"]
REFS = [(f"refA{i}", SKELETON_A, "result_first" if i < 3 else "question", NARRATIVES[i % 5], i * 0.2)
        for i in range(4)] \
    + [(f"refB{i}", SKELETON_B, "result_first" if i < 2 else "situation", NARRATIVES[(i + 2) % 5], i * 0.2)
       for i in range(3)]
MINE = "mineVid01"


class _FrozenDatetime(datetime):
    """cluster.run_at·applied_at을 두 실행에서 같게(파일 바이트에 들어간다)."""

    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        return cls(2026, 11, 1, 0, 0, 0, tzinfo=tz)


def _raw(vid: str, title: str) -> dict[str, Any]:
    return {"id": vid, "url": f"https://youtu.be/{vid}", "format": "shorts", "fetched_at": "2026-10-20T00:00:00+00:00",
            "meta": {"title": title, "channel": "채널", "duration": 60, "language": "ko"},
            "transcript": {"source": "auto", "language": "ko", "segments": [
                {"start": 0.0, "dur": 2.0, "text": "제주 한 달 살기 🏝️"},
                {"start": 2.5, "dur": 3.0, "text": "첫날은 바다부터"},
                {"start": 50.0, "dur": 2.0, "text": "구독 좋아요"}]}}


def _cuts(vid: str, shift: float) -> dict[str, Any]:
    cuts = [round(3.0 + shift, 2), round(15.0 + shift, 2), round(30.0 + shift, 2), round(52.0 + shift, 2)]
    edges = [0.0, *cuts, 60.0]
    return {"id": vid, "duration_sec": 60.0, "fps": 30.0, "scene_count": 5, "avg_cut_sec": 12.0, "cuts": cuts,
            "camera": {"static": 0.8, "pan": 0.2, "zoom": 0.0}, "frames": [],
            "scenes": [{"start": a, "end": b, "camera": "static", "frame": None}
                       for a, b in zip(edges, edges[1:], strict=False)]}


def _analysis(skeleton: list[tuple[str, str | None, int, int]], hook: str, narrative: str,
              shift: float) -> dict[str, Any]:
    sections = []
    for label, sub, start, end in skeleton:
        s: dict[str, Any] = {"label": label, "start": 0 if start == 0 else round(start + shift, 2),
                             "end": 60 if end == 60 else round(end + shift, 2), "content": f"{label} 장면 🌊"}
        if sub:
            s["sub"] = sub
        sections.append(s)
    return {"summary": "제주 한 달 살기 브이로그", "synopsis": "바다에서 시작해 숙소와 맛집으로 이어간다.",
            "hook_type": hook, "narrative_type": narrative, "sections": sections,
            "techniques": [{"kind": "훅", "name": "결과 선공개", "desc": "완성 장면을 첫 1초에", "at": 0},
                           {"kind": "엔딩", "name": "구독 요청", "desc": "마지막에 말과 자막으로"}]}


DRAFT = {
    "title": "제주 한 달 살기 3단계 | 첫날", "topic": "제주 여행", "source": "M-travel-001", "archetype": "A1",
    "hook_type": "result_first", "narrative_type": "journey",
    "sections": [
        {"label": "hook", "start": 0, "end": 2, "script": "자막: 한 달 살기 첫날?",
         "shots": [{"size": "탑뷰", "angle": "탑뷰", "sec": 2, "action": "캐리어 열기"}]},
        {"label": "body", "sub": "story", "start": 2, "end": 6, "script": "말: 바다부터 갑니다",
         "shots": [{"size": "클로즈업", "angle": "45도", "sec": 2, "action": "파도 | 발"},
                   {"size": "익스트림 클로즈업", "sec": 2, "action": "모래"}]},
        {"label": "cta", "start": 6, "end": 8, "script": "자막: 다음은 숙소",
         "shots": [{"size": "인서트", "angle": "탑뷰", "sec": 2, "action": "열쇠"}]},
    ],
}

CONTI = {
    "script": [{"text": "한 달 살기 첫날?"}, {"text": "바다부터 갑니다. 와 파랗다"}, {"text": ""}],
    "cuts": [
        {"section": 0, "size": "탑뷰", "angle": "탑뷰", "sec": 2, "action": "캐리어", "line": "한 달 살기 첫날?",
         "caption": "첫날 🧳", "sfx": "띵", "guide": "카메라 고정"},
        {"section": 1, "size": "클로즈업", "angle": "45도", "sec": 2, "action": "파도", "line": "바다부터 갑니다.",
         "caption": "", "sfx": "파도 소리", "guide": "소리 크게"},
        {"section": 1, "size": "익스트림 클로즈업", "sec": 2, "action": "모래", "line": "와 파랗다",
         "caption": "파랗다", "sfx": "", "guide": "발만"},
        {"section": 2, "size": "인서트", "angle": "탑뷰", "sec": 2, "action": "열쇠", "line": "",
         "caption": "다음은 숙소", "sfx": "", "guide": "3초 정지"},
    ],
}

MISSION = {"title": "파도 소리 15초 찍기", "kind": "촬영", "minutes": 20, "source": "M-travel-001",
           "why": "★카드의 소리 기법 연습", "steps": ["폰 고정", "파도 15초"], "deliverable": "15초 클립 1개"}
NOTES = {"axes": {a: "근거 🧭" for a in ["structure", "hook", "narrative", "title", "pacing"]},
         "archetypes": [{"id": "A1", "name": "결과 먼저 여행기"}, {"id": "A2", "name": "리스트형 숙소 소개"}]}
CARDS = [{"type": "technique", "title": "제주 3단계 바다", "idea": "단계 아이콘을 띄우고 가까운 바다부터 간다.",
          "how": ["0–3초 탑뷰", "단계별 한 컷"], "basis": "A1 결과 먼저 여행기", "evidence": ["refA0"]}]


def _handoff(c: Ctx, name: str, data: Any) -> Path:
    path = c.scratch.tmp_path(name)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def scenario(c: Ctx, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """같은 순서의 실제 코어 작업. -> [(단계 이름, 반환값)]. 반환값은 JSON으로 한 번 왕복한다(CLI 출력과 같은 모양)."""
    counter = itertools.count(1)
    monkeypatch.setattr(conti, "new_cut_id", lambda: f"id{next(counter):06d}")
    monkeypatch.setattr(cluster, "datetime", _FrozenDatetime)
    out: list[tuple[str, Any]] = []

    def step(name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        shown = json.loads(json.dumps(result, ensure_ascii=False))
        if isinstance(shown, dict):
            shown.pop("written_at", None)                       # allowed difference 2 (return value only)
        out.append((name, shown))
        return result

    with ctx_mod.use(c):
        step("scaffold", refstore.scaffold)
        step("init", refstore.init_category, CAT)
        c.store.write(keys.PROFILE, PROFILE.encode("utf-8"))
        for vid, skeleton, hook, narrative, shift in [*REFS, (MINE, SKELETON_A, "question", "journey", 0.0)]:
            fetch.save_raw(_raw(vid, f"제주 한 달 살기 {vid}! 🏝️"))
            c.shared.put(keys.cuts(vid), refstore.text_bytes(json.dumps(_cuts(vid, shift), ensure_ascii=False)))
            role = "mine" if vid == MINE else "ref"
            path = _handoff(c, f"analysis_{vid}.json", _analysis(skeleton, hook, narrative, shift))
            step(f"videomd.write {vid}", videomd.write, vid, CAT, role, path)
        a0 = keys.video_md(CAT, "refA0")
        step("relabel", videomd.relabel, "refA0", CAT, {"hook_type": "question"}, c.store.read(a0).version)
        step("cluster.run", cluster.run, CAT)
        step("cluster.apply", cluster.apply, CAT, _handoff(c, "notes.json", NOTES))
        step("motif.add", motif.add, CAT, _handoff(c, "cards.json", CARDS))
        saved = step("draft.save", draft.save, CAT, _handoff(c, "draft.json", DRAFT))
        draft_key = saved["path"]
        made = step("conti.save", conti.save, draft_key, _handoff(c, "conti.json", CONTI), now=T0)
        conti_key = made["path"]
        step("conti.update_cut", conti.update_cut, conti_key, 2, {"line": "바다부터 갑니다 🌊"},
             c.store.read(conti_key).version, now=T0 + 10)
        step("history.name", conti_history.name, conti_key, "첫 수정", c.store.read(conti_key).version, now=T0 + 20)
        step("history.restore", conti_history.restore, conti_key, 1, c.store.read(conti_key).version, now=T0 + 30)
        step("history.list", conti_history.list_versions, conti_key)
        step("history.diff", conti_history.diff_versions, conti_key, 1, 2)
        step("mission.save", mission.save, _handoff(c, "mission.json", MISSION), DAY)
        step("mission.submit", mission.submit, "C:\\clips\\wave.mp4", "right", "좋았다 👍", DAY, "파도 찍음")
        step("report.build", report.build, CAT, DAY)
        step("status", status.overview)
        step("trash.video", videomd.trash_video, "refB2", CAT)
        step("trash.draft", draft.trash, draft_key)
        step("status.after", status.overview)
    return out


# --- 비교 ------------------------------------------------------------------------------------

_STAMP_RE = re.compile(r"^workspace/trash/(\d{8}-\d{6}(?:-\d+)?)(/.*)?$")


def _stamp_order(stamp: str) -> tuple[str, int]:
    base, _, n = stamp.partition("-")[2].partition("-")
    return stamp[:8] + base, int(n or 1)


def _restamp(keys_: list[str]) -> dict[str, str]:
    """휴지통 스탬프 → `#1`, `#2` …(시각·같은 초 번호 순서). 스탬프는 실행 시각이라 두 쪽이 다르다(허용 차이 2)."""
    found = {m.group(1) for key in keys_ if (m := _STAMP_RE.match(key))}
    return {stamp: f"#{i}" for i, stamp in enumerate(sorted(found, key=_stamp_order), 1)}


def _renamed(key: str, stamps: dict[str, str]) -> str:
    m = _STAMP_RE.match(key)
    return f"{keys.TRASH}{stamps[m.group(1)]}{m.group(2) or ''}" if m else key


def _local_view(root: Path) -> tuple[dict[str, str], set[str]]:
    """File 작업 폴더의 {키: sha256}(scratch 제외, 휴지통 안 공유 캐시 제외)와 폴더 키 집합."""
    files = tree(root, skip=(keys.TMP,))
    stamps = _restamp(list(files))
    view: dict[str, str] = {}
    for key, sha in files.items():
        m = _STAMP_RE.match(key)
        if m and (m.group(2) or "/")[1:].startswith(keys.SHARED_PREFIXES):
            continue                                            # allowed difference 3
        view[_renamed(key, stamps)] = sha
    folders = {p.relative_to(root).as_posix() + "/" for p in root.rglob("*") if p.is_dir()}
    return view, {f for f in folders if not f.startswith((keys.TMP, keys.TRASH, *keys.SHARED_PREFIXES))}


def _server_view(backend: Any, uid: str) -> tuple[dict[str, str], set[str]]:
    from server.backend import export_tree

    out = export_tree(backend, uid)
    hashes = out.hashes()
    stamps = _restamp(list(hashes))
    return ({_renamed(k, stamps): v for k, v in hashes.items()},
            {f for f in out.folders if not f.startswith(keys.TRASH)})


ABS_FIELDS = ("abs_html", "abs_path")      # display_path: File = str(root / key), server = key (§3.3)
LABEL_FIELDS = ("root", "work_root")       # store.label(): File = str(root), server = ytc:{email}
TIME_FIELDS = ("mtime",)                   # status.recent: wall-clock write time (the order is still compared)


def _normalize(value: Any, to_key: Callable[[str], str]) -> Any:
    """허용 차이 1·2만 지운다: 라벨 필드 → `<root>`, display_path 필드 → `<root>/<키>`, 쓰기 시각 → `<mtime>`,
    휴지통 스탬프 → `<stamp>`."""
    if isinstance(value, dict):
        return {k: ("<root>" if k in LABEL_FIELDS and isinstance(v, str)
                    else "<mtime>" if k in TIME_FIELDS and isinstance(v, (int, float))
                    else f"<root>/{to_key(v)}" if k in ABS_FIELDS and isinstance(v, str)
                    else _normalize(v, to_key)) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, to_key) for v in value]
    if isinstance(value, str):
        m = _STAMP_RE.match(value)
        if m and not m.group(2):
            return f"{keys.TRASH}<stamp>"
    return value


@pytest.fixture
def runs(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "work"
    root.mkdir()
    local_out = scenario(Ctx(root=root), monkeypatch)
    server = pg_ctx(request, "eq@example.com")
    server_out = scenario(server, monkeypatch)
    return {"root": root, "local_out": local_out, "server": server, "server_out": server_out,
            "backend": request.getfixturevalue("pg_backend")}


def test_work_tree_bytes_and_folders_are_equal(runs: dict[str, Any]) -> None:
    local_files, local_folders = _local_view(runs["root"])
    server_files, server_folders = _server_view(runs["backend"], runs["server"].user_id)
    assert sorted(server_files) == sorted(local_files)
    assert server_files == local_files
    assert len(local_files) >= 30 and len(local_folders) >= 10          # not a vacuous comparison
    assert sorted(server_folders) == sorted(local_folders)
    # the scenario really reached every store: docs, objects, shared cache, trash (doc, object, folder)
    trash = [k for k in local_files if k.startswith(keys.TRASH)]
    assert keys.TRASH + "#1/" + keys.video_md(CAT, "refB2") in trash
    assert any(k.startswith(keys.TRASH + "#2/workspace/conti_history/") for k in trash)
    assert keys.typify(CAT) in local_files and keys.report_html(CAT, DAY) in local_files
    assert keys.raw("refA0") in local_files and keys.mission_md(DAY) in local_files
    # allowed difference 3: the server keeps the trashed video's shared cache in place
    server = runs["server"]
    with ctx_mod.use(server):
        assert server.shared.exists(keys.raw("refB2")) and server.shared.exists(keys.cuts("refB2"))
    assert not (runs["root"] / "workspace" / "raw" / "refB2.json").exists()       # File moved it to its trash


def test_returned_json_is_equal_except_documented_display_strings(runs: dict[str, Any]) -> None:
    root = str(runs["root"])

    def local_key(s: str) -> str:
        assert s.startswith(root), s
        return s[len(root):].lstrip("\\/").replace("\\", "/")

    local = [(n, _normalize(v, local_key)) for n, v in runs["local_out"]]
    for _, value in local:                                     # allowed difference 3: shared keys stay on the server
        if isinstance(value, dict) and isinstance(value.get("moved"), list):
            value["moved"] = [k for k in value["moved"] if keys.kind(k) != "shared"]
    remote = [(n, _normalize(v, lambda s: s)) for n, v in runs["server_out"]]
    assert [n for n, _ in remote] == [n for n, _ in local]
    for (name, a), (_, b) in zip(local, remote, strict=True):
        assert b == a, name
    assert dict(runs["server_out"])["scaffold"]["root"] == "ytc:eq@example.com"
