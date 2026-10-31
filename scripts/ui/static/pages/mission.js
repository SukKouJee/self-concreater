// 미션 화면
//   오늘 상태(미션 없음/있음) → "오늘 미션 받기"(작업: 자료 모으기 → Claude) → 미리보기 → "이걸로 할게요"(저장).
//   있으면 "완료 제출"(피드백 받기 → 확인 후 저장) / "건너뛰기"(사유 선택 후 확인) / "바꾸기"(교체 미리보기 → 저장).
//   Claude는 받기·다른 걸로·바꾸기·피드백 받기 클릭 때만 부른다. 파일 쓰기는 항상 사용자가 누른 저장/확인 뒤.
import { h, fill } from "/static/dom.js";
import { isServer, t } from "/static/copy.js";
import { renderMarkdown } from "/static/md.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const DAY_CHECK_MS = 60000;
const HANDLED_KEY = "ytc.mission.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const MAX_NOTE = 300;
const MAX_REASON = 100;
const STATUS_KO = { open: "진행 중", submitted: "완료", skipped: "건너뜀" };
const STATUS_ICON = { open: "…", submitted: "✓", skipped: "–" };
const DIFF_OPTIONS = [["easy", "쉬움"], ["right", "적당"], ["hard", "어려움"]];
const DIFF_KO = { easy: "쉬움", right: "적당", hard: "어려움" };
const SKIP_REASONS = ["시간 부족", "재료 없음", "하기 싫음"];
const OTHER_REASON = "기타";
const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];
const STEP_TEXT = {
  mission: { context: "자료 모으는 중", claude: "Claude가 미션 만드는 중" },
  mission_feedback: { copy: "파일 복사하는 중", claude: "Claude가 피드백 쓰는 중" },
};
const JOB_LABEL = { mission: "미션 만들기 진행", mission_feedback: "피드백 받기 진행" };

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const ignorable = (e) => !e || e.error === "aborted";

