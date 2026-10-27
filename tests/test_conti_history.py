import copy
import json
import shutil

import pytest

from scripts import conti, refstore
from scripts import conti_history as ch
from scripts.common import ScriptError
from tests.test_conti import CONTI, conti_file, draft_md, home  # noqa: F401 - fixtures

T0 = 1_000_000.0


@pytest.fixture
def saved(draft_md, home):
    return home / conti.save(draft_md, conti_file(home), now=T0)["path"]


def fm_of(path):
    return conti._load_conti(path)


def versions(path):
    return ch.list_versions(path)


def edit_line(path, text, now, n=1):
    return conti.update_cut(path, n, {"line": text}, now=now)


def add_memo(path, memo):
    path.write_bytes(path.read_bytes().replace("## 내 메모".encode(), f"## 내 메모\n{memo}".encode()))


def state(path):
    return ch.state_of(fm_of(path))


# --- 묶음 ---------------------------------------------------------------------------

def test_save_records_created(saved):
    vs = versions(saved)
    assert [(v["n"], v["kind"]) for v in vs] == [(1, "created")]
    assert vs[0]["summary"] == {"cuts": 4, "total_sec": 8.0, "changed": 0}
    assert (ch.history_dir(saved) / "0001.json").is_file()


def test_auto_within_group_window_overwrites_latest(saved):
    edit_line(saved, "첫 수정", T0 + 10)
    edit_line(saved, "둘째 수정", T0 + 10 + 4 * 60)
    vs = versions(saved)
    assert [(v["n"], v["kind"]) for v in vs] == [(2, "auto"), (1, "created")]
    assert ch.get_version(saved, 2)["state"]["cuts"][0]["line"] == "둘째 수정"
    assert vs[0]["summary"]["changed"] == 1


def test_auto_after_group_window_opens_new_version(saved):
    edit_line(saved, "첫 수정", T0 + 10)
    edit_line(saved, "둘째 수정", T0 + 10 + 6 * 60)
    assert [(v["n"], v["kind"]) for v in versions(saved)] == [(3, "auto"), (2, "auto"), (1, "created")]
    assert ch.get_version(saved, 2)["state"]["cuts"][0]["line"] == "첫 수정"


def test_group_window_follows_last_touch_not_first(saved):
    edit_line(saved, "a", T0 + 10)
    edit_line(saved, "b", T0 + 10 + 240)
    edit_line(saved, "c", T0 + 10 + 480)            # 마지막 손길 기준 4분 → 계속 같은 묶음
    assert [v["n"] for v in versions(saved)] == [2, 1]


def test_edit_after_restore_opens_new_auto(saved):
    edit_line(saved, "바꿈", T0 + 10)
    ch.restore(saved, 1, None, now=T0 + 20)
    edit_line(saved, "다시 바꿈", T0 + 30)
    assert [(v["n"], v["kind"]) for v in versions(saved)][:3] == [(4, "auto"), (3, "restored"), (2, "auto")]


def test_edit_after_named_opens_new_auto(saved):
    edit_line(saved, "바꿈", T0 + 10)
    ch.name(saved, "기획 완료", None, now=T0 + 20)
    edit_line(saved, "또 바꿈", T0 + 30)
    assert [v["kind"] for v in versions(saved)][:3] == ["auto", "named", "auto"]


def test_record_rejects_unknown_kind(saved):
    with pytest.raises(ScriptError) as e:
        ch.record(saved, fm_of(saved), "weird")
    assert e.value.code == "bad_request"


def test_versions_are_valid_json_without_temp_files(saved):
    edit_line(saved, "x", T0 + 1)
    files = sorted(p.name for p in ch.history_dir(saved).iterdir())
    assert files == ["0001.json", "0002.json"]
    assert json.loads((ch.history_dir(saved) / "0002.json").read_text("utf-8"))["kind"] == "auto"


# --- 외부 수정 감지 --------------------------------------------------------------------

