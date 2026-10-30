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
    assert labels["footage"] == {"label": "Claude가 촬영 영상을 맞추는 중", "name": "촬영 영상 맞추기", "claude": True}
    assert not labels["footage_clips"]["claude"] and not labels["srtfit"]["claude"] and not labels["caption_video"]["claude"]
    assert labels["refs_describe"]["label"] == "Claude가 사진 설명을 만드는 중"
    assert labels["conti_art"] == {"label": "Claude가 장면 그림을 그리는 중", "name": "장면 그림", "claude": True}


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
        {"id": "footage-1", "text": "Claude가 촬영 영상을 맞추는 중 · 5초", "href": "#/video/a/conti", "link": "보기"},
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
        'snap("conti_art", "done", { item: "drafts/a-conti.md", summary: { path: "drafts/a-conti.md" } })': "#/video/a/conti",
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
        {"text": "촬영 영상 맞추기 끝났어요", "kind": "ok", "href": "#/video/a/conti", "link": "결과 보기"},
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
    assert "nextDue({" in text and "ctl.abort()" in text
    # one polite live line beside the chip, outside the element that is re-rendered
    assert '"aria-live": "polite"' in text and "document.body.append(live)" in text
    api = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    assert '"ytc:job"' in api
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'from "/static/status-chip.js"' in app and "createStatusChip(" in app
    assert "jobStarted" in app


def test_fast_poll_only_for_jobs_that_work():
    # m-002: a collect job that only waits for the user's review stays on the slow sweep
    r = run_js("""
      const jobs = m.trackJobs([], [snap("collect", "awaiting", { items: [{ id: "a", state: "awaiting", ask: "review" }, { id: "b", state: "saved" }] }),
        snap("footage", "awaiting", { items: [{ id: "f", state: "awaiting" }, { id: "g", state: "running" }] })], 0, "");
      return [jobs.map((j) => j.working), m.dueKinds(jobs, 20000, 10000, 15000)];
    """)
    assert r == [[False, True], {"sweep": False, "kinds": ["footage"]}]


def test_next_due_keeps_an_earlier_poll_and_pauses_when_hidden():
    # m-009: page snapshots arrive every second; the chip's own poll must still come (no starvation)
    r = run_js("""
      const run = m.trackJobs([], [snap("motif", "running")], 0, "");
      const wait = m.trackJobs([], [snap("collect", "awaiting", { items: [{ id: "a", state: "awaiting" }] })], 0, "");
      const at = (jobs, now, current, hidden = false) => m.nextDue({ jobs, now, lastSweep: 1000, hidden, current });
      // first schedule while a job runs: 1.5 s later
      const first = at(run, 2000, null);
      // page events at +0.5 s, +1.0 s: the 3.5 s poll stays, it is not pushed to +1.5 s from each event
      const again = [at(run, 2500, first), at(run, 3000, first)];
      return [first, again, at([], 2000, null), at(wait, 2000, null), at(run, 2000, null, true), at([], 20000, null)];
    """)
    first, again, idle, waiting, hidden, overdue = r
    assert first == 3500 and again == [3500, 3500]
    assert idle == 16000 and waiting == 16000          # slow sweep 15 s after the last one
    assert hidden is None
    assert overdue == 20000                             # a sweep already due: now


def test_poll_outcome_failures_and_bad_token():
    # m-004: three polls in a row with every request failing clear the tracked jobs; bad_token stops polling
    r = run_js("""
      const fail = [{ err: "network" }, { err: "network" }];
      const a = m.pollOutcome(0, fail), b = m.pollOutcome(a.fails, fail), c = m.pollOutcome(b.fails, fail);
      return [a, b, c, m.pollOutcome(2, [{ err: "network" }, { ok: null }]), m.pollOutcome(1, [{ err: "bad_token" }]),
              m.pollOutcome(1, [])];
    """)
    a, b, c, mixed, token, empty = r
    assert a == {"fails": 1, "clear": False, "stop": False} and b == {"fails": 2, "clear": False, "stop": False}
    assert c == {"fails": 3, "clear": True, "stop": False}
    assert mixed == {"fails": 0, "clear": False, "stop": False}
    assert token["stop"] is True
    assert empty == {"fails": 1, "clear": False, "stop": False}           # nothing asked: unchanged


def test_add_and_refs_are_the_same_screen_for_collect():
    # m-005: the add form lives on the 레퍼런스 screen; collect results show there (no toast, live text instead)
    r = run_js("""
      const a = m.trackJobs([], [snap("collect", "running")], 0, "#/add");
      const done = m.finishedJobs(a, m.trackJobs(a, [snap("collect", "done")], 1, ""))[0];
      const b = m.trackJobs([], [snap("collect", "running")], 0, "#/refs");
      const wait = m.finishedJobs(b, m.trackJobs(b, [snap("collect", "awaiting")], 1, ""))[0];
      return [m.finishToast(done, "#/add"), m.finishToast(done, "#/refs"), m.finishToast(wait, "#/refs"),
              m.finishToast(wait, "#/home").href];
    """)
    assert r == [None, None, None, "#/add"]


