"""자막 영상(Remotion) 렌더: 하위 프로세스는 가짜로 바꾸고, 실제 렌더 통합 테스트는 설치돼 있을 때만 돈다."""
import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts import captions, conti
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home, with_cuts  # noqa: F401 - fixtures
from tests.test_cuts import sample_video  # noqa: F401 - fixture

REMOTION_DIR = Path(__file__).resolve().parent.parent / "remotion"
WIN = sys.platform == "win32"


@pytest.fixture
def saved(home, draft_md):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home))["path"]


def rel(home, path):
    return path.relative_to(home).as_posix()


def tmp_leftovers(home):
    return list((home / "workspace" / "tmp").glob("captions_*"))


class FakeProc:
    """Popen 대역: stdout 줄을 차례로 내주고, hang이면 terminate()/kill() 전까지 끝나지 않는다."""

    def __init__(self, lines, returncode=0, hang=False, kill_needed=False):
        self.pid = 4242
        self._q: queue.Queue = queue.Queue()
        self._rc = returncode
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._kill_needed = kill_needed
        self._done = threading.Event()
        self.stdout = self
        for ln in lines:
            self._q.put(ln)
        if not hang:
            self._finish()

    def _finish(self, rc=None):
        self.returncode = self._rc if rc is None else rc
        self._q.put("")
        self._done.set()

    def readline(self):
        return self._q.get()

    def close(self):
        pass

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self._kill_needed:
            self._finish(-15)

    def kill(self):
        self.killed = True
        self._finish(-9)


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})
    monkeypatch.setattr(captions.shutil, "which", lambda name: name)


