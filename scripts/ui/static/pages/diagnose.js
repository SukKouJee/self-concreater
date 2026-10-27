// 진단 화면
//   목록(내 영상·기획안) → 대상 하나: 저장된 진단 보기(갭마다 "일부러 다르게 한 거예요") / "비교하기"(Claude 없음)
//   / "설명 받기"(작업: 비교 → Claude 1회) → 미리보기(편집) → 저장.
//   미리보기 단계에서는 mine/ 이 바뀌지 않는다. 저장은 사용자가 누른 뒤에만. 의도한 차이 표시는 사용자 행동이라 바로 저장한다.
import { h, fill } from "/static/dom.js";
import { sectionLabelName } from "/static/labels.js";
import { renderMarkdown } from "/static/md.js";
import { recordMismatch as gapsMismatch, previewStale as gapsStale } from "/static/diagnose-gaps.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.diagnose.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const MAX_ACTIONS = 3;
const MISSION_COMMAND = "/yt-coach:yt-mission today";
const STEP_TEXT = { compare: "아키타입과 비교하는 중", claude: "Claude가 설명 쓰는 중" };
// Mirrors scripts/ui/api_diagnose.AXIS_KO; the server's `axis_ko` wins when present.
const AXIS_KO = { structure: "구조", hook: "훅", narrative: "서사", title: "제목", pacing: "컷 속도", shots: "샷" };
const AXIS_ORDER = ["structure", "hook", "narrative", "title", "pacing", "shots"];

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const detailHref = (path) => `#/diagnose/${encodeURIComponent(path)}`;
const pct = (v) => `${Math.round(v * 100)}%`;

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved (or discarded) in this browser is not restored after a reload.
function markHandled(jobId) {
  if (!jobId) return;
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

function previewOf(job) {
  const item = job && job.items && job.items[0];
  const p = item && item.state === "awaiting" ? item.preview : null;
  return p && p.result && Array.isArray(p.result.gaps) && p.notes ? p : null;
}

function gapName(key, axisKo) {
  const [axis, sub] = String(key).split(":");
  const name = (axisKo && axisKo[axis]) || AXIS_KO[axis] || axis;
  if (axis === "structure" && sub) return `${name}: ${sectionLabelName(sub)}`;
  if (axis === "pacing" && sub) return `${name}: 평균 컷 길이`;
  return name;
}

function gapValue(key, v) {
  if (v === null || v === undefined || v === "") return "없음";
  if (key.startsWith("structure:") && typeof v === "number") return pct(v);
  if (key.startsWith("pacing:") && typeof v === "number") return `${v.toFixed(1)}초`;
  return String(v);
}

const gapNumbers = (g) => `나 ${gapValue(g.key, g.target)} / 아키타입 ${gapValue(g.key, g.archetype)}`;
const nearestId = (n) => (n && typeof n === "object" ? n.id : n);

// ---- page state ---------------------------------------------------------------

let S = null;

// Each category/target change builds a fresh state, so late async work of the old one sees S !== mine and stops.
function freshState(el, ctx, category, path) {
  return {
    el, ctx, category, path, targets: null, entry: null, record: null, intentional: [], gapInfo: new Map(),
    compare: null, compareBusy: 0, cmpSeq: 0, compareSoft: false, compareError: null, mismatchText: "", staleText: "", otherTimer: null, recordError: null, toggleBusy: new Set(),
    job: null, jobId: null, otherJob: null, preview: null, edits: null, confirmRerun: false, confirmDiscard: false,
    running: false, runError: null, saving: false, saveError: null,
    timer: null, pollErrors: 0, runSig: null, alertSig: null, nodes: {},
  };
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
  if (S && S.otherTimer) { clearTimeout(S.otherTimer); S.otherTimer = null; }
}

function ignorable(e) {
  return !e || e.error === "aborted";
}

function focusStatus() {
  const t = S && S.nodes.status;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

const qs = (mine) => `category=${encodeURIComponent(mine.category)}`;

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  if (!mine.category) {
    fill(mine.el, h("h1", {}, "진단"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", {}, "진단"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const calls = [mine.ctx.api("GET", `/api/diagnose/targets?${qs(mine)}`)];
    if (mine.path) calls.push(mine.ctx.api("GET", "/api/jobs/current?kind=diagnose"));
    const [targets, cur] = await Promise.all(calls);
    if (!alive(mine)) return;
    mine.targets = targets;
    if (!mine.path) { renderList(); return; }
    mine.entry = [...targets.videos, ...targets.drafts].find((t) => t.path === mine.path) || null;
    if (mine.entry && mine.entry.diagnosed) await loadRecord(mine);
    if (!alive(mine)) return;
    pickJob(mine, cur && cur.job);
    renderDetail();
    if (isActive(mine.job)) schedulePoll();
    if (mine.otherJob) watchOtherJob();
    if (canCompare()) compareNow(true);           // free (no Claude): shows the current gaps right away
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e);
  }
}

async function loadRecord(mine) {
  try {
    const rec = await mine.ctx.api("GET", `/api/diagnose/record?${qs(mine)}&name=${encodeURIComponent(mine.entry.name)}`);
    if (!alive(mine)) return;
    setRecord(mine, rec);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    if (e.error !== "diagnosis_not_found" && e.error !== "bad_request") throw e;     // unusable name: treat as no record
    mine.record = null;
  }
}

function setRecord(mine, rec) {
  mine.record = rec;
  mine.intentional = Array.isArray(rec.fm.intentional) ? rec.fm.intentional.slice() : [];
}

// Re-entry: only a job for THIS target is restored; one for another target just blocks a new run.
function pickJob(mine, job) {
  if (!job || job.category !== mine.category || isHandled(job.id)) return;
  const item = job.items && job.items[0];
  if (!item || item.id !== mine.path) {
    if (isActive(job)) mine.otherJob = job;
    return;
  }
  if (isActive(job) || (job.status === "done" && previewOf(job))) {
    adoptJob(mine, job);
  } else if (job.status === "failed" || job.status === "stopped") {
    adoptJob(mine, job);          // shown once, then not again
    markHandled(job.id);
  }
}

// Another target's explain job blocks a new run; re-check until it ends so the button comes back.
function watchOtherJob() {
  const mine = S;
  if (mine.otherTimer) clearTimeout(mine.otherTimer);
  mine.otherTimer = setTimeout(async () => {
    mine.otherTimer = null;
    if (!alive(mine) || !mine.otherJob) return;
    try {
      const cur = await mine.ctx.api("GET", "/api/jobs/current?kind=diagnose");
      if (!alive(mine)) return;
      const job = cur && cur.job;
      if (!job || job.id !== mine.otherJob.id || !isActive(job)) {
        mine.otherJob = null;
        renderRunBox();
        return;
      }
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
    }
    if (alive(mine) && mine.otherJob) watchOtherJob();
  }, POLL_MS * 3);
}

function loadError(e) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) load(); });
  fill(S.el, h("h1", {}, "진단"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

function adoptJob(mine, job) {
  mine.job = job;
  const preview = job.status === "done" ? previewOf(job) : null;
  if (preview && (!mine.preview || mine.jobId !== job.id)) {
    mine.preview = preview;
    mine.jobId = job.id;
    initEdits(mine, preview);
  }
}

function initEdits(mine, preview) {
  const notes = preview.notes;
  const gaps = {};
  for (const g of preview.result.gaps) gaps[g.key] = String((notes.gaps || {})[g.key] || "");
  const actions = (notes.actions || []).map(String).slice(0, MAX_ACTIONS);
  const edits = { summary: String(notes.summary || ""), gaps, actions: actions.length ? actions : [""] };
  mine.edits = { ...edits, orig: JSON.stringify(edits) };
  mine.confirmRerun = false;
  mine.confirmDiscard = false;
  mine.saveError = null;
  mine.runError = null;
  rememberGaps(mine, preview.result);
}

const editsSnapshot = (e) => JSON.stringify({ summary: e.summary, gaps: e.gaps, actions: e.actions });
const hasEdits = () => !!S.preview && !!S.edits && editsSnapshot(S.edits) !== S.edits.orig;

function rememberGaps(mine, result) {
  for (const g of result.gaps || []) mine.gapInfo.set(g.key, g);
}

// ---- list ---------------------------------------------------------------------

function targetRow(t) {
  const kids = [
    h("span", { class: "diag-title" }, t.title),
    h("span", { class: "muted small num" }, t.date),
  ];
  if (t.diagnosed) {
    const near = nearestId(t.diagnosed.nearest);
    kids.push(h("span", { class: "badge diag-done" }, `진단함 ${t.diagnosed.date}${near ? ` · ${near}` : ""}`));
  }
  return h("li", {}, h("a", { class: "diag-row", href: detailHref(t.path) }, ...kids));
}

function listSection(id, title, items, emptyKids) {
  const kids = [h("h2", { id }, `${title} (${items.length})`)];
  if (items.length) kids.push(h("ul", { class: "diag-list", "aria-labelledby": id }, ...items.map(targetRow)));
  else kids.push(h("p", { class: "muted" }, ...emptyKids));
  return h("section", { class: "card", "aria-labelledby": id }, ...kids);
}

function noArchetypeNote() {
  return h("p", { class: "muted hint" }, "비교할 아키타입이 없습니다. ", h("a", { href: "#/typify" }, "유형화 하러 가기"));
}

function renderList() {
  const t = S.targets;
  S.nodes = {};
  const kids = [
    h("h1", { id: "diag-title" }, "진단"),
    h("p", { class: "lead" }, "내 영상이나 기획안을 레퍼런스 아키타입과 축별로 비교합니다."),
  ];
  if (!t.has_typify) kids.push(noArchetypeNote());
  else if (t.preliminary) kids.push(h("p", { class: "muted hint" }, "예비 진단 — 레퍼런스가 10개 미만이라 참고용으로 봐 주세요"));
  kids.push(
    listSection("diag-videos", "내 영상", t.videos, [
      "레퍼런스 화면에서 구분을 '내 영상'으로 넣으면 여기 나옵니다 ", h("a", { href: "#/add" }, "레퍼런스 넣기"),
    ]),
    listSection("diag-drafts", "기획안", t.drafts, [
      "진단할 기획안이 없습니다 ", h("a", { href: "#/draft" }, "기획안 만들러 가기"),
    ]),
    missionHint(),
  );
  fill(S.el, ...kids);
}

// ---- detail -------------------------------------------------------------------

function missionHint() {
  const btn = h("button", { type: "button", class: "btn" }, "명령 복사");
  btn.addEventListener("click", async () => {
    const mine = S;
    try {
      await navigator.clipboard.writeText(MISSION_COMMAND);
      if (alive(mine)) mine.ctx.toast("명령을 복사했습니다", "info");
    } catch {
      if (alive(mine)) mine.ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
    }
  });
  return h("section", { class: "diag-mission", "aria-label": "오늘 미션" },
    h("p", { class: "muted small" }, "오늘 미션은 터미널에서 받습니다"),
    h("div", { class: "cmd-row" }, h("code", { class: "cmd" }, MISSION_COMMAND), btn));
}

function renderDetail() {
  S.nodes = {};
  S.runSig = null;
  S.alertSig = null;
  const e = S.entry;
  if (!e) {
    fill(S.el,
      h("p", { class: "crumb" }, h("a", { href: "#/diagnose" }, "← 진단 목록")),
      h("h1", {}, "진단"),
      h("p", { class: "error", role: "alert" }, "이 카테고리에서 진단할 수 있는 대상을 찾을 수 없습니다(내 영상 또는 기획안만 가능)"));
    return;
  }
  S.nodes.status = h("h1", { id: "diag-title" }, `진단: ${e.title}`);
  S.nodes.run = h("section", { class: "job-box diag-run", "aria-label": "비교와 설명" });
  S.nodes.compare = h("section", { class: "card diag-compare", "aria-label": "비교 결과" });
  S.nodes.record = h("section", { class: "diag-record", "aria-label": "저장된 진단" });
  S.nodes.preview = h("section", { class: "diag-preview", "aria-label": "설명 미리보기" });
  fill(S.el,
    h("p", { class: "crumb" }, h("a", { href: "#/diagnose" }, "← 진단 목록")),
    S.nodes.status,
    h("p", { class: "muted small" }, `${e.date} · ${e.path}`),
    S.nodes.run, S.nodes.record, S.nodes.compare, S.nodes.preview, missionHint());
  renderRunBox();
  renderCompare();
  renderRecord();
  renderPreview();
}

// ---- run box (compare / explain buttons, progress, errors) ---------------------

const canCompare = () => !!S.targets && S.targets.has_typify;

function renderRunBox() {
  const box = S.nodes.run;
  if (!box) return;
  const job = S.job;
  const active = isActive(job);
  const failed = !!job && (job.status === "failed" || job.status === "stopped");
  const item0 = job && job.items && job.items[0];
  const sig = JSON.stringify([active, failed, S.running, S.compareBusy > 0, !!S.compare, S.runError, S.confirmRerun, !!S.record, S.saving,
    item0 && item0.step, item0 && item0.message, job && job.message, !!S.otherJob, canCompare()]);
  if (sig === S.runSig && box.childNodes.length) return;
  S.runSig = sig;
  const busy = S.running || active || S.saving;
  const compareBtn = h("button", { type: "button", class: "btn", disabled: !canCompare() || S.compareBusy > 0 || busy }, S.compare ? "다시 비교" : "비교하기");
  const explainBtn = h("button", {
    type: "button", class: "btn btn-primary", disabled: !canCompare() || busy || !!S.otherJob,
  }, S.record ? "설명 다시 받기" : "설명 받기");
  compareBtn.addEventListener("click", () => compareNow(false));
  explainBtn.addEventListener("click", requestExplain);
  const kids = [
    h("div", { class: "btn-row" }, compareBtn, explainBtn,
      h("span", { class: "muted small" }, "비교하기는 바로 끝납니다. 설명 받기는 Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (!canCompare()) kids.push(noArchetypeNote());
  if (S.otherJob) {
    const other = S.otherJob.items[0];
    kids.push(h("p", { class: "muted hint", role: "status" }, "다른 대상의 설명을 만드는 중입니다 ",
      h("a", { href: detailHref(other.id) }, other.title || "그 대상으로 가기")));
  }
  if (S.confirmRerun) kids.push(rerunConfirm());
  if (active) {
    const step = (item0 && STEP_TEXT[item0.step]) || "준비 중";
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "설명 만들기 진행" }),
      h("span", {}, `${step}…`)));
  }
  if (failed) {
    kids.push(h("p", { class: "error", role: "alert" },
      `${(item0 && item0.message) || job.message || "설명을 만들지 못했습니다"} 저장된 진단은 그대로입니다.`));
  }
  if (S.runError) kids.push(h("p", { class: "error", role: "alert" }, S.runError));
  fill(box, ...kids);
}

function rerunConfirm() {
  const yes = h("button", { type: "button", class: "btn btn-danger", "data-rerun-yes": "" }, "다시 받기");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  yes.addEventListener("click", startExplain);
  no.addEventListener("click", () => {
    S.confirmRerun = false;
    renderRunBox();
    const back = S.nodes.run.querySelector("button.btn-primary");
    if (back) back.focus();
  });
  return h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, "지금 미리보기에서 고친 내용은 사라집니다. 저장한 진단은 그대로입니다."),
    h("div", { class: "btn-row" }, yes, no));
}