function localDay() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function shortDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso));
  if (!m) return String(iso);
  const wd = WEEKDAY[new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getDay()];
  return `${m[2]}-${m[3]} (${wd})`;
}

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved (or closed) in this browser is not restored after a reload.
function markHandled(jobId) {
  if (!jobId) return;
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

function itemOf(job) {
  return job && job.items && job.items[0] ? job.items[0] : null;
}

// Preview of a finished job for the screen's date, or null.
function previewOf(job, kind, day) {
  const item = itemOf(job);
  if (!item || item.id !== day || item.state !== "awaiting" || !item.preview) return null;
  if (kind === "mission") return item.preview.mission && typeof item.preview.mission === "object" ? item.preview : null;
  return typeof item.preview.feedback === "string" ? item.preview : null;
}

// ---- page state ---------------------------------------------------------------

let S = null;

function freshState(el, ctx) {
  return {
    el, ctx, date: null, localAtLoad: localDay(), stale: false, data: null,
    kind: null, job: null, jobId: null, preview: null, fb: null, fbText: "",
    mode: null, form: { note: "", difficulty: "", file: "" }, skip: { reason: "", other: "" },
    confirmSwap: false, confirmOverwrite: false, confirmRetry: false,
    busy: false, runError: null, saveError: null, conflict: false, formErrors: {}, skipError: null,
    timer: null, dayTimer: null, pollErrors: 0, refreshSeq: 0, nodes: {},
  };
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

function focusNode(node) {
  if (!node) return;
  if (!/^(INPUT|TEXTAREA|BUTTON|SELECT|A)$/.test(node.tagName)) node.setAttribute("tabindex", "-1");
  node.focus({ preventScroll: true });
}

const focusStatus = () => focusNode(S && S.nodes.status);

function clearMessages(mine) {
  mine.runError = null;
  mine.saveError = null;
  mine.conflict = false;
  mine.formErrors = {};
  mine.skipError = null;
}

const todayOf = () => (S.data ? S.data.today : null);

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  fill(mine.el, h("h1", {}, "미션"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const [data, mj, fj] = await Promise.all([
      mine.ctx.api("GET", "/api/mission"),
      mine.ctx.api("GET", "/api/jobs/current?kind=mission"),
      mine.ctx.api("GET", "/api/jobs/current?kind=mission_feedback"),
    ]);
    if (!alive(mine)) return;
    mine.data = data;
    mine.date = data.date;
    mine.localAtLoad = localDay();
    reenter(mine, [["mission_feedback", fj.job], ["mission", mj.job]]);
    render();
    if (isActive(mine.job)) schedulePoll();
    startDayWatch();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e);
  }
}

// Restore one job of this screen's date: an active one first, then a finished preview, then a failure (shown once).
function reenter(mine, candidates) {
  const mine_ = candidates.filter(([kind, job]) => {
    const item = itemOf(job);
    return job && item && item.id === mine.date && !isHandled(job.id)
      && (kind === "mission_feedback" ? !!mine.data.today : true);
  });
  const usable = ([kind, job]) => {
    if (job.status !== "done") return false;
    const pv = previewOf(job, kind, mine.date);
    // A first-draft preview is stale once the day already has a mission (saved elsewhere).
    return !!pv && !(kind === "mission" && !pv.replace && mine.data.today);
  };
  const pick = mine_.find(([, job]) => isActive(job)) || mine_.find(usable)
    || mine_.find(([, job]) => job.status === "failed" || job.status === "stopped");
  for (const [kind, job] of mine_) {
    if (job.status === "done" && !usable([kind, job])) markHandled(job.id);      // stale preview
  }
  if (!pick) return;
  const [kind, job] = pick;
  if (job.status === "failed" || job.status === "stopped") {
    const item = itemOf(job);
    mine.runError = `${(item && item.message) || job.message || "작업을 마치지 못했습니다"}`;
    mine.mode = kind === "mission_feedback" ? "submit" : (mine.data.today ? "swap" : null);
    markHandled(job.id);
    return;
  }
  adoptJob(mine, job, kind);
}

function loadError(e) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) load(); });
  fill(S.el, h("h1", {}, "미션"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

// Fresh state for a new day / after another tab saved a mission.
function reloadAll() {
  stopPolling();
  stopDayWatch();
  S = freshState(S.el, S.ctx);
  load();
}

function adoptJob(mine, job, kind) {
  mine.job = job;
  mine.kind = kind;
  if (kind === "mission") mine.mode = mine.data.today ? "swap" : null;
  else mine.mode = "submit";
  if (job.status !== "done") return;
  const pv = previewOf(job, kind, mine.date);
  if (!pv || mine.jobId === job.id) return;
  mine.jobId = job.id;
  if (kind === "mission") {
    mine.preview = pv;
    mine.fb = null;
  } else {
    mine.fb = pv;
    mine.fbText = pv.feedback;
    mine.preview = null;
    mine.form.note = pv.note || mine.form.note;
    mine.form.difficulty = pv.difficulty || mine.form.difficulty;
  }
  mine.confirmOverwrite = false;
  mine.confirmRetry = false;
}

// ---- date watch ---------------------------------------------------------------

function checkDay() {
  if (!S || !S.ctx.isCurrent() || !S.data) return;
  const stale = localDay() !== S.localAtLoad;
  if (stale === S.stale) return;
  S.stale = stale;
  renderStale();
  renderToday();
}

function startDayWatch() {
  stopDayWatch();
  S.dayTimer = setInterval(checkDay, DAY_CHECK_MS);
  document.addEventListener("visibilitychange", checkDay);
  window.addEventListener("focus", checkDay);
}

function stopDayWatch() {
  if (S && S.dayTimer) { clearInterval(S.dayTimer); S.dayTimer = null; }
  document.removeEventListener("visibilitychange", checkDay);
  window.removeEventListener("focus", checkDay);
}

// ---- jobs ---------------------------------------------------------------------

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
      mine.job = data.job;
      if (wasActive && !isActive(mine.job)) {
        finishJob(mine);
        return;
      }
      updateProgress();
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 시도하세요", "error");
        mine.job = null;
        render();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

function finishJob(mine) {
  const job = mine.job;
  const kind = mine.kind;
  if (job.status === "done") {
    adoptJob(mine, job, kind);
    if (kind === "mission" && mine.preview && !mine.preview.replace && mine.data.today) {
      // Saved elsewhere while this first-draft preview was running: never offer to save it.
      markHandled(job.id);
      mine.preview = null;
      mine.job = null;
      mine.jobId = null;
      mine.mode = null;
      mine.conflict = true;
    }
  } else {
    const item = itemOf(job);
    mine.runError = (item && item.message) || job.message || "작업을 마치지 못했습니다";
    markHandled(job.id);
    mine.job = null;
  }
  renderToday(job.status === "done" ? (kind === "mission" ? "[data-preview-title]" : "[data-fb-text]") : null);
}

function updateProgress() {
  const node = S.nodes.progressText;
  if (!node || !S.job) return;
  const item = itemOf(S.job);
  const text = `${(item && STEP_TEXT[S.kind] && STEP_TEXT[S.kind][item.step]) || "준비 중"}…`;
  if (node.textContent !== text) node.textContent = text;
}

function progressNode() {
  const item = itemOf(S.job);
  const step = (item && STEP_TEXT[S.kind] && STEP_TEXT[S.kind][item.step]) || "준비 중";
  S.nodes.progressText = h("span", {}, `${step}…`);
  return h("div", { class: "job-status", role: "status" },
    h("progress", { class: "job-progress", "aria-label": JOB_LABEL[S.kind] || "진행" }), S.nodes.progressText);
}

async function startDraft(replace) {
  const mine = S;
  if (mine.busy || isActive(mine.job) || mine.stale) return;
  mine.busy = true;
  if (mine.jobId) markHandled(mine.jobId);
  clearMessages(mine);
  mine.confirmSwap = false;
  mine.preview = null;
  mine.jobId = null;
  mine.mode = replace ? "swap" : null;
  renderToday();
  try {
    const res = await mine.ctx.api("POST", "/api/mission/draft", { date: mine.date, replace });
    if (!alive(mine)) return;
    mine.pollErrors = 0;
    adoptJob(mine, res.job, "mission");
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    if (e.error === "mission_exists") mine.conflict = true;
    else mine.runError = e.message || "미션을 만들지 못했습니다";
  } finally {
    if (alive(mine)) {
      mine.busy = false;
      renderToday(mine.job && isActive(mine.job) ? "[data-progress]" : null);
    }
  }
}

async function startFeedback() {
  const mine = S;
  if (mine.busy || isActive(mine.job)) return;
  const errors = validateForm();
  if (Object.keys(errors).length) {
    mine.formErrors = errors;
    renderToday(errors.note ? "#mi-note" : errors.difficulty ? "[name=mi-diff]" : "#mi-file");
    return;
  }
  mine.busy = true;
  if (mine.jobId) markHandled(mine.jobId);
  clearMessages(mine);
  mine.confirmRetry = false;
  mine.fb = null;
  mine.jobId = null;
  renderToday();
  try {
    const res = await mine.ctx.api("POST", "/api/mission/feedback", {
      date: mine.date, note: mine.form.note.trim(), difficulty: mine.form.difficulty, file: mine.form.file.trim(),
    });
    if (!alive(mine)) return;
    mine.pollErrors = 0;
    adoptJob(mine, res.job, "mission_feedback");
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    if (e.error === "bad_file") mine.formErrors = { file: e.message || "이 파일은 낼 수 없습니다" };
    else if (e.error === "no_mission") { mine.runError = e.message; refreshData(true); }
    else mine.runError = e.message || "피드백을 받지 못했습니다";
  } finally {
    if (alive(mine)) {
      mine.busy = false;
      renderToday(mine.job && isActive(mine.job) ? "[data-progress]" : (mine.formErrors.file ? "#mi-file" : null));
    }
  }
}

function validateForm() {
  const f = S.form;
  const errors = {};
  const note = f.note.trim();
  if (!note) errors.note = "한 일을 한두 줄로 적어 주세요";
  else if (note.length > MAX_NOTE) errors.note = `${MAX_NOTE}자까지 쓸 수 있어요`;
  if (!DIFF_KO[f.difficulty]) errors.difficulty = "난이도를 골라 주세요";
  return errors;
}

// ---- save / submit / skip -----------------------------------------------------

async function saveDraft() {
  const mine = S;
  if (mine.busy || !mine.preview) return;
  const pv = mine.preview;
  const jobId = mine.jobId;
  mine.busy = true;
  clearMessages(mine);
  renderToday();
  try {
    const res = await mine.ctx.api("POST", "/api/mission/save", {
      date: mine.date, mission: pv.mission, replace: !!pv.replace, job_id: jobId,
    });
    markHandled(jobId);                         // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.ctx.toast(pv.replace ? "오늘 미션을 바꿨습니다" : "오늘 미션을 저장했습니다", "info");
    mine.data.today = res.today;
    mine.preview = null;
    mine.job = null;
    mine.jobId = null;
    mine.mode = null;
    mine.busy = false;
    renderToday("[data-today-title]");
    refreshData();
  } catch (e) {
    if (e && e.error === "aborted") markHandled(jobId);   // the server may have saved before the page was left
    if (!alive(mine) || ignorable(e)) return;
    if (e.error === "mission_exists") mine.conflict = true;
    else mine.saveError = e.message || "저장하지 못했습니다";
    mine.busy = false;
    renderToday("[data-save-error]");
  }
}

function closePreview() {
  if (S.busy) return;
  markHandled(S.jobId);
  S.preview = null;
  S.job = null;
  S.jobId = null;
  S.mode = null;
  clearMessages(S);
  S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  renderToday("[data-today-focus]");
}

async function submitFeedback() {
  const mine = S;
  if (mine.busy || !mine.fb) return;
  const text = mine.fbText.trim();
  if (!text) {
    mine.saveError = "피드백이 비어 있습니다. 내용을 쓰거나 다시 받아 주세요";
    renderToday("[data-fb-text]");
    return;
  }
  const today = todayOf();
  if (today && today.status === "submitted" && !mine.confirmOverwrite) {
    mine.confirmOverwrite = true;
    renderToday("[data-overwrite-yes]");
    return;
  }
  const fb = mine.fb;
  const jobId = mine.jobId;
  mine.busy = true;
  mine.confirmOverwrite = false;
  clearMessages(mine);
  renderToday();
  try {
    const res = await mine.ctx.api("POST", "/api/mission/submit", {
      date: mine.date, note: fb.note, difficulty: fb.difficulty, feedback: text,
      file_name: fb.file_name || null, job_id: jobId,
    });
    markHandled(jobId);
    if (!alive(mine)) return;
    mine.ctx.toast("제출을 저장했습니다", "info");
    mine.data.today = res.today;
    mine.fb = null;
    mine.fbText = "";
    mine.job = null;
    mine.jobId = null;
    mine.mode = null;
    mine.form = { note: "", difficulty: "", file: "" };
    mine.busy = false;
    renderToday("[data-today-title]");
    refreshData();
  } catch (e) {
    if (e && e.error === "aborted") markHandled(jobId);
    if (!alive(mine) || ignorable(e)) return;
    mine.saveError = e.message || "저장하지 못했습니다";
    mine.busy = false;
    if (e.error === "no_mission") refreshData(true);
    renderToday("[data-save-error]");
  }
}

function skipReasonValue() {
  const r = S.skip.reason;
  return r === OTHER_REASON ? S.skip.other.trim() : r;
}

async function skipMission() {
  const mine = S;
  if (mine.busy) return;
  const reason = skipReasonValue();
  if (!mine.skip.reason) mine.skipError = "건너뛰는 이유를 골라 주세요";
  else if (!reason) mine.skipError = "이유를 적어 주세요";
  else if (reason.length > MAX_REASON) mine.skipError = `이유는 ${MAX_REASON}자까지 쓸 수 있어요`;
  else mine.skipError = null;
  if (mine.skipError) { renderToday("[data-skip-error]"); return; }
  mine.busy = true;
  clearMessages(mine);
  renderToday();
  try {
    const res = await mine.ctx.api("POST", "/api/mission/skip", { date: mine.date, reason });
    if (!alive(mine)) return;
    mine.ctx.toast("오늘 미션을 건너뛰었습니다", "info");
    mine.data.today = res.today;
    mine.mode = null;
    mine.skip = { reason: "", other: "" };
    mine.busy = false;
    renderToday("[data-today-title]");
    refreshData();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.skipError = e.message || "건너뛰지 못했습니다";
    mine.busy = false;
    if (e.error === "already_submitted" || e.error === "no_mission") refreshData(true);
    renderToday("[data-skip-error]");
  }
}

// Re-reads the day (history, week, today) without touching an in-progress form. `withToday` also replaces today's record.
async function refreshData(withToday) {
  const mine = S;
  const seq = ++mine.refreshSeq;
  try {
    const d = await mine.ctx.api("GET", `/api/mission?date=${encodeURIComponent(mine.date)}`);
    if (!alive(mine) || seq !== mine.refreshSeq) return;
    mine.data.history = d.history;
    mine.data.week = d.week;
    if (withToday) { mine.data.today = d.today; renderToday(); }
    renderHistory();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.ctx.toast("기록을 새로 불러오지 못했습니다", "error");
  }
}

// ---- mode switches ------------------------------------------------------------

const locked = () => S.busy || isActive(S.job) || !!S.preview || !!S.fb;

function openMode(mode) {
  if (locked() || S.mode === mode) return;
  clearMessages(S);
  S.confirmSwap = false;
  S.mode = mode;
  const sel = mode === "submit" ? "#mi-note" : mode === "skip" ? "[name=mi-skip]" : "[data-swap-yes]";
  if (mode === "swap") S.confirmSwap = true;
  renderToday(sel);
}

function closeMode() {
  if (locked()) return;
  clearMessages(S);
  S.mode = null;
  S.confirmSwap = false;
  renderToday("[data-today-focus]");
}

// ---- render -------------------------------------------------------------------

function render() {
  S.nodes = {};
  S.nodes.status = h("h1", { id: "mi-title" }, "미션");
  S.nodes.stale = h("div", { class: "mi-stale" });
  S.nodes.today = h("section", { class: "mi-sec", "aria-labelledby": "mi-today-title" });
  S.nodes.history = h("section", { class: "mi-sec", "aria-labelledby": "mi-history-title" });
  const d = S.data;
  const kids = [
    S.nodes.status,
    h("p", { class: "lead" }, `${d.date} · 성장 단계: ${d.stage}`),
    S.nodes.stale,
  ];
  if (!d.has_profile) kids.push(profileNote());
  kids.push(S.nodes.today, S.nodes.history);
  fill(S.el, ...kids);
  S.stale = localDay() !== S.localAtLoad;
  renderStale();
  renderToday();
  renderHistory();
}

function profileNote() {
  return h("div", { class: "mi-profile" },
    h("p", { class: "muted" }, t("mission.profileNote")));
}

function renderStale() {
  const box = S.nodes.stale;
  if (!box) return;
  if (!S.stale) { fill(box); return; }
  const btn = h("button", { type: "button", class: "btn btn-primary" }, "날짜가 바뀌었습니다 — 새로 불러오기");
  btn.addEventListener("click", reloadAll);
  fill(box, h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, `화면은 ${S.date} 기준입니다. 새로 불러오면 오늘 날짜의 미션을 보여 줍니다.`),
    h("div", { class: "btn-row" }, btn)));
}