def test_memo_only_change_is_not_external(saved):
    add_memo(saved, "현장에서 적은 메모")
    assert ch.check_external(saved, fm_of(saved), now=T0 + 5) is None
    assert len(versions(saved)) == 1


def test_changed_line_in_md_creates_one_external(saved):
    fm, _ = refstore.read_md(saved)
    fm["cuts"][0]["line"] = "손으로 고침"
    conti._write_conti(saved, saved.read_bytes(), conti.retime(fm))
    result = ch.check_external(saved, fm_of(saved), now=T0 + 5)
    assert result["kind"] == "external" and result["n"] == 2
    assert ch.check_external(saved, fm_of(saved), now=T0 + 6) is None
    assert [v["kind"] for v in versions(saved)] == ["external", "created"]


def test_first_id_assignment_is_not_external(saved):
    fm, body = refstore.read_md(saved)
    for c in fm["cuts"]:
        c.pop("id")
    fm["schema_version"] = 2
    saved.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    assert conti.ensure_ids(saved) is True
    assert ch.check_external(saved, fm_of(saved), now=T0 + 5) is None
    assert len(versions(saved)) == 1


def test_check_external_without_history_creates_created(saved):
    shutil.rmtree(ch.history_dir(saved))
    assert ch.check_external(saved, fm_of(saved), now=T0) is None
    assert [v["kind"] for v in versions(saved)] == ["created"]


def test_update_on_legacy_conti_keeps_pre_edit_state(saved):
    shutil.rmtree(ch.history_dir(saved))
    edit_line(saved, "고침", T0 + 5)
    assert [v["kind"] for v in versions(saved)] == ["auto", "created"]
    assert ch.get_version(saved, 1)["state"]["cuts"][0]["line"] == "4단계 다 먹을 수 있을까?"


# --- 다시 만들기 -----------------------------------------------------------------------

def test_force_save_records_before_recreate_then_created(saved, draft_md, home):
    edit_line(saved, "고친 대사", T0 + 5)
    data = copy.deepcopy(CONTI)
    data["cuts"][0]["line"] = "새로 만든 대사"
    conti.save(draft_md, conti_file(home, data), force=True, now=T0 + 1000)
    assert [v["kind"] for v in versions(saved)] == ["created", "before_recreate", "auto", "created"]
    assert ch.get_version(saved, 3)["state"]["cuts"][0]["line"] == "고친 대사"
    assert ch.get_version(saved, 4)["state"]["cuts"][0]["line"] == "새로 만든 대사"


# --- 되돌리기 · 이름 ---------------------------------------------------------------------

def test_restore_keeps_memo_and_restores_state(saved):
    add_memo(saved, "지우면 안 되는 메모")
    edit_line(saved, "바뀐 대사", T0 + 5)
    result = ch.restore(saved, 1, conti.content_hash(saved), now=T0 + 10)
    assert result["status"] == "restored"
    assert fm_of(saved)["cuts"][0]["line"] == "4단계 다 먹을 수 있을까?"
    assert "지우면 안 되는 메모" in saved.read_text("utf-8")
    assert saved.with_suffix(".html").is_file()
    latest = versions(saved)[0]
    assert latest["kind"] == "restored" and latest["name"].startswith("되돌림: ")
    assert result["hash"] == conti.content_hash(saved)


def test_restore_rejects_stale_hash_and_leaves_file(saved):
    before = saved.read_bytes()
    with pytest.raises(ScriptError) as e:
        ch.restore(saved, 1, "0" * 64)
    assert e.value.code == "conti_changed" and saved.read_bytes() == before


def test_restore_unknown_version(saved):
    with pytest.raises(ScriptError) as e:
        ch.restore(saved, 9, None)
    assert e.value.code == "version_not_found"


def test_restore_uses_version_name_in_label(saved):
    ch.name(saved, "촬영 후", None, now=T0 + 1)
    edit_line(saved, "바뀜", T0 + 2)
    ch.restore(saved, 2, None, now=T0 + 3)
    assert versions(saved)[0]["name"] == "되돌림: 촬영 후"