@pytest.fixture
def popen(monkeypatch):
    """Popen 호출을 기록하고 factory가 만든 FakeProc을 돌려준다. 성공하면 출력 mp4를 만든다."""

    class Recorder:
        calls: list = []
        factory = staticmethod(lambda cmd: FakeProc(["Rendered 1/3\n", "Rendered 3/3\n"]))
        write_output = True
        proc = None
        props = None

    def fake_popen(cmd, **kwargs):
        Recorder.calls.append((cmd, kwargs))
        Recorder.props = json.loads(Path(cmd[6].split("=", 1)[1]).read_text("utf-8"))
        proc = Recorder.factory(cmd)
        Recorder.proc = proc
        if Recorder.write_output and proc.returncode == 0:
            Path(cmd[5]).write_bytes(b"MP4DATA")
        return proc

    Recorder.calls = []
    monkeypatch.setattr(captions.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(captions.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    return Recorder


# --- caption_blocks --------------------------------------------------------------------------

def test_caption_blocks_matches_srt_export_for_planned_times(saved, home):
    calc = conti.caption_blocks(rel(home, saved))
    srt = conti.export_srt(rel(home, saved))
    assert len(calc["blocks"]) == srt["count"] == 3
    assert calc["blocks"][0] == [0, 2000, "4단계 도전"] and calc["duration_ms"] == 8000
    assert calc["retime"] is None and calc["video"] is None
    body = (home / srt["path"]).read_bytes().decode("utf-8")
    for a, b, text in calc["blocks"]:
        assert f"{conti._srt_time(a)} --> {conti._srt_time(b)}\r\n{text}\r\n" in body


def test_caption_blocks_with_video_matches_srt_fit(saved, home, sample_video):
    calc = conti.caption_blocks(rel(home, saved), "caption", sample_video)
    srt = conti.export_srt(rel(home, saved), "caption", sample_video)
    assert len(calc["blocks"]) == srt["count"] and calc["retime"] == srt["retime"]
    assert calc["video"] == sample_video and calc["duration_ms"] / 1000 == pytest.approx(srt["duration_sec"])


def test_caption_blocks_rejects_bad_track(saved, home):
    with pytest.raises(ScriptError) as e:
        conti.caption_blocks(rel(home, saved), "sfx")
    assert e.value.code == "bad_request"


# --- check_setup / setup -----------------------------------------------------------------------

def test_check_setup_reports_missing_node(monkeypatch):
    monkeypatch.setattr(captions.shutil, "which", lambda name: None)
    with pytest.raises(ScriptError) as e:
        captions.check_setup()
    assert e.value.code == "remotion_missing"
    assert e.value.message == ("움직이는 자막을 만들려면 처음 한 번 설치가 필요합니다: ytc captions setup (Node 18 이상)")


def _install_fake_cli(monkeypatch, tmp_path, major):
    monkeypatch.setattr(captions.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(captions, "_node_major", lambda node: major)
    monkeypatch.setattr(captions, "REMOTION_DIR", tmp_path)
    cli = tmp_path.joinpath(*captions.REMOTION_CLI_REL)
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")


def test_check_setup_reports_missing_node_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(captions.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(captions, "_node_major", lambda node: 24)
    monkeypatch.setattr(captions, "REMOTION_DIR", tmp_path)
    with pytest.raises(ScriptError) as e:
        captions.check_setup()
    assert e.value.code == "remotion_missing"
    _install_fake_cli(monkeypatch, tmp_path, 24)
    assert captions.check_setup()["ready"] is True


@pytest.mark.parametrize("major,ok", [(16, False), (17, False), (18, True), (24, True), (None, True)])
def test_check_setup_requires_node_18(monkeypatch, tmp_path, major, ok):
    _install_fake_cli(monkeypatch, tmp_path, major)
    if ok:
        assert captions.check_setup()["ready"] is True
    else:
        with pytest.raises(ScriptError) as e:
            captions.check_setup()
        assert e.value.code == "remotion_missing" and "Node 18" in e.value.message


def test_node_major_parses_version(monkeypatch):
    monkeypatch.setattr(captions.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "v24.22.0\n", ""))
    assert captions._node_major("node") == 24

    def boom(cmd, **k):
        raise OSError("x")

    monkeypatch.setattr(captions.subprocess, "run", boom)
    assert captions._node_major("node") is None


def test_setup_uses_npm_ci_with_lockfile(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "added 5 packages\n", "")

    monkeypatch.setattr(captions.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(captions.subprocess, "run", fake_run)
    monkeypatch.setattr(captions, "REMOTION_DIR", tmp_path)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    out = captions.setup()
    assert seen[0][0][1] == "ci" and seen[0][1]["cwd"] == tmp_path and out["ok"] is True
    (tmp_path / "package-lock.json").unlink()
    captions.setup()
    assert seen[1][0][1] == "install"


def test_setup_failure_reports_last_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(captions.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(captions, "REMOTION_DIR", tmp_path)
    monkeypatch.setattr(captions.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "npm ERR! boom\n"))
    with pytest.raises(ScriptError) as e:
        captions.setup()
    assert e.value.code == "setup_failed" and "boom" in e.value.message


# --- build_props -------------------------------------------------------------------------------

def test_build_props_planned_shorts(saved, home):
    props = captions.build_props(rel(home, saved))
    assert props["fps"] == 30 and props["width"] == 1080 and props["height"] == 1920
    assert props["durationInFrames"] == 240 and props["background"] == "#00FF00"
    assert props["captions"] == [{"start": 0.0, "end": 2.0, "text": "4단계 도전"},
                                 {"start": 4.0, "end": 6.0, "text": "순한맛"},
                                 {"start": 6.0, "end": 8.0, "text": "다음은 불닭"}]


def _patch_calc(monkeypatch, **patch):
    real = conti.caption_blocks

    def fake(*a, **k):
        r = real(*a, **k)
        r.update(patch)
        return r

    monkeypatch.setattr(conti, "caption_blocks", fake)


def test_build_props_long_format_is_landscape(saved, home, monkeypatch):
    real = conti.caption_blocks

    def long_fm(*a, **k):
        r = real(*a, **k)
        r["fm"] = {**r["fm"], "format": "long"}
        return r

    monkeypatch.setattr(conti, "caption_blocks", long_fm)
    props = captions.build_props(rel(home, saved))
    assert (props["width"], props["height"]) == (1920, 1080)


def test_build_props_duration_rounds_up(saved, home, monkeypatch):
    _patch_calc(monkeypatch, duration_ms=1001)
    assert captions.build_props(rel(home, saved))["durationInFrames"] == 31


@pytest.mark.parametrize("w,h,expected", [
    (1920, 1080, (1920, 1080)), (3840, 2160, (1920, 1080)), (1080, 1920, (1080, 1920)),
    (720, 1280, (1080, 1920)), (1000, 1000, (1080, 1080)),
])
def test_build_props_size_follows_edited_video(saved, home, monkeypatch, w, h, expected):
    monkeypatch.setattr(captions, "_video_size", lambda path: (w, h))
    _patch_calc(monkeypatch, video=Path("edited.mp4"))
    props = captions.build_props(rel(home, saved))
    assert (props["width"], props["height"]) == expected


def test_build_props_with_real_video_uses_its_ratio(saved, home, sample_video):
    import cv2
    props = captions.build_props(rel(home, saved), video=sample_video)
    cap = cv2.VideoCapture(str(sample_video))
    w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    assert min(props["width"], props["height"]) == 1080
    assert props["width"] / props["height"] == pytest.approx(w / h, abs=0.01)


# --- render (가짜 하위 프로세스) ---------------------------------------------------------------

def test_render_runs_npx_in_remotion_dir_and_replaces_output(saved, home, ready, popen):
    progress = []
    out = captions.render(rel(home, saved), progress=lambda n, m: progress.append((n, m)))
    [(cmd, kwargs)] = popen.calls
    assert Path(cmd[0]).stem == "node" and Path(cmd[1]).name == "remotion-cli.js"
    assert cmd[2:5] == ["render", "src/index.jsx", "Captions"]
    assert cmd[6].startswith("--props=") and kwargs["cwd"] == REMOTION_DIR
    assert kwargs["env"]["NO_COLOR"] == "1" and kwargs["env"]["FORCE_COLOR"] == "0"
    assert ("start_new_session" in kwargs) == (not WIN)
    assert Path(cmd[5]).resolve().parent == (home / "workspace" / "tmp").resolve()
    assert Path(cmd[5]).name.startswith("captions_")
    assert progress == [(1, 3), (3, 3)]
    expected = "drafts/" + saved.name.replace("-conti.md", "-captions.mp4")
    assert out == {"path": expected, "count": 3, "duration_sec": 8.0, "width": 1080, "height": 1920}
    assert (home / expected).read_bytes() == b"MP4DATA"
    assert not tmp_leftovers(home)
    assert popen.props["captions"][0]["text"] == "4단계 도전"


def test_render_overwrites_previous_output(saved, home, ready, popen):
    target = home / "drafts" / saved.name.replace("-conti.md", "-captions.mp4")
    target.write_bytes(b"OLD")
    captions.render(rel(home, saved))
    assert target.read_bytes() == b"MP4DATA"


def test_render_failure_keeps_existing_output_and_reports_last_lines(saved, home, ready, popen):
    target = home / "drafts" / saved.name.replace("-conti.md", "-captions.mp4")
    target.write_bytes(b"OLD")
    popen.factory = staticmethod(lambda cmd: FakeProc([f"line{i}\n" for i in range(1, 9)], returncode=1))
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved))
    assert e.value.code == "render_failed" and e.value.message.startswith("자막 영상을 만들지 못했습니다: ")
    assert "line8" in e.value.message and "line3" not in e.value.message
    assert target.read_bytes() == b"OLD"
    assert not tmp_leftovers(home)


def test_render_fails_when_no_output_file_even_with_exit_zero(saved, home, ready, popen):
    popen.write_output = False
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved))
    assert e.value.code == "render_failed"
    assert not tmp_leftovers(home)