function btn(label, cls, onClick, attrs = {}) {
  const b = h("button", { type: "button", class: `btn ${cls}`.trim(), ...attrs }, label);
  b.addEventListener("click", onClick);
  return b;
}

// Rebuilds the "today" section. `focusSel` (optional) is focused afterwards, so keyboard users keep their place.
function renderToday(focusSel) {
  const box = S.nodes.today;
  if (!box) return;
  S.nodes.progressText = null;
  const d = S.data;
  const today = d.today;
  const kids = [h("h2", { id: "mi-today-title" }, "오늘 미션")];
  if (S.conflict) kids.push(conflictNode());
  kids.push(...(today ? existsView(today) : emptyView(d)));
  fill(box, ...kids);
  if (focusSel === "[data-today-focus]") focusNode(box.querySelector("h2"));
  else if (focusSel) focusNode(box.querySelector(focusSel));
}

function conflictNode() {
  const b = btn("불러오기", "btn-primary", reloadAll, { "data-conflict-load": "" });
  return h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, "다른 곳에서 오늘 미션이 저장됐습니다 — 불러오기"),
    h("div", { class: "btn-row" }, b));
}

function errorNode(text, extra = {}) {
  return text ? h("p", { class: "error", role: "alert", ...extra }, text) : null;
}

