// Header job status chip (spec 1.5, 7): one place in the header says what Claude (or a long local job) is doing,
// "Claude가 모티브 카드를 만드는 중 · 18초"; several jobs read "작업 2개" and open a small list. When a job the chip saw
// running finishes, a toast says so with a link to its result.
//
// Data: there is no "all jobs" API, so the chip asks GET /api/jobs/current?kind=<kind> per kind: every kind slowly
// (POLL_MS.slow), only kinds with a running job quickly (POLL_MS.fast), nothing while the browser tab is hidden.
// api.js also fires a "ytc:job" window event for every response that carries a job (a page starting or polling a
// job), so the chip learns about a new job at once without its own request. Snapshots carry no start time: the
// elapsed seconds count from when the chip first saw the job.
//
// The pure helpers (no DOM) are exported for node tests; createStatusChip() is the DOM part.
import { h } from "./dom.js";
import { parseVideoHash, videoHref, videoId } from "./video-utils.js";

// kind -> running label (header), short name (toasts, waiting text), Claude call or local processing.
// help_chat is left out: the help chatbot shows its own answer in the help panel.
export const JOB_KINDS = {
  collect: { label: "Claude가 레퍼런스를 분석하는 중", name: "레퍼런스 분석", claude: true },
  refs_describe: { label: "Claude가 사진 설명을 만드는 중", name: "사진 설명 만들기", claude: true },
  conti: { label: "Claude가 콘티를 만드는 중", name: "콘티 만들기", claude: true },
  conti_merge: { label: "Claude가 장면을 합치는 중", name: "장면 합치기", claude: true },
  conti_refs: { label: "Claude가 장면 사진을 고르는 중", name: "사진 고르기", claude: true },
  srtfit: { label: "자막 시간 맞추는 중", name: "자막 시간 맞추기", claude: false },
  caption_video: { label: "자막 영상 만드는 중", name: "자막 영상 만들기", claude: false },
  footage: { label: "Claude가 촬영 영상을 맞추는 중", name: "촬영 영상 맞추기", claude: true },   // footage_match asks Claude
  footage_clips: { label: "컷 파일 만드는 중", name: "컷 파일 만들기", claude: false },
  diagnose: { label: "Claude가 진단 설명을 쓰는 중", name: "진단 설명", claude: true },
  draft: { label: "Claude가 기획안을 만드는 중", name: "기획안 만들기", claude: true },
  polish: { label: "Claude가 훅·제목을 다듬는 중", name: "다듬기", claude: true },
  library_summary: { label: "Claude가 섹션 요약을 만드는 중", name: "섹션 요약", claude: true },
  mission: { label: "Claude가 오늘 미션을 만드는 중", name: "미션 만들기", claude: true },
  mission_feedback: { label: "Claude가 미션 피드백을 쓰는 중", name: "미션 피드백", claude: true },
  motif: { label: "Claude가 모티브 카드를 만드는 중", name: "모티브 카드 뽑기", claude: true },
  review: { label: "Claude가 주간 회고를 쓰는 중", name: "주간 회고", claude: true },
  typify: { label: "Claude가 유형을 나누는 중", name: "유형 나누기", claude: true },
};
export const POLL_KINDS = Object.keys(JOB_KINDS);
export const POLL_MS = { fast: 1500, slow: 15000 };

const isActive = (status) => status === "running" || status === "awaiting";
// Polled quickly: running, or waiting for the user while other items still work. A job that only waits for the
// user (collect review cards) changes when the user acts on its page, so the slow sweep is enough.
const isBusy = (j) => j.status === "running" || (j.status === "awaiting" && j.working !== false);
const WAITING = "확인할 게 있어요";

// "18초", "1분 5초", "2분", "1시간 2분".
export function elapsedText(ms) {
  const s = Math.floor(Math.max(0, Number(ms) || 0) / 1000);
  if (s < 60) return `${s}초`;
  if (s < 3600) return `${Math.floor(s / 60)}분` + (s % 60 ? ` ${s % 60}초` : "");
  const min = Math.floor((s % 3600) / 60);
  return `${Math.floor(s / 3600)}시간` + (min ? ` ${min}분` : "");
}