def test_render_cancel_terminates_only_that_process(saved, home, ready, popen):
    popen.factory = staticmethod(lambda cmd: FakeProc(["Rendered 1/3\n"], hang=True))
    flag = {"stop": False}

    def progress(n, m):
        flag["stop"] = True

    with pytest.raises(captions.RenderCancelled):
        captions.render(rel(home, saved), progress=progress, cancel=lambda: flag["stop"])
    assert popen.proc.terminated and not popen.proc.killed
    assert not tmp_leftovers(home)


def test_render_cancel_kills_after_grace_period(saved, home, ready, popen, monkeypatch):
    monkeypatch.setattr(captions, "TERMINATE_GRACE_SEC", 0.05)
    popen.factory = staticmethod(lambda cmd: FakeProc([], hang=True, kill_needed=True))
    with pytest.raises(captions.RenderCancelled):
        captions.render(rel(home, saved), cancel=lambda: True)
    assert popen.proc.terminated and popen.proc.killed


def test_render_never_kills_by_image_name(saved, home, ready, popen, monkeypatch):
    runs = []
    monkeypatch.setattr(captions.subprocess, "run",
                        lambda cmd, **k: runs.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    popen.factory = staticmethod(lambda cmd: FakeProc([], hang=True))
    with pytest.raises(captions.RenderCancelled):
        captions.render(rel(home, saved), cancel=lambda: True)
    for cmd in runs:
        flat = " ".join(map(str, cmd)).lower()
        assert "/im" not in flat and "pkill" not in flat and "stop-process" not in flat
        assert "/pid" in flat and "4242" in flat


def test_render_timeout_stops_process(saved, home, ready, popen):
    popen.factory = staticmethod(lambda cmd: FakeProc([], hang=True))
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved), timeout=0.2)
    assert e.value.code == "render_timeout" and popen.proc.terminated
    assert not tmp_leftovers(home)