@pytest.mark.parametrize("label,ok", [("", False), ("   ", False), ("가", True), ("가" * 30, True), ("가" * 31, False)])
def test_name_length_check(saved, label, ok):
    if ok:
        assert ch.name(saved, label, conti.content_hash(saved), now=T0 + 1)["kind"] == "named"
    else:
        with pytest.raises(ScriptError) as e:
            ch.name(saved, label, conti.content_hash(saved))
        assert e.value.code == "invalid_name"


def test_name_rejects_stale_hash(saved):
    with pytest.raises(ScriptError) as e:
        ch.name(saved, "이름", "bad")
    assert e.value.code == "conti_changed"


# --- 비교 ---------------------------------------------------------------------------

def test_diff_changed_line_and_sec_and_total(saved):
    a = state(saved)
    b = copy.deepcopy(a)
    b["cuts"][1]["line"] = "바뀐 대사"
    b["cuts"][2]["sec"] = 3.0
    result = ch.diff(a, b)
    got = {(c["kind"], c["field"]) for c in result["changes"]}
    assert {("changed", "line"), ("changed", "sec"), ("total", "total_sec")} <= got
    line = next(c for c in result["changes"] if c["field"] == "line")
    assert (line["n_from"], line["n_to"], line["from"], line["to"]) == (2, 2, "1단계부터 갑니다.", "바뀐 대사")
    total = next(c for c in result["changes"] if c["kind"] == "total")
    assert (total["from"], total["to"]) == (8.0, 9.0)
    assert len(result["left"]) == len(result["right"]) == 4


def test_diff_added_removed_and_moved(saved):
    a = state(saved)
    b = copy.deepcopy(a)
    removed = b["cuts"].pop(0)
    b["cuts"].append(b["cuts"].pop(0))                          # 원래 2번 컷을 맨 뒤로
    b["cuts"].append({**a["cuts"][0], "id": "newcut01"})        # 새 컷
    result = ch.diff(a, b)
    kinds = {(c["kind"], c["cut"]) for c in result["changes"]}
    assert ("removed", removed["id"]) in kinds and ("added", "newcut01") in kinds
    moved = [c for c in result["changes"] if c["kind"] == "moved"]
    assert [(m["cut"], m["n_from"]) for m in moved] == [(a["cuts"][1]["id"], 2)]


def test_diff_pure_deletion_is_not_reported_as_moves(saved):
    a = state(saved)
    b = copy.deepcopy(a)
    b["cuts"].pop(0)
    kinds = [c["kind"] for c in ch.diff(a, b)["changes"]]
    assert "moved" not in kinds and kinds.count("removed") == 1


def test_diff_without_ids_matches_by_position(saved):
    a = state(saved)
    for c in a["cuts"]:
        c["id"] = None
    b = copy.deepcopy(a)
    b["cuts"][0]["guide"] = "다른 가이드"
    b["cuts"].pop()
    result = ch.diff(a, b)
    pairs = {(c["kind"], c["n_from"]) for c in result["changes"] if c["kind"] != "total"}
    assert pairs == {("removed", 4), ("changed", 1)}
    assert all(c["cut"] is None for c in result["changes"])


def test_diff_versions_against_current(saved):
    edit_line(saved, "지금 대사", T0 + 5)
    result = ch.diff_versions(saved, 1)
    assert any(c["kind"] == "changed" and c["field"] == "line" and c["to"] == "지금 대사" for c in result["changes"])
    assert ch.diff_versions(saved, 1, 2)["changes"] == result["changes"]


def test_same_state_ignores_id_and_rounding(saved):
    a = state(saved)
    b = copy.deepcopy(a)
    for c in b["cuts"]:
        c["id"] = "zzzzzzzz"
    b["script"][0]["start"] = a["script"][0]["start"] + 0.001
    assert ch.same_state(a, b)
    b["cuts"][0]["caption"] = "다름"
    assert not ch.same_state(a, b)


# --- 휴지통 · CLI -----------------------------------------------------------------------