// The work-folder path a job is about: summary.path (footage, photo picks) or the first item id (draft / conti).
function jobPath(snap) {
  const sum = snap.summary && typeof snap.summary.path === "string" ? snap.summary.path : null;
  const item = Array.isArray(snap.items) && snap.items[0] && typeof snap.items[0].id === "string" ? snap.items[0].id : null;
  return sum || item;
}

// Tracked jobs after new snapshots. A record: { id, kind, status, path, message, check (an item waits to be
// saved), since (first seen, ms), origin (the hash then), asked (was already waiting for the user once) }.
// Jobs never seen running are not tracked; finished records stay for exactly one update (finishedJobs reads them).
export function trackJobs(prev, snaps, now, origin = "") {
  const before = new Map((prev || []).map((j) => [j.id, j]));
  const next = new Map((prev || []).filter((j) => isActive(j.status)).map((j) => [j.id, j]));
  for (const s of snaps || []) {
    if (!s || typeof s.id !== "string" || !JOB_KINDS[s.kind]) continue;
    const old = before.get(s.id);
    if (!old && !isActive(s.status)) continue;
    if (old && !isActive(old.status) && !isActive(s.status)) { next.delete(s.id); continue; }
    next.set(s.id, {
      id: s.id, kind: s.kind, status: s.status, path: jobPath(s) || (old ? old.path : null),
      message: typeof s.message === "string" ? s.message : null,
      check: Array.isArray(s.items) && s.items.some((i) => i && i.state === "awaiting"),
      working: Array.isArray(s.items) && s.items.some((i) => i && (i.state === "waiting" || i.state === "running")),
      since: old ? old.since : now, origin: old ? old.origin : origin,
      asked: old ? !!old.asked || old.status === "awaiting" : false,
    });
  }
  return [...next.values()];
}

// Jobs that were running in `prev` and are now done / failed / stopped, or waiting for the user for the first
// time. outcome: done | check (done, a result waits to be saved) | awaiting | failed | stopped.
export function finishedJobs(prev, next) {
  const after = new Map((next || []).map((j) => [j.id, j]));
  const out = [];
  for (const p of prev || []) {
    if (p.status !== "running") continue;
    const n = after.get(p.id);
    if (!n || n.status === "running") continue;
    if (n.status === "awaiting") { if (!p.asked) out.push({ ...n, outcome: "awaiting" }); continue; }
    if (n.status === "done") out.push({ ...n, outcome: n.check ? "check" : "done" });
    else if (n.status === "failed" || n.status === "stopped") out.push({ ...n, outcome: n.status });
  }
  return out;
}

const VIDEO_TAB = {
  conti: "conti", conti_merge: "conti", conti_refs: "conti", srtfit: "conti", caption_video: "conti",
  footage: "conti", footage_clips: "conti", polish: "plan", diagnose: "diagnose",
};
const SCREEN = {
  collect: "#/refs", refs_describe: "#/refs", draft: "#/draft/new", library_summary: "#/refs?tab=library",
  mission: "#/mission", mission_feedback: "#/mission", motif: "#/motif", review: "#/report/review", typify: "#/typify",
};
const isDraftPath = (p) => /^drafts\/.+\.md$/i.test(p);

// Where a job's result is.
export function resultHref(job) {
  if (!job) return null;
  if (job.kind === "collect" && job.status === "awaiting") return "#/add";      // analysed videos wait to be saved
  if (job.kind === "refs_describe") {
    const from = parseVideoHash(job.origin || "");                             // started from a conti tab
    if (from && from.mode === "video") return videoHref(from.id, "conti");
  }
  const tab = VIDEO_TAB[job.kind];
  if (tab) {
    const path = String(job.path || "");
    if (isDraftPath(path)) return videoHref(videoId(path.replace(/-conti\.md$/i, ".md")), tab);
    if (job.kind === "diagnose" && path) return `#/diagnose/${encodeURIComponent(path)}`;   // an uploaded video
    return "#/videos";
  }
  return SCREEN[job.kind] || null;
}

// Routes that show the same screen (the add form lives on the 레퍼런스 screen).
const SCREEN_ALIAS = { "#/add": "#/refs" };
const screenOf = (hash) => SCREEN_ALIAS[hash] || hash;