def test_render_without_captions_never_starts_process(home, draft_md, ready, popen):
    data = with_cuts(c0={"caption": ""}, c2={"caption": ""}, c3={"caption": ""})
    path = home / conti.save(draft_md, conti_file(home, data))["path"]
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, path))
    assert e.value.code == "no_captions" and e.value.message == "내보낼 자막이 없습니다"
    assert popen.calls == []


class TerminateRaises(FakeProc):
    def terminate(self):
        self.terminated = True
        raise PermissionError("access denied")


def test_cancel_surfaces_even_when_terminate_raises(saved, home, ready, popen, monkeypatch):
    monkeypatch.setattr(captions, "TERMINATE_GRACE_SEC", 0.05)
    popen.factory = staticmethod(lambda cmd: TerminateRaises([], hang=True))
    with pytest.raises(captions.RenderCancelled):
        captions.render(rel(home, saved), cancel=lambda: True)
    assert popen.proc.terminated and popen.proc.killed
    assert not tmp_leftovers(home)


def test_timeout_surfaces_even_when_kill_also_raises(saved, home, ready, popen, monkeypatch):
    monkeypatch.setattr(captions, "TERMINATE_GRACE_SEC", 0.05)

    class Stubborn(TerminateRaises):
        def kill(self):
            self.killed = True
            raise PermissionError("access denied")

    popen.factory = staticmethod(lambda cmd: Stubborn([], hang=True))
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved), timeout=0.2)
    assert e.value.code == "render_timeout"


def test_temp_files_removed_after_transient_permission_errors(saved, home, ready, popen, monkeypatch):
    monkeypatch.setattr(captions, "REMOVE_RETRY_SEC", 0)
    real_unlink = Path.unlink
    fails = {"n": 0}

    def flaky(self, missing_ok=False):
        if self.name.startswith("captions_") and fails["n"] < 4:
            fails["n"] += 1
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    popen.factory = staticmethod(lambda cmd: FakeProc([], hang=True))
    with pytest.raises(captions.RenderCancelled):
        captions.render(rel(home, saved), cancel=lambda: True)
    assert fails["n"] == 4
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert not tmp_leftovers(home)


