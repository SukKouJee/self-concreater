"""conti_history 버전 파일의 바이트와 오류 코드를 고정한다(S3 Task 8 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같아야 한다.
- 버전 JSON: `json.dumps(ensure_ascii=False, indent=1)`, 줄바꿈 LF 고정(Windows에서도 CRLF 변환 금지), 끝 줄바꿈 없음.
- 폴더 이름은 콘티 파일 stem. 임시 파일(.NNNN.json.pid.tid.tmp)은 남기지 않는다.
- 읽기 캐시는 (수정 시각 ns, 크기)가 같아도 다른 작업 폴더의 파일을 섞지 않는다.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path

import pytest

from scripts import conti, conti_history as ch
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def stable(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count(1)
    monkeypatch.setattr(conti, "new_cut_id", lambda: f"id{next(counter):06d}")


@pytest.fixture
def saved(draft_md, home):
    return home / conti.save(draft_md, conti_file(home), now=T0)["path"]


def folder(home: Path, saved: Path) -> Path:
    return home / "workspace" / "conti_history" / saved.stem


def fixed_at(raw: bytes) -> str:
    """`at`(현지 시간대)만 고정해 다시 쓴 JSON의 sha256."""
    data = json.loads(raw)
    data["at"] = "T"
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")).hexdigest()


def test_version_file_bytes_are_indent1_lf_without_trailing_newline(saved, home):
    conti.update_cut(saved, 1, {"line": "첫 수정 🍜"}, now=T0 + 10)
    hist = folder(home, saved)
    assert sorted(p.name for p in hist.iterdir()) == ["0001.json", "0002.json"]
    for p in hist.iterdir():
        raw = p.read_bytes()
        assert b"\r" not in raw and not raw.endswith(b"\n")
        assert raw.startswith(b'{\n "n": ')
        assert raw == json.dumps(json.loads(raw), ensure_ascii=False, indent=1).encode("utf-8")
    assert "첫 수정 🍜".encode() in (hist / "0002.json").read_bytes()    # 이모지·한글 그대로
    assert [fixed_at((hist / n).read_bytes()) for n in ("0001.json", "0002.json")] == GOLDEN_SHA


def test_touched_noop_rewrites_same_file_bytes_with_new_last_touch(saved, home):
    conti.update_cut(saved, 1, {"line": "바뀜"}, now=T0 + 10)
    conti.update_cut(saved, 1, {"line": "바뀜"}, now=T0 + 210)
    raw = (folder(home, saved) / "0002.json").read_bytes()
    data = json.loads(raw)
    assert data["last_touch"] == T0 + 210 and data["n"] == 2
    assert raw == json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    assert sorted(p.name for p in folder(home, saved).iterdir()) == ["0001.json", "0002.json"]


def test_history_folder_is_conti_stem(saved, home):
    assert saved.stem.endswith("-conti")
    assert (home / "workspace" / "conti_history" / saved.stem / "0001.json").is_file()
    assert [p.name for p in (home / "workspace" / "conti_history").iterdir()] == [saved.stem]


@pytest.mark.parametrize("call, code", [
    (lambda p: ch.get_version(p, 99), "version_not_found"),
    (lambda p: ch.name(p, "", None), "invalid_name"),
    (lambda p: ch.name(p, "x" * 31, None), "invalid_name"),
    (lambda p: ch.name(p, "이름", "0" * 64), "conti_changed"),
    (lambda p: ch.restore(p, 1, "0" * 64), "conti_changed"),
    (lambda p: ch.restore(p, 99, None), "version_not_found"),
])
def test_error_codes(saved, call, code):
    with pytest.raises(ScriptError) as e:
        call(saved)
    assert e.value.code == code


def test_missing_conti_is_conti_not_found(home):
    key = "drafts/none-conti.md"
    for call in (lambda: ch.name(key, "이름", None), lambda: ch.restore(key, 1, None)):
        with pytest.raises(ScriptError) as e:
            call()
        assert (e.value.code, e.value.message) == ("conti_not_found", f"콘티가 없습니다: {key}")


def test_corrupt_version_file_is_history_corrupt(saved, home):
    (folder(home, saved) / "0001.json").write_bytes(b"{not json")
    with pytest.raises(ScriptError) as e:
        ch.list_versions(saved)
    assert e.value.code == "history_corrupt" and "0001.json" in e.value.message


def test_invalid_utf8_version_file_is_history_corrupt(saved, home):
    (folder(home, saved) / "0001.json").write_bytes(b"\xff\xfe{}")
    with pytest.raises(ScriptError) as e:
        ch.get_version(saved, 1)
    assert e.value.code == "history_corrupt" and "0001.json" in e.value.message


@pytest.mark.parametrize("call", [
    lambda p: ch.list_versions(p),
    lambda p: ch.get_version(p, 1),
    lambda p: ch.name(p, "이름", None),
    lambda p: ch.restore(p, 1, None),
])
def test_conti_path_outside_work_folder_is_bad_path(saved, home, tmp_path_factory, call):
    """작업 폴더 밖 콘티 경로를 직접 부르면(이전엔 경로 이름만 봤다) 이제 bad_path로 거절한다."""
    outside = tmp_path_factory.mktemp("outside") / "drafts" / "x-conti.md"
    outside.parent.mkdir()
    outside.write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(ScriptError) as e:
        call(outside)
    assert e.value.code == "bad_path"


def test_cache_does_not_mix_work_folders_with_equal_stamp(saved, home, tmp_path_factory, monkeypatch):
    """두 작업 폴더에 같은 키·같은 크기·같은 수정 시각의 버전 파일이 있어도 서로 섞이지 않는다."""
    path = folder(home, saved) / "0001.json"
    data = json.loads(path.read_bytes())
    data["name"] = "aaaa"
    path.write_bytes(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    other = tmp_path_factory.mktemp("other")
    twin = other / path.relative_to(home)
    twin.parent.mkdir(parents=True)
    data["name"] = "bbbb"
    twin.write_bytes(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    st = path.stat()
    os.utime(twin, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert twin.stat().st_size == st.st_size and twin.stat().st_mtime_ns == st.st_mtime_ns
    key = saved.relative_to(home).as_posix()
    assert ch.get_version(key, 1)["name"] == "aaaa"
    monkeypatch.setenv("YT_COACH_HOME", str(other))
    assert ch.get_version(key, 1)["name"] == "bbbb"
    monkeypatch.setenv("YT_COACH_HOME", str(home))
    assert ch.get_version(key, 1)["name"] == "aaaa"


GOLDEN_SHA = [
    "855827f4ac9a1af1ddc1a649fbd8331972d556ca833c926d866bdc6dda4fd5a6",
    "1b070cbeabc49d497121a26c2f47cab3d116bc59e6afef43c820186fae9d2481",
]