// The user already looks at `href` (the same video counts without a tab in the address). A sub route of a result
// screen with two or more parts counts too (#/draft/new/<card>, #/report/review/<week>); a one-part screen does not
// (#/refs/<id> shows one ref, not the collect result).
function sameScreen(here, href) {
  if (!href) return false;
  if (screenOf(here) === screenOf(href)) return true;
  const base = String(here || "").split("?")[0];
  if (!href.includes("?") && href.split("/").length >= 3 && base.startsWith(`${href}/`)) return true;
  const a = parseVideoHash(here || "");
  const b = parseVideoHash(href);
  return !!a && !!b && a.mode === "video" && b.mode === "video" && a.id === b.id && (!a.tab || a.tab === b.tab);
}

// Toast for a finished job: { text, kind, href, link }, or null when the user is on the result screen already
// (the page shows the result, and its own message).
export function finishToast(job, here) {
  const info = JOB_KINDS[job.kind];
  const href = resultHref(job);
  if (!info || sameScreen(here, href)) return null;
  const kind = job.outcome === "failed" ? "error" : job.outcome === "awaiting" || job.outcome === "stopped" ? "info" : "ok";
  const link = job.outcome === "failed" || job.outcome === "stopped" ? "보러 가기" : "결과 보기";
  return { text: finishText(job), kind, href: href || null, link: href ? link : null };
}

// What a finished job says (toast, or the live line when its result screen is open).
export function finishText(job) {
  const name = (JOB_KINDS[job.kind] || { name: job.kind }).name;
  if (job.outcome === "check") return `${name} 끝났어요. ${WAITING}`;
  if (job.outcome === "awaiting") return `${name} · ${WAITING}`;
  if (job.outcome === "failed") return `${name} 못 끝냈어요` + (job.message ? `: ${job.message}` : "");
  if (job.outcome === "stopped") return `${name} 멈췄어요`;
  return `${name} 끝났어요`;
}

// Jobs seen running for the first time (the live line says they started).
export function startedJobs(prev, next) {
  const before = new Set((prev || []).map((j) => j.id));
  return (next || []).filter((j) => j.status === "running" && !before.has(j.id));
}
// The live line speaks for a job only away from its result screen: a page showing its own job announces it.
export function chipSays(job, here) {
  return !sameScreen(here, resultHref(job));
}
export function startText(job) {
  return `${JOB_KINDS[job.kind].label}이에요`;
}

// m-006: finished jobs stay in the chip list for RECENT_MS ("끝남 · 결과 보기") so the keyboard can reach the result
// after the toast is gone. A job still waiting for the user stays a live row instead.
export const RECENT_MS = 60000;
const RECENT_WORD = { done: "끝남", check: WAITING, failed: "못 끝냄", stopped: "멈춤" };
export function keepRecent(recent, finished, now) {
  const fresh = (finished || []).filter((j) => j.outcome !== "awaiting")
    .map((j) => ({ id: j.id, kind: j.kind, outcome: j.outcome, href: resultHref(j), until: now + RECENT_MS }));
  const ids = new Set(fresh.map((r) => r.id));
  return [...(recent || []).filter((r) => r.until > now && !ids.has(r.id)), ...fresh];
}
function recentRow(r) {
  return { id: r.id, text: `${JOB_KINDS[r.kind].name} · ${RECENT_WORD[r.outcome] || "끝남"}`, href: r.href, link: "결과 보기" };
}

function jobLine(job, now) {
  const info = JOB_KINDS[job.kind];
  return job.status === "awaiting" ? `${info.name} · ${WAITING}` : `${info.label} · ${elapsedText(now - job.since)}`;
}

// Chip text: "" (nothing to show), one job's line, "작업 N개", or finished rows only: "끝난 작업 N개" / one row's
// text. short: the phone header ("작업 중 · 18초"). recent: keepRecent() rows still shown.
export function chipText(jobs, now, { short = false, recent = [] } = {}) {
  const active = (jobs || []).filter((j) => isActive(j.status));
  const done = (recent || []).filter((r) => r.until > now);
  if (active.length + done.length === 0) return "";
  if (active.length === 0) return done.length > 1 ? `끝난 작업 ${done.length}개` : recentRow(done[0]).text;
  if (active.length + done.length > 1) return `작업 ${active.length + done.length}개`;
  const job = active[0];
  if (!short) return jobLine(job, now);
  return job.status === "awaiting" ? WAITING : `작업 중 · ${elapsedText(now - job.since)}`;
}