// ---- today: no mission yet ----------------------------------------------------

function emptyView(d) {
  if (isActive(S.job) && S.kind === "mission") return [progressBox()];
  if (S.preview && !S.preview.replace) return [previewCard(S.preview, false)];
  const get = btn("오늘 미션 받기", "btn-primary", () => startDraft(false), { disabled: S.busy || S.stale });
  const kids = [
    h("p", { class: "muted" }, "오늘 미션이 아직 없습니다."),
    h("div", { class: "btn-row" }, get, h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (S.stale) kids.push(h("p", { class: "muted small" }, "날짜가 바뀌었습니다. 새로 불러온 뒤 받을 수 있어요"));
  if (S.busy) kids.push(progressBox(true));
  kids.push(errorNode(S.runError));
  return kids;
}

function progressBox(starting) {
  if (starting || !S.job) {
    S.nodes.progressText = h("span", {}, "시작하는 중…");
    return h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "시작" }), S.nodes.progressText);
  }
  const node = progressNode();
  node.setAttribute("data-progress", "");
  node.setAttribute("tabindex", "-1");
  return node;
}

function adjustText(rec) {
  if (!rec) return null;
  if (rec.adjust === "easier") return "지난 평가 반영해 5분 줄였어요";
  if (rec.adjust === "harder") return "지난 평가 반영해 5분 늘렸어요";
  return null;
}