def test_trash_moves_history_folder_with_conti(saved, home):
    folder = ch.history_dir(saved)
    assert folder.is_dir()
    result = conti.trash(saved)
    assert not folder.exists() and not saved.exists()
    assert (home / result["trash"] / "workspace" / "conti_history" / saved.stem / "0001.json").is_file()


def test_cli_history_name_restore_diff(saved, home, monkeypatch):
    rel = saved.relative_to(home).as_posix()
    monkeypatch.chdir(home)
    assert conti.main(["history", rel])[0]["kind"] == "created"
    named = conti.main(["history-name", rel, "--label", "기획 완료", "--expect-hash", conti.content_hash(saved)])
    assert named["name"] == "기획 완료"
    conti.update_cut(saved, 1, {"line": "바꿈"})
    assert conti.main(["history-diff", rel, "--a", "2"])["changes"]
    assert conti.main(["history-restore", rel, "--n", "2"])["status"] == "restored"
    assert fm_of(saved)["cuts"][0]["line"] == "4단계 다 먹을 수 있을까?"


# --- 수정 1차: 손편집 보존 · 동시성 · 무변경 편집 · 큰 번호 ------------------------------------

def hand_edit(path, line):
    fm, _ = refstore.read_md(path)
    fm["cuts"][0]["line"] = line
    conti._write_conti(path, path.read_bytes(), conti.retime(fm))


def test_restore_after_hand_edit_keeps_the_hand_edit_in_history(saved):
    hand_edit(saved, "손으로 고친 대사")
    ch.restore(saved, 1, None, now=T0 + 5)
    kinds = [v["kind"] for v in versions(saved)]
    assert kinds == ["restored", "external", "created"]
    assert ch.get_version(saved, 2)["state"]["cuts"][0]["line"] == "손으로 고친 대사"


def test_name_after_hand_edit_records_external_first(saved):
    hand_edit(saved, "손으로 고친 대사")
    ch.name(saved, "촬영 후", None, now=T0 + 5)
    assert [v["kind"] for v in versions(saved)] == ["named", "external", "created"]


def test_update_cut_with_bad_number_records_nothing(saved):
    hand_edit(saved, "손으로 고친 대사")
    with pytest.raises(ScriptError) as e:
        conti.update_cut(saved, 99, {"line": "x"}, now=T0 + 5)
    assert e.value.code == "cut_not_found" and len(versions(saved)) == 1


def test_concurrent_updates_with_same_hash_one_wins(saved):
    import threading
    h = conti.content_hash(saved)
    outcomes = []
    gate = threading.Barrier(2)

    def worker(text):
        gate.wait()
        try:
            conti.update_cut(saved, 1, {"line": text}, expected_hash=h, now=T0 + 5)
            outcomes.append("ok")
        except ScriptError as e:
            outcomes.append(e.code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("가", "나")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conti_changed", "ok"]
    assert [v["n"] for v in versions(saved)] == [2, 1]
    assert not [p for p in ch.history_dir(saved).iterdir() if p.suffix == ".tmp"]


def test_noop_auto_edit_only_touches_last_touch(saved):
    edit_line(saved, "바뀜", T0 + 10)
    edit_line(saved, "바뀜", T0 + 10 + 200)             # 같은 값: 새 버전 없음
    edit_line(saved, "다시", T0 + 10 + 200 + 280)       # 앞 no-op의 시각에서 5분 안 → 같은 묶음
    assert [v["n"] for v in versions(saved)] == [2, 1]
    assert ch.get_version(saved, 2)["last_touch"] == T0 + 490


def test_version_numbers_beyond_9999_sort_numerically(saved):
    folder = ch.history_dir(saved)
    src = folder / "0001.json"
    for n in (9999, 10000):
        data = json.loads(src.read_text("utf-8"))
        data["n"] = n
        (folder / f"{n:04d}.json").write_text(json.dumps(data), encoding="utf-8")
    assert [v["n"] for v in versions(saved)] == [10000, 9999, 1]
    assert ch.record(saved, fm_of(saved), "named", name="x", now=T0 + 1)["n"] == 10001