// Rows of the job list: running / waiting jobs, then finished ones (recent).
export function chipItems(jobs, now, recent = []) {
  return [
    ...(jobs || []).filter((j) => isActive(j.status))
      .map((j) => ({ id: j.id, text: jobLine(j, now), href: resultHref(j), link: "보기" })),
    ...(recent || []).filter((r) => r.until > now).map(recentRow),
  ];
}

// Kinds to ask now: every kind when the slow sweep is due, else the kinds of running / waiting jobs.
export function dueKinds(jobs, now, lastSweep, slowMs) {
  if (!lastSweep || now - lastSweep >= slowMs) return { sweep: true, kinds: [...POLL_KINDS] };
  const kinds = [];
  for (const j of jobs || []) if (isBusy(j) && !kinds.includes(j.kind)) kinds.push(j.kind);
  return { sweep: false, kinds };
}

// When the chip polls next (ms timestamp), or null while the tab is hidden. `current`: the poll already scheduled;
// an earlier one is kept, so snapshots that pages send every second never push the chip's own poll away.
export function nextDue({ jobs, now, lastSweep, hidden, current = null }) {
  if (hidden) return null;
  const due = now + ((jobs || []).some(isBusy) ? POLL_MS.fast : Math.max(0, POLL_MS.slow - (now - lastSweep)));
  return current !== null && current !== undefined && current <= due ? current : due;
}

// After a poll: results are { ok: snapshot|null } or { err: code }. Every request failing three polls in a row
// (server gone) clears the tracked jobs; bad_token (a new server) stops polling.
export const FAILS_TO_CLEAR = 3;
export function pollOutcome(fails, results) {
  const list = results || [];
  const stop = list.some((r) => r && r.err === "bad_token");
  if (list.length === 0) return { fails, clear: false, stop };
  const allFailed = list.every((r) => r && r.err);
  const next = allFailed ? fails + 1 : 0;
  return { fails: next, clear: allFailed && next >= FAILS_TO_CLEAR, stop };
}

// ---- DOM ------------------------------------------------------------------------------------------------------