function previewCard(pv, swap) {
  const m = pv.mission;
  const title = h("h3", { "data-preview-title": "", tabindex: "-1" }, m.title);
  const note = adjustText(pv.recommended);
  const label = swap ? "이걸로 바꿀게요" : "이걸로 할게요";
  const save = btn(label, "btn-primary", saveDraft, { disabled: S.busy });
  const again = btn("다른 걸로", "", () => startDraft(!!swap), { disabled: S.busy || S.stale });
  const close = btn("닫기", "", closePreview, { disabled: S.busy });
  const kids = [
    h("div", { class: "card-head" },
      h("div", { class: "mi-badges" },
        h("span", { class: "badge mi-kind" }, m.kind), h("span", { class: "badge" }, `${m.minutes}분`),
        swap ? h("span", { class: "badge" }, "교체 미리보기") : h("span", { class: "badge" }, "미리보기"))),
    title,
    note ? h("p", { class: "small mi-adjust" }, note) : null,
    m.why ? h("p", {}, h("span", { class: "muted" }, "왜 "), m.why) : null,
    m.steps && m.steps.length
      ? h("ol", { class: "mi-steps", "aria-label": "단계" }, ...m.steps.map((s) => h("li", {}, s))) : null,
    m.deliverable ? h("p", {}, h("span", { class: "muted" }, "결과물 "), m.deliverable) : null,
    m.source ? h("p", { class: "small" }, h("span", { class: "muted" }, "재료 "), m.source) : null,
    h("p", { class: "muted small" }, "저장하기 전에는 파일이 바뀌지 않습니다."),
    h("div", { class: "btn-row" }, save, again, close),
    errorNode(S.saveError, { "data-save-error": "", tabindex: "-1" }),
    errorNode(S.runError),
  ];
  return h("article", { class: "card preview-card", "aria-label": "미션 미리보기" }, ...kids);
}