function requestExplain() {
  if (S.preview && hasEdits() && !S.confirmRerun) {
    S.confirmRerun = true;
    renderRunBox();
    const yes = S.nodes.run.querySelector("[data-rerun-yes]");
    if (yes) yes.focus();
    return;
  }
  startExplain();
}

async function startExplain() {
  const mine = S;
  if (mine.running || mine.saving || isActive(mine.job)) return;
  mine.running = true;
  mine.runError = null;
  mine.confirmRerun = false;
  renderRunBox();
  try {
    const res = await mine.ctx.api("POST", "/api/diagnose/explain", { category: mine.category, target: mine.path });
    if (!alive(mine)) return;
    if (mine.jobId) markHandled(mine.jobId);
    mine.job = res.job;
    mine.preview = null;
    mine.jobId = null;
    mine.edits = null;
    mine.pollErrors = 0;
    renderPreview();
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.runError = e.message || "설명을 만들지 못했습니다";
    if (e.error === "job_running") {
      try {
        const cur = await mine.ctx.api("GET", "/api/jobs/current?kind=diagnose");
        if (alive(mine) && cur && cur.job && isActive(cur.job) && cur.job.items[0].id !== mine.path) {
          mine.otherJob = cur.job;
          watchOtherJob();
        }
      } catch { /* the error above is enough */ }
    }
  } finally {
    if (alive(mine)) { mine.running = false; renderRunBox(); if (!mine.runError) focusStatus(); }
  }
}

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    if (!alive(mine) || !mine.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(mine.job.id)}`);
      if (!alive(mine)) return;
      if (mine.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.pollErrors = 0;
      const wasActive = isActive(mine.job);
      adoptJob(mine, data.job);
      if (wasActive && !isActive(mine.job)) {
        if (mine.job.status === "failed" || mine.job.status === "stopped") markHandled(mine.job.id);
        renderRunBox();
        renderPreview();
        focusStatus();
      } else {
        renderRunBox();
      }
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 설명을 다시 받으세요", "error");
        mine.job = null;
        renderRunBox();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

// ---- compare ------------------------------------------------------------------

function setCompare(mine, result) {
  mine.compare = result;
  rememberGaps(mine, result);
}

// `silent`: automatic compare on open. No focus move, failures are plain text (not an alert).
// Responses are applied newest-request-first (cmpSeq), so an older one never overwrites a newer one.
async function compareNow(silent) {
  const mine = S;
  const seq = ++mine.cmpSeq;
  mine.compareBusy += 1;
  mine.compareError = null;
  renderRunBox();
  renderCompare();
  let applied = false;
  try {
    const res = await mine.ctx.api("POST", "/api/diagnose/compare", { category: mine.category, target: mine.path });
    if (!alive(mine)) return;
    if (seq === mine.cmpSeq) { setCompare(mine, res); applied = true; }
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    if (seq === mine.cmpSeq) { mine.compareError = e.message || "비교하지 못했습니다"; mine.compareSoft = !!silent; }
  } finally {
    if (alive(mine)) {
      mine.compareBusy -= 1;
      renderRunBox();
      renderCompare();
      renderRecord();
      updateActions();
      const head = mine.nodes.compare && mine.nodes.compare.querySelector("h2");
      if (!silent && applied && head) { head.setAttribute("tabindex", "-1"); head.focus({ preventScroll: true }); }
    }
  }
}

function proximityTable(c) {
  const axisKo = c.axis_ko || AXIS_KO;
  const keys = [...AXIS_ORDER.filter((k) => k in c.axes), ...Object.keys(c.axes).filter((k) => !AXIS_ORDER.includes(k))];
  const rows = keys.map((k) => {
    const a = c.axes[k];
    const known = a.proximity !== null && a.proximity !== undefined;
    return h("tr", {},
      h("th", { scope: "row" }, axisKo[k] || k),
      h("td", { class: "num" }, known ? pct(a.proximity) : "정보 없음"),
      h("td", { class: "muted small" }, !known && a.note ? a.note : ""));
  });
  return h("div", { class: "table-wrap" },
    h("table", { class: "preview-table diag-table" },
      h("caption", { class: "sr-only" }, "축별 근접도"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "축"), h("th", { scope: "col" }, "근접도"), h("th", { scope: "col" }, "비고"))),
      h("tbody", {}, ...rows)));
}

function gapList(gaps, axisKo) {
  if (!gaps.length) return h("p", { class: "muted" }, "눈에 띄는 갭이 없습니다");
  return h("ul", { class: "diag-gaps" }, ...gaps.map((g) => h("li", {},
    h("strong", {}, gapName(g.key, axisKo)), h("span", { class: "muted" }, ` — ${gapNumbers(g)}`))));
}

function renderCompare() {
  const box = S.nodes.compare;
  if (!box) return;
  const c = S.compare;
  if (!c && !S.compareError && !S.compareBusy) { box.hidden = true; fill(box); return; }
  box.hidden = false;
  const kids = [h("h2", {}, "비교 결과")];
  if (S.compareBusy && !c) kids.push(h("p", { class: "muted" }, "비교하는 중…"));
  if (S.compareError) {
    kids.push(S.compareSoft
      ? h("p", { class: "muted" }, `자동 비교를 하지 못했습니다. ${S.compareError}`)
      : h("p", { class: "error", role: "alert" }, S.compareError));
  }
  if (c) {
    const axisKo = c.axis_ko || AXIS_KO;
    if (c.preliminary) kids.push(h("p", { class: "warn-text" }, "예비 진단 — 레퍼런스가 10개 미만이라 참고용입니다"));
    const near = c.nearest;
    kids.push(
      h("h3", {}, "가장 가까운 아키타입"),
      h("p", {}, h("strong", {}, `${near.id}${near.name ? ` ${near.name}` : ""}`)),
      h("p", { class: "small" }, h("span", { class: "muted" }, "아키타입 골격 "), near.skeleton || "-"),
      h("p", { class: "small" }, h("span", { class: "muted" }, "내 골격 "), c.target_sequence || "-"),
      h("h3", {}, "축별 근접도"),
      proximityTable(c));
    if ((c.others || []).length) {
      kids.push(h("h3", {}, "다른 아키타입과의 구조 근접도"),
        h("ul", { class: "diag-gaps" }, ...c.others.map((o) => h("li", {},
          `${o.id}${o.name ? ` ${o.name}` : ""}`,
          h("span", { class: "muted" }, ` — ${o.structure === null || o.structure === undefined ? "정보 없음" : pct(o.structure)}`)))));
    }
    kids.push(h("h3", {}, "갭"), gapList(c.gaps || [], axisKo));
  }
  fill(box, ...kids);
}

// ---- saved record + intentional toggles ---------------------------------------

const compareKeys = () => (S.compare ? (S.compare.gaps || []).map((g) => g.key) : []);

// Gaps the user marked intentional since the save are expected to be missing from the current compare.
function recordMismatch() {
  if (!S.record || !S.compare) return false;
  const saved = Array.isArray(S.record.fm.gaps) ? S.record.fm.gaps : [];
  return gapsMismatch(saved, S.intentional, compareKeys());
}

// Once a compare is known it is the source of truth; intentional gaps are not part of compare.gaps, so they are added.
function gapKeys() {
  const base = S.compare ? S.compare.gaps.map((g) => g.key)
    : ((S.record && Array.isArray(S.record.fm.gaps)) ? S.record.fm.gaps : []);
  return [...new Set([...base, ...S.intentional])];
}

// A persistent node: its text changes only when the message changes, so it is not announced again on every re-render.
function mismatchNode() {
  if (!S.nodes.mismatch) { S.nodes.mismatch = h("p", { class: "warn-text", role: "status" }); S.mismatchText = ""; }
  const text = recordMismatch() ? "저장된 설명이 지금 갭과 다릅니다 — 설명 다시 받기" : "";
  if (text !== S.mismatchText) { S.mismatchText = text; S.nodes.mismatch.textContent = text; }
  S.nodes.mismatch.hidden = !text;
  return S.nodes.mismatch;
}

function toggleRow(key) {
  const on = S.intentional.includes(key);
  const info = S.gapInfo.get(key);
  const axisKo = S.compare && S.compare.axis_ko;
  const name = gapName(key, axisKo);
  const btn = h("button", {
    type: "button", class: "btn diag-toggle", "aria-pressed": String(on), "data-gap-key": key,
    "aria-label": `일부러 다르게 한 거예요: ${name}`, "aria-disabled": S.toggleBusy.has(key) || S.saving ? "true" : false,
  }, "일부러 다르게 한 거예요");
  btn.addEventListener("click", () => toggleIntentional(key));
  // Intentional gaps are not in compare.gaps, so they have no current numbers.
  const detail = on ? "일부러 다르게 한 갭" : (info ? gapNumbers(info) : "");
  return h("li", { class: "diag-gap-row" },
    h("div", { class: "diag-gap-text" },
      h("strong", {}, name),
      detail ? h("span", { class: "muted small" }, ` — ${detail}`) : null),
    btn);
}

async function toggleIntentional(key) {
  const mine = S;
  if (!mine.record || mine.saving || mine.toggleBusy.has(key)) return;
  const on = !mine.intentional.includes(key);
  mine.toggleBusy.add(key);
  mine.recordError = null;
  mine.intentional = on ? [...mine.intentional, key] : mine.intentional.filter((k) => k !== key);
  const seq = ++mine.cmpSeq;                    // a newer compare (button or toggle) wins over this response's compare
  renderRecord(key);
  let failed = false;
  try {
    const res = await mine.ctx.api("POST", "/api/diagnose/intentional",
      { category: mine.category, name: mine.record.name, key, on });
    if (!alive(mine)) return;
    mine.toggleBusy.delete(key);
    // With other toggles in flight the local list is newer than this response.
    if (!mine.toggleBusy.size && Array.isArray(res.intentional)) mine.intentional = res.intentional.slice();
    if (res.compare && seq === mine.cmpSeq) setCompare(mine, res.compare);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.toggleBusy.delete(key);
    failed = true;
    const has = mine.intentional.includes(key);
    if (on && has) mine.intentional = mine.intentional.filter((k) => k !== key);       // roll back
    else if (!on && !has) mine.intentional = [...mine.intentional, key];
    mine.recordError = `표시를 바꾸지 못했습니다. ${e.message || ""}`.trim();
  } finally {
    if (alive(mine)) {
      mine.toggleBusy.delete(key);
      renderRecord(key);
      renderCompare();
      updateActions();
      if (failed && !mine.compare && canCompare()) compareNow(true);     // show the real gaps even though the toggle failed
    }
  }
}

function renderRecord(focusKey) {
  const box = S.nodes.record;
  if (!box) return;
  const rec = S.record;
  if (!rec) { fill(box); return; }
  const fm = rec.fm;
  const near = fm.nearest || {};
  const kids = [
    h("h2", { id: "diag-record-title" }, "저장된 진단"),
    h("p", { class: "muted small" },
      `진단함 ${fm.date || ""} · 가장 가까운 아키타입 ${near.id || "-"}${near.name ? ` ${near.name}` : ""}`,
      fm.preliminary ? " · 예비 진단(레퍼런스 10개 미만)" : ""),
  ];
  const parts = Array.isArray(rec.body) ? rec.body : [];
  const pageTitle = `진단: ${S.entry.title}`;
  for (const p of parts) {
    if (p.heading === "의도한 차이") continue;     // the toggle list below is the source of truth
    kids.push(h("div", { class: "card diag-md" },
      p.heading ? h("h3", {}, p.heading) : null,
      h("div", { class: "md" }, renderMarkdown(p.text || "", { dropTitle: pageTitle }))));
  }
  const keys = gapKeys();
  const toggles = [h("h3", { id: "diag-toggle-title" }, "갭 표시")];
  toggles.push(h("p", { class: "muted small" }, "일부러 다르게 만든 갭은 이후 진단에서 빠집니다. 누르면 바로 저장되고, 표시는 이 목록이 기준입니다."));
  if (keys.length) toggles.push(h("ul", { class: "diag-gap-list", "aria-labelledby": "diag-toggle-title" }, ...keys.map(toggleRow)));
  else toggles.push(h("p", { class: "muted" }, "표시할 갭이 없습니다"));
  toggles.push(mismatchNode());
  if (S.recordError) toggles.push(h("p", { class: "error", role: "alert" }, S.recordError));
  kids.push(h("div", { class: "card" }, ...toggles));
  fill(box, ...kids);
  if (focusKey) {
    const btn = box.querySelector(`[data-gap-key="${CSS.escape(focusKey)}"]`);
    if (btn) btn.focus({ preventScroll: true });
  }
}

// ---- preview (editable notes) -------------------------------------------------

// The open preview was written for a gap set that no longer matches the current compare (after a toggle).
function previewStale() {
  if (!S.preview || !S.compare) return false;
  return gapsStale(S.preview.result.gaps.map((g) => g.key), compareKeys());
}

// Preview gaps still present in the current compare (the server drops the notes of vanished gaps).
function activeGaps() {
  if (!S.compare) return S.preview.result.gaps;
  const now = compareKeys();
  return S.preview.result.gaps.filter((g) => now.includes(g.key));
}

const STALE_TEXT = "갭이 바뀌었습니다 — 설명 다시 받기 후 저장하세요";

function saveBlocker() {
  if (previewStale()) return STALE_TEXT;
  const e = S.edits;
  if (!e.summary.trim()) return "요약이 비어 있습니다";
  const axisKo = S.preview.result.axis_ko;
  for (const g of activeGaps()) {
    if (!e.gaps[g.key].trim()) return `'${gapName(g.key, axisKo)}' 설명이 비어 있습니다`;
  }
  if (!e.actions.some((a) => a.trim())) return "다음 행동을 1개 이상 적어 주세요";
  return "";
}

function notesPayload() {
  const e = S.edits;
  const gaps = {};
  for (const g of activeGaps()) gaps[g.key] = e.gaps[g.key].trim();
  return { summary: e.summary.trim(), gaps, actions: e.actions.map((a) => a.trim()).filter(Boolean) };
}

function renderPreview() {
  const box = S.nodes.preview;
  if (!box) return;
  if (!S.preview || !S.edits) { fill(box); return; }
  const p = S.preview;
  const e = S.edits;
  const axisKo = p.result.axis_ko;
  const summary = h("input", { id: "diag-summary", type: "text" });
  summary.value = e.summary;
  summary.addEventListener("input", () => { e.summary = summary.value; updateActions(); });
  const gapFields = p.result.gaps.map((g, i) => {
    const id = `diag-gap-${i}`;
    const ta = h("textarea", { id, rows: "3" });
    ta.value = e.gaps[g.key];
    ta.addEventListener("input", () => { e.gaps[g.key] = ta.value; updateActions(); });
    return h("div", { class: "field" },
      h("label", { for: id }, gapName(g.key, axisKo), h("span", { class: "muted small" }, ` — ${gapNumbers(g)}`)), ta);
  });
  S.nodes.actionList = h("div", { class: "diag-actions" });
  S.nodes.saveBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "diag-save-hint" }, "저장");
  S.nodes.discardBtn = h("button", { type: "button", class: "btn" }, "버리기");
  S.nodes.saveBtn.addEventListener("click", save);
  S.nodes.discardBtn.addEventListener("click", askDiscard);
  S.nodes.hint = h("p", { class: "muted hint", id: "diag-save-hint" });
  S.nodes.stale = h("p", { class: "warn-text", role: "status" });
  S.staleText = "";
  S.nodes.alerts = h("div", { class: "preview-alerts" });
  S.alertSig = null;
  fill(box, h("div", { class: "card preview-card" },
    h("h2", { id: "diag-preview-title" }, "설명 미리보기"),
    h("p", { class: "muted small" }, "저장하기 전에는 파일이 바뀌지 않습니다. 요약·갭 설명·다음 행동은 고칠 수 있고, 다른 화면으로 가면 고친 내용은 사라집니다."),
    h("div", { class: "field" }, h("label", { for: "diag-summary" }, "요약"), summary),
    ...gapFields,
    h("fieldset", { class: "field diag-action-set" }, h("legend", {}, `다음 행동 (1~${MAX_ACTIONS}개)`), S.nodes.actionList),
    S.nodes.stale,
    h("div", { class: "btn-row" }, S.nodes.saveBtn, S.nodes.discardBtn), S.nodes.hint, S.nodes.alerts));
  renderActions();
  updateActions();
  renderAlerts();
}

function renderActions(focusIndex) {
  const e = S.edits;
  const rows = e.actions.map((text, i) => {
    const input = h("input", { type: "text", "aria-label": `다음 행동 ${i + 1}`, maxlength: "200" });
    input.value = text;
    input.addEventListener("input", () => { e.actions[i] = input.value; updateActions(); });
    const del = h("button", { type: "button", class: "btn", "aria-label": `다음 행동 ${i + 1} 삭제`, disabled: e.actions.length <= 1 }, "삭제");
    del.addEventListener("click", () => {
      e.actions.splice(i, 1);
      renderActions(Math.min(i, e.actions.length - 1));
      updateActions();
    });
    return h("div", { class: "diag-action-row" }, input, del);
  });
  const add = h("button", { type: "button", class: "btn", disabled: e.actions.length >= MAX_ACTIONS }, "행동 추가");
  add.addEventListener("click", () => {
    e.actions.push("");
    renderActions(e.actions.length - 1);
    updateActions();
  });
  fill(S.nodes.actionList, ...rows, add);
  if (focusIndex !== undefined) {
    const input = S.nodes.actionList.querySelectorAll("input")[focusIndex];
    if (input) input.focus();
  }
}

function updateActions() {
  if (!S.preview || !S.nodes.saveBtn) return;
  const reason = saveBlocker();
  S.nodes.saveBtn.disabled = S.saving || !!reason;
  S.nodes.discardBtn.disabled = S.saving;
  S.nodes.hint.textContent = reason === STALE_TEXT ? "" : reason;
  const stale = previewStale() ? STALE_TEXT : "";
  if (S.nodes.stale && stale !== S.staleText) {       // announced once per change
    S.staleText = stale;
    S.nodes.stale.textContent = stale;
    S.nodes.stale.hidden = !stale;
  }
}

// Alerts live in their own node and are rebuilt only when their content changes (no re-announcing on keystrokes).
function renderAlerts() {
  const box = S.nodes.alerts;
  if (!box || !S.preview) return;
  const sig = JSON.stringify([S.saveError, S.confirmDiscard, S.saving]);
  if (sig === S.alertSig) return;
  S.alertSig = sig;
  const kids = [];
  if (S.confirmDiscard) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-discard-yes": "", disabled: S.saving }, "버리기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", discard);
    no.addEventListener("click", () => { S.confirmDiscard = false; renderAlerts(); S.nodes.discardBtn.focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "미리보기를 버립니다. 고친 내용은 사라집니다. 저장한 진단은 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (S.saveError) kids.push(h("p", { class: "error", role: "alert" }, S.saveError));
  fill(box, ...kids);
}

// ---- save / discard -----------------------------------------------------------

async function save() {
  const mine = S;
  if (mine.saving || !mine.preview || saveBlocker()) return;
  const jobId = mine.jobId;
  mine.saving = true;
  mine.saveError = null;
  updateActions();
  renderAlerts();
  renderRunBox();
  renderRecord();                               // toggles are aria-disabled while saving
  try {
    const res = await mine.ctx.api("POST", "/api/diagnose/save",
      { category: mine.category, target: mine.path, notes: notesPayload(), job_id: jobId });
    markHandled(jobId);                         // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.ctx.toast("진단을 기록했습니다", "info");
    setRecord(mine, res.record);
    if (mine.entry) mine.entry.diagnosed = { date: res.record.fm.date, nearest: res.record.fm.nearest };
    mine.preview = null;
    mine.edits = null;
    mine.job = null;
    mine.jobId = null;
    mine.saving = false;
    renderRunBox();
    renderCompare();
    renderRecord();
    renderPreview();
    focusStatus();
    mine.ctx.refreshCategories();               // home step bar
  } catch (e) {
    // An aborted request (page left mid-save) may still have been saved by the server: never restore that preview.
    if (e && e.error === "aborted") markHandled(jobId);
    if (!alive(mine) || ignorable(e)) return;
    mine.saveError = e.message || "저장하지 못했습니다";
    mine.saving = false;
    renderRecord();
    updateActions();
    renderAlerts();
    renderRunBox();
  }
}

function askDiscard() {
  if (S.saving) return;
  S.confirmDiscard = true;
  renderAlerts();
  const yes = S.nodes.alerts && S.nodes.alerts.querySelector("[data-discard-yes]");
  if (yes) yes.focus();
}

function discard() {
  if (S.saving) return;
  markHandled(S.jobId);
  S.preview = null;
  S.edits = null;
  S.job = null;
  S.jobId = null;
  S.saveError = null;
  S.confirmDiscard = false;
  S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  renderRunBox();
  renderPreview();
  focusStatus();
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  S = freshState(el, ctx, ctx.category, params && params.path ? params.path : null);
  await load();
}

// Switching category while a target is open goes back to the list (the target belongs to the old category).
export function onCategory(category) {
  if (!S || S.category === category) return;
  stopPolling();
  if (S.path) {
    location.hash = "#/diagnose";               // the router remounts the list with the new category
    return;
  }
  S = freshState(S.el, S.ctx, category, null);
  load();
}

export function unmount() {
  stopPolling();
  S = null;
}