def test_live_text_for_started_and_quiet_finishes():
    # I-001: a polite live line says when a job starts and when it ends on the screen the user is looking at
    r = run_js("""
      const a = m.trackJobs([], [snap("conti", "running", { item: "drafts/a.md" })], 0, "");
      const b = m.trackJobs(a, [snap("conti", "running", { item: "drafts/a.md" }), snap("polish", "running", { item: "drafts/a.md" })], 1, "");
      const c = m.trackJobs(b, [snap("polish", "done", { item: "drafts/a.md" }), snap("conti", "running", { item: "drafts/a.md" })], 2, "");
      return [m.startedJobs([], a).map((j) => m.startText(j)), m.startedJobs(a, b).map((j) => j.kind), m.startedJobs(b, c),
              m.finishedJobs(b, c).map((j) => m.finishText(j))];
    """)
    assert r == [["Claude가 콘티를 만드는 중이에요"], ["polish"], [], ["다듬기 끝났어요"]]


def test_finished_rows_stay_in_the_list_for_a_minute():
    # m-006: a finished job stays reachable from the chip ("끝남 · 결과 보기") for 60 s
    r = run_js("""
      const a = m.trackJobs([], [snap("motif", "running"), snap("typify", "running")], 0, "");
      const b = m.trackJobs(a, [snap("motif", "done"), snap("typify", "failed")], 1000, "");
      const recent = m.keepRecent([], m.finishedJobs(a, b), 1000);
      const later = m.keepRecent(recent, [], 61001);
      return [recent.map((r) => [r.id, r.until]), m.chipText(b, 2000, { recent }), m.chipItems(b, 2000, recent),
              m.chipText([], 2000, { recent: recent.slice(0, 1) }), later.length,
              m.chipText(m.trackJobs([], [snap("conti", "running")], 0, ""), 2000, { recent: recent.slice(0, 1) })];
    """)
    kept, text, items, one, later, mixed = r
    assert kept == [["motif-1", 61000], ["typify-1", 61000]]
    assert text == "끝난 작업 2개"
    assert items == [
        {"id": "motif-1", "text": "모티브 카드 뽑기 · 끝남", "href": "#/motif", "link": "결과 보기"},
        {"id": "typify-1", "text": "유형 나누기 · 못 끝냄", "href": "#/typify", "link": "결과 보기"},
    ]
    assert one == "모티브 카드 뽑기 · 끝남" and later == 0
    assert mixed == "작업 2개"


def test_sub_routes_of_a_result_screen_count_as_the_same_screen():
    # final m-2: #/draft/new/<card> and #/report/review/<week> are the draft / review screens (no chip toast there);
    # a one-part result (#/refs) does not swallow its detail pages (#/refs/<id> shows one ref, not the collect result)
    r = run_js("""
      const a = m.trackJobs([], [snap("draft", "running"), snap("review", "running"), snap("collect", "running")], 0, "");
      const b = m.trackJobs(a, [snap("draft", "done"), snap("review", "done"), snap("collect", "done")], 1, "");
      const [draft, review, collect] = m.finishedJobs(a, b);
      return [m.finishToast(draft, "#/draft/new/card-1"),
              m.finishToast(review, "#/report/review/2026-W43"), m.finishToast(review, "#/report").href,
              m.finishToast(draft, "#/draft/newer").href, m.finishToast(collect, "#/refs/abc").href];
    """)
    assert r == [None, None, "#/report/review", "#/draft/new", "#/refs"]


def test_live_line_skips_jobs_whose_screen_is_open():
    # final m-1: a page showing its own job announces its start and end itself; the chip's live line stays quiet there
    r = run_js("""
      const a = m.trackJobs([], [snap("conti", "running", { item: "drafts/a.md" }), snap("motif", "running")], 0, "");
      const [conti, motif] = a;
      return [m.chipSays(conti, "#/video/a/conti"), m.chipSays(conti, "#/video/a"), m.chipSays(conti, "#/home"),
              m.chipSays(motif, "#/motif"), m.chipSays(motif, "#/video/a/conti")];
    """)
    assert r == [False, False, True, False, True]
    text = CHIP.read_text(encoding="utf-8")
    assert ".filter((j) => chipSays(j, at))" in text


def test_live_line_survives_the_hidden_shoot_mode_header():
    # final (deferred T6 minor): the live line sits on <body>, not inside the header that shoot mode hides
    text = CHIP.read_text(encoding="utf-8")
    assert "document.body.append(live)" in text and "el.after(live)" not in text