// ---- today: mission exists ----------------------------------------------------

function bodyNodes(today) {
  const parts = Array.isArray(today.body) ? today.body : [];
  const out = [];
  for (const p of parts) {
    if (!(p.text || "").trim()) continue;
    const md = renderMarkdown(p.text || "", { dropTitle: `${today.date} 미션` });
    if (!md.childNodes.length) continue;
    out.push(h("div", { class: "mi-part" }, p.heading ? h("h3", {}, p.heading) : null, h("div", { class: "md" }, md)));
  }
  return out;
}

function existsView(today) {
  const status = today.status || "open";
  const badges = [h("span", { class: `badge mi-status mi-${status}` }, STATUS_KO[status] || String(status))];
  if (today.kind) badges.push(h("span", { class: "badge mi-kind" }, today.kind));
  if (today.minutes) badges.push(h("span", { class: "badge" }, `${today.minutes}분`));
  if (today.difficulty) badges.push(h("span", { class: "badge" }, `난이도 ${DIFF_KO[today.difficulty] || today.difficulty}`));
  if (today.skip_reason) badges.push(h("span", { class: "badge" }, `사유 ${today.skip_reason}`));
  const head = h("h3", { "data-today-title": "", tabindex: "-1" }, today.title || `${today.date} 미션`);
  const body = bodyNodes(today);
  const busy = locked();
  const swapDisabled = busy || S.stale;
  const actions = h("div", { class: "btn-row", role: "group", "aria-label": "오늘 미션 동작" },
    btn(status === "submitted" ? "다시 제출" : "완료 제출", S.mode === "submit" ? "btn-primary" : "", () => openMode("submit"),
      { disabled: busy, "aria-pressed": String(S.mode === "submit") }),
    status === "submitted" ? null
      : btn("건너뛰기", "", () => openMode("skip"), { disabled: busy, "aria-pressed": String(S.mode === "skip") }),
    status === "submitted" ? null
      : btn("바꾸기", "", () => openMode("swap"), { disabled: swapDisabled, "aria-pressed": String(S.mode === "swap") }));
  const kids = [
    h("article", { class: "card mi-today" },
      h("div", { class: "card-head" }, h("div", { class: "mi-badges" }, ...badges)),
      head, ...body, actions,
      S.stale ? h("p", { class: "muted small" }, "날짜가 바뀌어 바꾸기는 새로 불러온 뒤 할 수 있어요") : null),
  ];
  const panel = modePanel(today);
  if (panel) kids.push(panel);
  return kids;
}

function modePanel(today) {
  if (S.mode === "submit") return submitPanel(today);
  if (S.mode === "skip") return skipPanel();
  if (S.mode === "swap") return swapPanel();
  return null;
}

// ---- submit panel -------------------------------------------------------------

