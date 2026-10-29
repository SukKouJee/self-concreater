"""status-chip.js 순수 함수 검증: 머리말 작업 상태 칩 문구, 끝난 작업 찾기, 결과로 가는 주소, 폴링 계획 (node 없으면 skip)."""
import json
import re
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
CHIP = STATIC_DIR / "status-chip.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Job snapshot like GET /api/jobs/current (jobs.Job.snapshot) — only the fields the chip reads.
HELPERS = """
const snap = (kind, status, extra = {}) => ({ id: extra.id || `${kind}-1`, kind, status, category: "여행",
  items: extra.items || [{ id: extra.item || "x", state: status === "done" ? "done" : "running" }],
  summary: extra.summary || null, message: extra.message || null });
"""


def run_js(body: str):
    code = (
        f"{HELPERS}\nimport({json.dumps(CHIP.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ {body} }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def js(expr: str):
    return run_js(f"return {expr};")


def backend_kinds():
    """Every kind the server starts (KIND constants and literal jobs.start kinds)."""
    kinds = set()
    for path in (STATIC_DIR.parent).glob("api_*.py"):
        text = path.read_text(encoding="utf-8")
        kinds.update(re.findall(r'^[A-Z_]*KIND = "([a-z_]+)"', text, re.M))
        kinds.update(re.findall(r'jobs\.start\("([a-z_]+)"', text))
    return kinds


def test_every_backend_kind_has_a_label_except_the_help_chat():
    kinds = js("Object.keys(m.JOB_KINDS)")
    assert set(kinds) == backend_kinds() - {"help_chat"}
    assert js("m.POLL_KINDS") == kinds
    labels = js("m.JOB_KINDS")
    for kind, info in labels.items():
        assert info["label"].endswith("중"), kind
        assert info["name"], kind
        # non-Claude work (files, ffmpeg-like processing) does not say "Claude가"
        assert info["label"].startswith("Claude가") == info["claude"], kind
    assert labels["motif"]["label"] == "Claude가 모티브 카드를 만드는 중"
    assert labels["conti"]["label"] == "Claude가 콘티를 만드는 중"
    assert not labels["footage"]["claude"] and not labels["srtfit"]["claude"] and not labels["caption_video"]["claude"]
    assert labels["refs_describe"]["label"] == "Claude가 사진 설명을 만드는 중"


def test_elapsed_text():
    assert js("m.elapsedText(0)") == "0초"
    assert js("m.elapsedText(18400)") == "18초"
    assert js("m.elapsedText(65000)") == "1분 5초"
    assert js("m.elapsedText(120000)") == "2분"
    assert js("m.elapsedText(3725000)") == "1시간 2분"
    assert js("m.elapsedText(-5)") == "0초"


def test_chip_text_elapsed():
    # one running job: label and the seconds since the chip first saw it (snapshots have no start time)
    r = run_js("""
      const jobs = m.trackJobs([], [snap("motif", "running")], 1000, "#/motif");
      return [m.chipText(jobs, 19000), m.chipText(jobs, 19000, { short: true }), jobs[0].since, jobs[0].origin];
    """)
    assert r == ["Claude가 모티브 카드를 만드는 중 · 18초", "작업 중 · 18초", 1000, "#/motif"]
    # a later snapshot of the same job keeps the first sighting
    r = run_js("""
      const a = m.trackJobs([], [snap("conti", "running")], 1000, "#/video/a/conti");
      const b = m.trackJobs(a, [snap("conti", "running")], 61000, "#/home");
      return [m.chipText(b, 62000), b[0].origin];
    """)
    assert r == ["Claude가 콘티를 만드는 중 · 1분 1초", "#/video/a/conti"]
    # nothing running: empty text
    assert js("m.chipText([], 5000)") == ""
    # a job waiting for the user's answer says so instead of a time
    r = run_js("""
      const jobs = m.trackJobs([], [snap("collect", "awaiting")], 0, "#/add");
      return [m.chipText(jobs, 9000), m.chipText(jobs, 9000, { short: true })];
    """)
    assert r == ["레퍼런스 분석 · 확인할 게 있어요", "확인할 게 있어요"]


def test_status_chip_multiple_jobs():
    # collect + footage at once (plan Review Focus 5): both tracked, the chip groups them
    r = run_js("""
      const jobs = m.trackJobs([], [snap("collect", "running"), snap("footage", "running", { summary: { path: "drafts/a-conti.md" } })], 0, "#/home");
      return [jobs.map((j) => j.kind), m.chipText(jobs, 5000), m.chipText(jobs, 5000, { short: true }),
              m.chipItems(jobs, 5000)];
    """)
    kinds, text, short, items = r
    assert kinds == ["collect", "footage"]
    assert text == "작업 2개" and short == "작업 2개"
    assert items == [
        {"id": "collect-1", "text": "Claude가 레퍼런스를 분석하는 중 · 5초", "href": "#/refs", "link": "보기"},
        {"id": "footage-1", "text": "촬영 영상 처리하는 중 · 5초", "href": "#/video/a/conti", "link": "보기"},
    ]
    # two jobs of the same kind (two contis at once) stay apart by job id
    r = run_js("""
      const a = m.trackJobs([], [snap("conti", "running", { id: "c1", item: "drafts/a.md" })], 0, "");
      const b = m.trackJobs(a, [snap("conti", "running", { id: "c2", item: "drafts/b.md" })], 1000, "");
      return [b.map((j) => j.id), m.chipText(b, 2000)];
    """)
    assert r == [["c1", "c2"], "작업 2개"]


def test_track_ignores_unknown_kinds_and_jobs_never_seen_running():
    r = run_js("""
      return m.trackJobs([], [snap("help_chat", "running"), snap("motif", "done"), null, { kind: "motif" }], 0, "");
    """)
    assert r == []


def test_finished_jobs_detected():
    r = run_js("""
      const a = m.trackJobs([], [snap("motif", "running"), snap("typify", "running"), snap("diagnose", "running", { item: "drafts/a.md" })], 0, "");
      const b = m.trackJobs(a, [snap("motif", "done", { items: [{ id: "여행", state: "awaiting" }] }),
        snap("typify", "failed", { message: "Claude 응답이 비었어요" }), snap("diagnose", "running", { item: "drafts/a.md" })], 3000, "");
      return [m.finishedJobs(a, b).map((j) => [j.kind, j.outcome]), b.map((j) => [j.kind, j.status])];
    """)
    finished, tracked = r
    # done with a preview to save = "확인할 게 있어요"; failed is reported; still running is not
    assert finished == [["motif", "check"], ["typify", "failed"]]
    assert tracked == [["motif", "done"], ["typify", "failed"], ["diagnose", "running"]]
    # finished records leave on the next update (no second toast)
    r = run_js("""
      const a = m.trackJobs([], [snap("motif", "running")], 0, "");
      const b = m.trackJobs(a, [snap("motif", "done")], 1, "");
      const c = m.trackJobs(b, [snap("motif", "done")], 2, "");
      const d = m.trackJobs(c, [], 3, "");
      return [m.finishedJobs(a, b).map((j) => j.outcome), c.length, m.finishedJobs(b, c).length, d.length];
    """)
    assert r == [["done"], 0, 0, 0]


def test_awaiting_answer_is_told_once_per_job():
    r = run_js("""
      const a = m.trackJobs([], [snap("collect", "running")], 0, "#/add");
      const b = m.trackJobs(a, [snap("collect", "awaiting")], 1, "");
      const c = m.trackJobs(b, [snap("collect", "running")], 2, "");
      const d = m.trackJobs(c, [snap("collect", "awaiting")], 3, "");
      const e = m.trackJobs(d, [snap("collect", "done")], 4, "");
      return [m.finishedJobs(a, b), m.finishedJobs(c, d), m.finishedJobs(d, e)].map((l) => l.map((j) => j.outcome));
    """)
    # awaiting -> done: the user answered (and finished it) themselves; no toast
    assert r == [["awaiting"], [], []]
    # first seen while already awaiting: nothing to announce (it was not seen finishing)
    assert run_js("""
      const a = m.trackJobs([], [snap("collect", "awaiting")], 0, "");
      const b = m.trackJobs(a, [snap("collect", "awaiting")], 1, "");
      return m.finishedJobs(a, b);
    """) == []


def test_result_href_per_kind():
    cases = {
        'snap("conti", "done", { item: "drafts/2026-10-29 부루마블.md" })': "#/video/2026-10-29%20%EB%B6%80%EB%A3%A8%EB%A7%88%EB%B8%94/conti",
        'snap("conti_merge", "done", { item: "drafts/a-conti.md" })': "#/video/a/conti",
        'snap("conti_refs", "done", { item: "drafts/a-conti.md", summary: { path: "drafts/a-conti.md" } })': "#/video/a/conti",
        'snap("srtfit", "done", { item: "drafts/sub/a-conti.md" })': "#/video/sub%2Fa/conti",
        'snap("caption_video", "done", { item: "drafts/a-conti.md" })': "#/video/a/conti",
        'snap("footage", "done", { item: "f1", summary: { path: "drafts/a-conti.md" } })': "#/video/a/conti",
        'snap("footage_clips", "done", { item: "c1", summary: { path: "drafts/a-conti.md" } })': "#/video/a/conti",
        'snap("polish", "done", { item: "drafts/a.md" })': "#/video/a/plan",
        'snap("diagnose", "done", { item: "drafts/a.md" })': "#/video/a/diagnose",
        'snap("diagnose", "done", { item: "mine/my-video.md" })': "#/diagnose/mine%2Fmy-video.md",
        'snap("motif", "done")': "#/motif",
        'snap("typify", "done")': "#/typify",
        'snap("draft", "done")': "#/draft/new",
        'snap("mission", "done")': "#/mission",
        'snap("mission_feedback", "done")': "#/mission",
        'snap("review", "done")': "#/report/review",
        'snap("library_summary", "done")': "#/refs?tab=library",
        'snap("collect", "done")': "#/refs",
        'snap("collect", "awaiting")': "#/add",
        'snap("refs_describe", "done")': "#/refs",
    }
    for expr, href in cases.items():
        # tracked while running, then the final status (awaiting/done) decides the screen
        got = run_js(
            f"const s = {expr}; const t = m.trackJobs([], [Object.assign({{}}, s, {{ status: 'running' }})], 0, '')[0];"
            f" t.status = s.status; return m.resultHref(t);")
        assert got == href, expr
    # photo descriptions go back to the conti tab they were started from
    r = run_js("""
      const t = m.trackJobs([], [snap("refs_describe", "running")], 0, "#/video/a%2Fb/conti")[0];
      const u = m.trackJobs([], [snap("refs_describe", "running")], 0, "#/video/a%2Fb")[0];
      return [m.resultHref(t), m.resultHref(u)];
    """)
    assert r == ["#/video/a%2Fb/conti", "#/video/a%2Fb/conti"]


def test_finish_toast_text_and_link():
    r = run_js("""
      const a = m.trackJobs([], [snap("conti", "running", { item: "drafts/a.md" }), snap("typify", "running"),
        snap("collect", "running"), snap("motif", "running"), snap("footage", "running", { summary: { path: "drafts/a-conti.md" } })], 0, "");
      const b = m.trackJobs(a, [snap("conti", "done", { item: "drafts/a.md", items: [{ id: "drafts/a.md", state: "awaiting" }] }),
        snap("typify", "failed", { message: "Claude 응답이 비었어요" }), snap("collect", "awaiting"), snap("motif", "stopped"),
        snap("footage", "done", { summary: { path: "drafts/a-conti.md" } })], 1, "");
      return m.finishedJobs(a, b).map((j) => m.finishToast(j, "#/home"));
    """)
    assert r == [
        {"text": "콘티 만들기 끝났어요. 확인할 게 있어요", "kind": "ok", "href": "#/video/a/conti", "link": "결과 보기"},
        {"text": "유형 나누기 못 끝냈어요: Claude 응답이 비었어요", "kind": "error", "href": "#/typify", "link": "보러 가기"},
        {"text": "레퍼런스 분석 · 확인할 게 있어요", "kind": "info", "href": "#/add", "link": "결과 보기"},
        {"text": "모티브 카드 뽑기 멈췄어요", "kind": "info", "href": "#/motif", "link": "보러 가기"},
        {"text": "촬영 영상 처리 끝났어요", "kind": "ok", "href": "#/video/a/conti", "link": "결과 보기"},
    ]
    # already on the result screen: the page shows (and toasts) the result itself -> no chip toast
    r = run_js("""
      const a = m.trackJobs([], [snap("motif", "running"), snap("conti", "running", { item: "drafts/a.md" })], 0, "");
      const b = m.trackJobs(a, [snap("motif", "done"), snap("conti", "done", { item: "drafts/a.md" })], 1, "");
      const [motif, conti] = m.finishedJobs(a, b);
      return [m.finishToast(motif, "#/motif"), m.finishToast(conti, "#/video/a"), m.finishToast(conti, "#/video/a/conti"),
              m.finishToast(conti, "#/video/a/plan").href];
    """)
    assert r == [None, None, None, "#/video/a/conti"]


def test_due_kinds_polls_all_slowly_and_active_fast():
    # first call / slow sweep due: every kind
    r = run_js("return m.dueKinds([], 20000, 0, 15000);")
    assert r["sweep"] is True and r["kinds"] == js("m.POLL_KINDS")
    # between sweeps: only kinds with a running/awaiting job (each once)
    r = run_js("""
      const jobs = m.trackJobs([], [snap("conti", "running", { id: "c1" }), snap("conti", "running", { id: "c2" }), snap("motif", "awaiting")], 0, "");
      return m.dueKinds(jobs, 20000, 10000, 15000);
    """)
    assert r == {"sweep": False, "kinds": ["conti", "motif"]}
    assert run_js("return m.dueKinds([], 20000, 10000, 15000);") == {"sweep": False, "kinds": []}
    assert js("m.POLL_MS") == {"fast": 1500, "slow": 15000}


def test_chip_module_is_safe_and_wired():
    text = CHIP.read_text(encoding="utf-8")
    assert "innerHTML" not in text and "insertAdjacentHTML" not in text
    assert 'from "./dom.js"' in text and 'from "./video-utils.js"' in text   # node-loadable relative imports
    assert "visibilitychange" in text and "document.hidden" in text           # paused while the tab is hidden
    assert '"ytc:job"' in text                                                # snapshots seen by pages
    assert "aria-expanded" in text and "Escape" in text                       # the job list is a disclosure
    # page snapshots (every second) must not push the chip's own poll away; a hung request is given up
    assert "if (S.timer && S.due <= due) return;" in text and "ctl.abort()" in text
    api = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    assert '"ytc:job"' in api
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'from "/static/status-chip.js"' in app and "createStatusChip(" in app
    assert "jobStarted" in app