def test_remove_gives_up_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "REMOVE_RETRY_SEC", 0)

    def denied(self, missing_ok=False):
        raise PermissionError("x")

    monkeypatch.setattr(Path, "unlink", denied)
    captions._remove(tmp_path / "captions_x.mp4")        # 예외 없이 돌아온다


def test_wait_timeout_after_output_end_is_render_failed(saved, home, ready, popen, monkeypatch):
    monkeypatch.setattr(captions, "TERMINATE_GRACE_SEC", 0.01)

    class NeverExits(FakeProc):
        def wait(self, timeout=None):
            if self.killed or self.terminated:
                return self.returncode
            raise subprocess.TimeoutExpired("fake", timeout)

    popen.factory = staticmethod(lambda cmd: NeverExits(["Rendered 3/3\n"], returncode=0))
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved))
    assert e.value.code == "render_failed" and popen.proc.terminated
    assert not tmp_leftovers(home)


def test_stop_signals_process_group_off_windows(monkeypatch):
    import signal
    sent = []
    monkeypatch.setattr(captions, "_WIN", False)
    monkeypatch.setattr(captions.os, "killpg", lambda pid, sig: sent.append((pid, sig)), raising=False)
    proc = FakeProc([], hang=True)
    captions._stop(proc)
    assert sent == [(4242, signal.SIGTERM)] and proc.terminated


def test_stop_survives_killpg_error(monkeypatch):
    monkeypatch.setattr(captions, "_WIN", False)

    def boom(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(captions.os, "killpg", boom, raising=False)
    proc = FakeProc([], hang=True)
    captions._stop(proc)
    assert proc.terminated


def test_error_tail_strips_ansi(saved, home, ready, popen):
    popen.factory = staticmethod(lambda cmd: FakeProc(["\x1b[31mError: boom\x1b[39m\n"], returncode=1))
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved))
    assert "\x1b" not in e.value.message and "Error: boom" in e.value.message


def test_render_reports_detect_step_for_edited_video(saved, home, ready, popen, sample_video):
    steps = []
    captions.render(rel(home, saved), video=sample_video, step=steps.append)
    assert steps == ["detect"]


def test_render_requires_setup(saved, home, monkeypatch, popen):
    monkeypatch.setattr(captions.shutil, "which", lambda name: None)
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, saved))
    assert e.value.code == "remotion_missing" and popen.calls == []


def test_render_rejects_non_conti_path(home, draft_md, ready, popen):
    with pytest.raises(ScriptError) as e:
        captions.render(rel(home, draft_md))
    assert e.value.code == "bad_path"


def test_main_cli_dispatch(saved, home, ready, popen):
    assert captions.main(["check"])["ready"] is True
    out = captions.main(["render", rel(home, saved), "--track", "caption"])
    assert out["count"] == 3


# --- 실제 Remotion 렌더(설치돼 있을 때만) --------------------------------------------------------

@pytest.mark.skipif(not (REMOTION_DIR / "node_modules" / "@remotion" / "cli").is_dir() or shutil.which("node") is None,
                    reason="remotion not installed")
def test_real_render_produces_mp4_with_expected_size_and_frames(saved, home, monkeypatch):
    import cv2
    real = conti.caption_blocks

    def three_seconds(*a, **k):                 # 렌더를 짧게: 앞 3초만 쓴다
        r = real(*a, **k)
        r["blocks"] = [[s, min(e, 3000), t] for s, e, t in r["blocks"] if s < 3000]
        r["duration_ms"] = 3000
        return r

    monkeypatch.setattr(conti, "caption_blocks", three_seconds)
    seen = []
    out = captions.render(rel(home, saved), timeout=300, progress=lambda n, m: seen.append((n, m)))
    assert seen and all(0 <= n <= m for n, m in seen)
    mp4 = home / out["path"]
    assert mp4.is_file() and (out["width"], out["height"]) == (1080, 1920)
    cap = cv2.VideoCapture(str(mp4))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    cap.release()
    mp4.unlink(missing_ok=True)
    assert size == (1080, 1920) and frames == 90
    assert not tmp_leftovers(home)