// createStatusChip({ el, api, toast, here }) -> { jobStarted(kind), stop() }
//   el: the header slot (#status-chip); api(method, path, body, signal) like api.js; toast(text, kind, { href, label });
//   here(): the current hash.
export function createStatusChip({ el, api, toast, here = () => location.hash || "#/home" }) {
  const S = { jobs: [], recent: [], lastSweep: 0, timer: null, due: 0, ticker: null, busy: false, again: false,
    stopped: false, extra: new Set(), open: false, shape: "", fails: 0, ready: false };
  // One polite live line (outside the re-rendered element): says when a job starts away from its result screen. A
  // finish away from it is a toast (the toast area is a live region); on it, the page announces its own job.
  // Elapsed seconds are never spoken.
  const live = h("span", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
  document.body.append(live);      // not in the header: shoot mode hides the header
  let sayTimer = null;
  const say = (text) => {
    clearTimeout(sayTimer);
    live.textContent = "";
    sayTimer = setTimeout(() => { live.textContent = text; }, 60);
  };
  const ASK_MS = 10000;      // a request that hangs longer is given up, so polling never stalls
  const POP_ID = "status-chip-list";
  const n = {};      // current nodes: { trigger, full, short, pop }

  const active = () => S.jobs.filter((j) => isActive(j.status));

  function ingest(snaps) {
    const prev = S.jobs;
    const now = Date.now();
    S.jobs = trackJobs(prev, snaps, now, here());
    const finished = finishedJobs(prev, S.jobs);
    const at = here();
    // not the jobs found by the first poll; not jobs whose own screen is open (the page announces those)
    const lines = S.ready ? startedJobs(prev, S.jobs).filter((j) => chipSays(j, at)).map(startText) : [];
    for (const job of finished) {
      const t = finishToast(job, at);
      if (t) toast(t.text, t.kind, t.href ? { href: t.href, label: t.link } : null);
    }
    if (lines.length) say(lines.join(". "));
    S.recent = keepRecent(S.recent, finished, now);
    render();
    schedule();
  }

  async function poll() {
    if (S.stopped) return;
    if (S.busy) { S.again = true; return; }
    S.busy = true;
    clearTimeout(S.timer);
    S.timer = null;
    const ask = (path) => {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), ASK_MS);
      return api("GET", path, undefined, ctl.signal).finally(() => clearTimeout(t));
    };
    try {
      const now = Date.now();
      const due = dueKinds(S.jobs, now, S.lastSweep, POLL_MS.slow);
      const kinds = [...new Set([...due.kinds, ...S.extra])];
      S.extra.clear();
      if (due.sweep) S.lastSweep = now;
      const results = await Promise.all(kinds.map((k) => ask(`/api/jobs/current?kind=${k}`)
        .then((r) => ({ ok: (r && r.job) || null }), (e) => ({ err: (e && e.error) || "network" }))));
      if (S.stopped) return;
      const out = pollOutcome(S.fails, results);
      S.fails = out.fails;
      if (out.stop) { stop(); return; }                   // a new server (bad_token): this page is stale
      if (out.clear) { S.jobs = []; S.recent = []; }      // server gone: forget, fall back to the slow sweep
      const answers = results.map((r) => (r.err ? undefined : r.ok));
      const snaps = answers.filter(Boolean);
      // A tracked job the "current" answer no longer names (an older job of a kind that runs several, e.g. two
      // contis): ask for it by id. Gone (server restarted): drop it quietly.
      const named = new Set(snaps.map((s) => s.id));
      const answered = new Set(kinds.filter((k, i) => answers[i] !== undefined));
      const lost = S.jobs.filter((j) => isActive(j.status) && answered.has(j.kind) && !named.has(j.id));
      const extra = await Promise.all(lost.map((j) => ask(`/api/jobs/${encodeURIComponent(j.id)}`)
        .then((r) => (r && r.job) || null, (e) => (e && e.error === "job_not_found" ? { gone: j.id } : null))));
      if (S.stopped) return;
      const gone = new Set(extra.filter((x) => x && x.gone).map((x) => x.gone));
      if (gone.size) S.jobs = S.jobs.filter((j) => !gone.has(j.id));
      ingest([...snaps, ...extra.filter((x) => x && !x.gone)]);
      S.ready = true;
    } finally {
      S.busy = false;
      if (S.again && !S.stopped) { S.again = false; poll(); } else schedule();
    }
  }

  // Next poll: soon while a job runs, else at the next sweep. A poll already due earlier is kept: pages that poll
  // their own job send snapshots every second ("ytc:job"), and those must not keep pushing the chip's poll away.
  function schedule() {
    // the 1 s tick: elapsed seconds, and finished rows leaving after RECENT_MS
    const ticking = active().some((j) => j.status === "running") || S.recent.length > 0;
    if (!ticking || S.stopped || document.hidden) { clearInterval(S.ticker); S.ticker = null; }
    else if (!S.ticker) S.ticker = setInterval(tick, 1000);
    if (S.stopped) return;
    if (S.busy) return;                                   // the running poll schedules when it ends
    const now = Date.now();
    const due = nextDue({ jobs: S.jobs, now, lastSweep: S.lastSweep, hidden: document.hidden,
      current: S.timer ? S.due : null });
    if (due === null) { clearTimeout(S.timer); S.timer = null; return; }
    if (S.timer && due === S.due) return;
    clearTimeout(S.timer);
    S.due = due;
    S.timer = setTimeout(() => { S.timer = null; poll(); }, due - now);
  }

  function tick() {
    const now = Date.now();
    const kept = S.recent.filter((r) => r.until > now);
    if (kept.length !== S.recent.length) { S.recent = kept; render(); schedule(); } else paintText();
  }

  // ---- render ----

  function setOpen(open) {
    S.open = open && !!n.pop;
    if (n.trigger && n.pop) {
      n.trigger.setAttribute("aria-expanded", String(S.open));
      n.pop.hidden = !S.open;
    }
  }

  function paintText() {
    const now = Date.now();
    if (n.full) n.full.textContent = chipText(S.jobs, now, { recent: S.recent });
    if (n.short) n.short.textContent = chipText(S.jobs, now, { short: true, recent: S.recent });
    if (n.rows) {
      for (const it of chipItems(S.jobs, now, S.recent)) {
        const row = n.rows.get(it.id);
        if (row) { row.text.textContent = it.text; row.link.setAttribute("aria-label", `${it.text} 보기`); }
      }
    }
  }

  function render() {
    const now = Date.now();
    const jobs = active();
    const items = chipItems(S.jobs, now, S.recent);
    const shape = items.map((it) => `${it.id}|${it.href}|${it.link}`).join(",") + `|${jobs.map((j) => j.status).join(",")}`;
    if (shape === S.shape) { paintText(); return; }
    S.shape = shape;
    const hadFocus = el.contains(document.activeElement);
    n.trigger = n.full = n.short = n.pop = null;
    n.rows = null;
    if (items.length === 0) { S.open = false; el.replaceChildren(); return; }
    const dot = h("span", { class: `chip-dot${jobs.some((j) => j.status === "running") ? " is-running" : jobs.length ? "" : " is-done"}`, "aria-hidden": "true" });
    n.full = h("span", { class: "chip-full" });
    n.short = h("span", { class: "chip-short", "aria-hidden": "true" });
    if (items.length === 1 && jobs.length === 1) {
      const href = items[0].href;
      n.trigger = href ? h("a", { class: "chip-job", href }, dot, n.full, n.short)
        : h("span", { class: "chip-job" }, dot, n.full, n.short);
      S.open = false;
      el.replaceChildren(n.trigger);
    } else {
      n.trigger = h("button", { type: "button", class: "chip-job", "aria-expanded": "false", "aria-controls": POP_ID },
        dot, n.full, n.short);
      n.trigger.addEventListener("click", () => setOpen(!S.open));
      n.rows = new Map();
      const list = h("ul", { class: "chip-list" }, ...items.map((it) => {
        const text = h("span", { class: "chip-row-text" }, it.text);
        const link = it.href ? h("a", { class: "chip-row-link", href: it.href, "aria-label": `${it.text} 보기` }, it.link)
          : h("span", {});
        link.addEventListener("click", () => setOpen(false));
        n.rows.set(it.id, { text, link });
        return h("li", { class: "chip-row" }, text, link);
      }));
      n.pop = h("div", { class: "chip-pop", id: POP_ID, hidden: true }, h("p", { class: "chip-pop-title" }, jobs.length ? "진행 중인 작업" : "끝난 작업"), list);
      el.replaceChildren(n.trigger, n.pop);
      setOpen(S.open);
    }
    paintText();
    if (hadFocus && n.trigger) n.trigger.focus();
  }

  // ---- events ----

  function onJob(e) {
    const snap = e && e.detail;
    if (snap && JOB_KINDS[snap.kind]) ingest([snap]);
  }
  function onVisibility() {
    if (document.hidden) { schedule(); return; }
    poll();
  }
  function onDocClick(e) {
    if (S.open && !el.contains(e.target)) setOpen(false);
  }
  function onKey(e) {
    if (e.key === "Escape" && S.open) { e.stopPropagation(); setOpen(false); if (n.trigger) n.trigger.focus(); }
  }
  function onFocusOut(e) {
    if (S.open && !(e.relatedTarget && el.contains(e.relatedTarget))) setOpen(false);
  }

  function stop() {
    S.stopped = true;
    clearTimeout(S.timer);
    clearInterval(S.ticker);
    clearTimeout(sayTimer);
    window.removeEventListener("ytc:job", onJob);
    document.removeEventListener("visibilitychange", onVisibility);
    document.removeEventListener("click", onDocClick);
    el.replaceChildren();
  }

  window.addEventListener("ytc:job", onJob);
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("click", onDocClick);
  el.addEventListener("keydown", onKey);
  el.addEventListener("focusout", onFocusOut);
  poll();

  return {
    // A page started a job of `kind`: ask for it now (its POST answer usually arrived through "ytc:job" already).
    jobStarted(kind) {
      if (!JOB_KINDS[kind] || S.stopped) return;
      S.extra.add(kind);
      poll();
    },
    stop,
  };
}