function submitPanel(today) {
  if (S.fb) return feedbackPanel(today);
  const active = isActive(S.job) && S.kind === "mission_feedback";
  const disabled = active || S.busy;
  const f = S.form;
  const e = S.formErrors;
  const note = h("textarea", {
    id: "mi-note", rows: "3", maxlength: String(MAX_NOTE), required: true, disabled,
    "aria-invalid": e.note ? "true" : false, "aria-describedby": e.note ? "mi-note-err" : false,
  });
  note.value = f.note;
  note.addEventListener("input", () => { f.note = note.value; });
  const diff = h("fieldset", { class: "field seg", disabled },
    h("legend", {}, "난이도 (필수)"),
    ...DIFF_OPTIONS.map(([val, label]) => {
      const r = h("input", { type: "radio", name: "mi-diff", value: val, checked: f.difficulty === val });
      r.addEventListener("change", () => { f.difficulty = val; });
      return h("label", { class: "seg-opt" }, r, h("span", {}, label));
    }));
  const file = h("input", {
    id: "mi-file", type: "text", disabled, placeholder: "예: C:\\Users\\me\\shot.png",
    "aria-describedby": e.file ? "mi-file-hint mi-file-err" : "mi-file-hint", "aria-invalid": e.file ? "true" : false,
  });
  file.value = f.file;
  file.addEventListener("input", () => { f.file = file.value; });
  const get = btn("피드백 받기", "btn-primary", startFeedback, { disabled });
  const close = btn("닫기", "", closeMode, { disabled });
  const kids = [
    h("h3", { id: "mi-submit-title" }, "완료 제출"),
    h("div", { class: "field" },
      h("label", { for: "mi-note" }, "한 일 (필수, 300자까지)"), note,
      e.note ? h("p", { class: "field-error error", id: "mi-note-err", role: "alert" }, e.note) : null),
    diff,
    e.difficulty ? h("p", { class: "field-error error", role: "alert" }, e.difficulty) : null,
    // a file location on the user's PC means nothing to the hosted server edition: no field there
    isServer() ? null : h("div", { class: "field" },
      h("label", { for: "mi-file" }, "첨부할 사진·스크린샷·글 파일 위치(선택)"), file,
      h("p", { class: "muted small", id: "mi-file-hint" },
        "원본은 건드리지 않고 잠깐 복사해 읽은 뒤 지웁니다. 이미지(png·jpg·webp·gif)와 텍스트(txt·md)만, 10MB까지 낼 수 있어요. 영상은 한 일에 설명으로 적어 주세요"),
      e.file ? h("p", { class: "field-error error", id: "mi-file-err", role: "alert" }, e.file) : null),
    h("div", { class: "btn-row" }, get, close, h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (active) kids.push(progressBox());
  kids.push(errorNode(S.runError));
  if (today.status === "submitted") kids.push(h("p", { class: "muted small" }, "이미 완료한 미션입니다. 다시 제출하면 이전 제출을 덮어씁니다"));
  return h("section", { class: "card mi-panel", "aria-labelledby": "mi-submit-title" }, ...kids);
}

function feedbackPanel(today) {
  const fb = S.fb;
  const text = h("textarea", { rows: "6", "data-fb-text": "", disabled: S.busy });
  text.value = S.fbText;
  text.addEventListener("input", () => { S.fbText = text.value; });
  const save = btn("저장", "btn-primary", submitFeedback, { disabled: S.busy });
  const again = btn("다시 받기", "", () => {
    if (S.fbText !== fb.feedback && !S.confirmRetry) {
      S.confirmRetry = true;
      renderToday("[data-retry-yes]");
      return;
    }
    startFeedback();
  }, { disabled: S.busy });
  const back = btn("내용 고치기", "", () => {
    if (S.busy) return;
    markHandled(S.jobId);
    S.fb = null;
    S.jobId = null;
    S.job = null;
    S.confirmOverwrite = false;
    S.confirmRetry = false;
    renderToday("#mi-note");
  }, { disabled: S.busy });
  const kids = [
    h("h3", { id: "mi-fb-title" }, "피드백 확인"),
    h("dl", { class: "mi-summary" },
      h("dt", {}, "한 일"), h("dd", {}, fb.note),
      h("dt", {}, "난이도"), h("dd", {}, DIFF_KO[fb.difficulty] || fb.difficulty),
      fb.file_name ? h("dt", {}, "파일") : null, fb.file_name ? h("dd", {}, fb.file_name) : null),
    h("div", { class: "field" }, h("label", {}, "피드백 (저장 전에 고칠 수 있어요)", text)),
    h("p", { class: "muted small" }, "저장하기 전에는 기록이 바뀌지 않습니다."),
    h("div", { class: "btn-row" }, save, again, back),
  ];
  if (S.confirmRetry) {
    const yes = btn("다시 받기", "btn-danger", () => { S.confirmRetry = false; startFeedback(); }, { "data-retry-yes": "" });
    const no = btn("취소", "", () => { S.confirmRetry = false; renderToday("[data-fb-text]"); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "고친 피드백은 사라집니다. 다시 받을까요?"), h("div", { class: "btn-row" }, yes, no)));
  }
  if (S.confirmOverwrite) {
    const yes = btn("덮어쓰기", "btn-danger", submitFeedback, { "data-overwrite-yes": "" });
    const no = btn("취소", "", () => { S.confirmOverwrite = false; renderToday("[data-fb-text]"); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "다시 제출하면 이전 제출을 덮어씁니다"), h("div", { class: "btn-row" }, yes, no)));
  }
  kids.push(errorNode(S.saveError, { "data-save-error": "", tabindex: "-1" }));
  return h("section", { class: "card mi-panel preview-card", "aria-labelledby": "mi-fb-title" }, ...kids);
}

// ---- skip panel ---------------------------------------------------------------

function skipPanel() {
  const options = [...SKIP_REASONS, OTHER_REASON];
  const radios = options.map((label) => {
    const r = h("input", { type: "radio", name: "mi-skip", value: label, checked: S.skip.reason === label, disabled: S.busy });
    r.addEventListener("change", () => {
      S.skip.reason = label;
      const other = S.nodes.today.querySelector("#mi-skip-other");
      if (other) { other.disabled = label !== OTHER_REASON; if (label === OTHER_REASON) other.focus(); }
    });
    return h("label", { class: "check" }, r, label);
  });
  const other = h("input", {
    id: "mi-skip-other", type: "text", maxlength: String(MAX_REASON), "aria-label": "기타 사유",
    disabled: S.busy || S.skip.reason !== OTHER_REASON,
  });
  other.value = S.skip.other;
  other.addEventListener("input", () => { S.skip.other = other.value; });
  const ok = btn("건너뛰기 확인", "btn-danger", skipMission, { disabled: S.busy });
  const close = btn("닫기", "", closeMode, { disabled: S.busy });
  return h("section", { class: "card mi-panel", "aria-labelledby": "mi-skip-title" },
    h("h3", { id: "mi-skip-title" }, "건너뛰기"),
    h("fieldset", { class: "field checks" }, h("legend", {}, "건너뛰는 이유"), ...radios, other),
    h("div", { class: "btn-row" }, ok, close),
    S.skipError ? h("p", { class: "error", role: "alert", "data-skip-error": "", tabindex: "-1" }, S.skipError) : null);
}

// ---- swap panel ---------------------------------------------------------------

function swapPanel() {
  if (isActive(S.job) && S.kind === "mission") {
    return h("section", { class: "card mi-panel", "aria-label": "미션 바꾸기" }, progressBox());
  }
  if (S.preview && S.preview.replace) return previewCard(S.preview, true);
  if (S.busy) return h("section", { class: "card mi-panel", "aria-label": "미션 바꾸기" }, progressBox(true));
  const kids = [];
  if (S.confirmSwap) {
    const yes = btn("바꾸기 시작", "btn-danger", () => startDraft(true), { "data-swap-yes": "", disabled: S.busy || S.stale });
    const no = btn("취소", "", closeMode);
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "오늘 미션을 다른 재료로 바꿉니다"),
      h("p", { class: "muted small" }, "Claude를 한 번 부릅니다. 미리보기에서 확인하기 전에는 지금 미션이 그대로 남습니다."),
      h("div", { class: "btn-row" }, yes, no)));
  } else {
    kids.push(h("div", { class: "btn-row" }, btn("다시 시도", "", () => { S.confirmSwap = true; renderToday("[data-swap-yes]"); }),
      btn("닫기", "", closeMode)));
  }
  kids.push(errorNode(S.runError));
  return h("section", { class: "card mi-panel", "aria-label": "미션 바꾸기" }, ...kids);
}

// ---- history ------------------------------------------------------------------

function weekText(w) {
  if (!w || !w.total) return "이번 주는 아직 기록이 없습니다";
  return `이번 주 ${w.done}/${w.total} 완료(${Math.round((w.rate || 0) * 100)}%)`;
}

function historyRow(e) {
  const status = e.status || "none";
  const label = status === "none" ? "없음" : (STATUS_KO[status] || String(status));
  const icon = status === "none" ? "·" : (STATUS_ICON[status] || "");
  const detail = status === "skipped" && e.skip_reason ? ` (${e.skip_reason})` : "";
  return h("tr", { class: `mi-row mi-row-${status}` },
    h("th", { scope: "row" }, shortDate(e.date)),
    h("td", {}, h("span", { "aria-hidden": "true" }, `${icon} `), `${label}${detail}`),
    h("td", {}, e.difficulty ? DIFF_KO[e.difficulty] || e.difficulty : "-"),
    h("td", {}, e.title ? `${e.title}${e.kind ? ` · ${e.kind}` : ""}` : "-"));
}

function renderHistory() {
  const box = S.nodes.history;
  if (!box) return;
  const d = S.data;
  const list = Array.isArray(d.history) ? d.history : [];
  const kids = [
    h("h2", { id: "mi-history-title" }, "최근 기록"),
    h("p", { class: "mi-week", role: "status" }, weekText(d.week)),
  ];
  if (!list.some((e) => e.status)) {
    kids.push(h("p", { class: "muted" }, "지난 14일 기록이 아직 없습니다. 오늘 미션부터 시작해 보세요"));
  } else {
    const rows = list.slice().reverse().map(historyRow);       // newest first
    kids.push(h("div", { class: "table-wrap mi-table-wrap" },
      h("table", { class: "preview-table mi-table" },
        h("caption", { class: "sr-only" }, "지난 14일 미션 기록"),
        h("thead", {}, h("tr", {}, ...["날짜", "상태", "난이도", "미션"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...rows))));
  }
  fill(box, ...kids);
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  if (S) { stopPolling(); stopDayWatch(); }
  S = freshState(el, ctx);
  await load();
}

export function unmount() {
  stopPolling();
  stopDayWatch();
  S = null;
}
