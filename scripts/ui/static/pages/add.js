// 레퍼런스 화면의 "링크 추가" 칸(라우트 페이지가 아니라 refs.js가 붙이는 컴포넌트).
// 링크 입력(입력이 멈추면 줄마다 상태 확인) → 1초 폴링 진행 표시 → 영상별 확인 카드 → 마무리 요약.
// 서버 작업(kind "collect")이 이미 있으면 칸을 마운트할 때 그 작업을 이어서 보여 준다.
import { h, filesUrl } from "/static/dom.js";
import {
  STATUS_TEXT, splitLinks, removeLine, startLabel, summaryText, needsAttention, itemCategory, reviewItems, queueLine, saveAllPlan,
  staleIds,
} from "/static/add-utils.js";
import { sectionBand } from "/static/section-band.js";
import { bodySubName, formatName, hookName, narrativeName, sectionLabelName, sectionName } from "/static/labels.js";
import {
  fmtDur, fmtClock, fmtRemain, spokenDur, rowView, itemProgress, stepWeights, monotonicFraction,
} from "/static/progress-utils.js";

const POLL_MS = 1000;
const CHECK_DEBOUNCE_MS = 300;
const LABELS = ["hook", "intro", "body", "demo", "transition", "cta", "outro"];
const STEPS = [
  { key: "fetch", name: "자막" },
  { key: "cuts", name: "컷" },
  { key: "analyze", name: "분석" },
  { key: "confirm", name: "확인" },
];
const ROW_NAMES = { fetch: "자막", cuts: "컷", analyze: "분석", shots: "샷 분류" };
const CLOCK_MS = 1000;
const STATE_TEXT = {
  waiting: "대기", running: "진행 중", awaiting: "확인 필요", saved: "저장됨", skipped: "건너뜀", failed: "실패",
};
const ACTIVE = new Set(["running", "awaiting"]);
const FINISHED_TEXT = { done: "작업을 마쳤습니다", stopped: "작업이 중단되었습니다", failed: "작업이 실패했습니다" };

// ---- helpers ----------------------------------------------------------------

function fmtTime(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function itemLabel(item) {
  return item.title || item.url || item.id;
}

function isActive(job) {
  return !!job && ACTIVE.has(job.status);
}

// ---- page state (reset on every mount) ---------------------------------------

let S = null;

function freshState(areaEl, ctx, hooks, startOpen) {
  const title = h("h2", { class: "add-area-title" });
  const body = h("div", { class: "add-area-body" });
  const jobEl = h("div", { class: "job-panel" });
  const formEl = h("div", { class: "form-panel" });
  body.append(jobEl, formEl);
  areaEl.replaceChildren(title, body);
  areaEl.hidden = !startOpen;
  return {
    area: areaEl, title, el: body, jobEl, formEl, ctx, hooks, isOpen: !!startOpen, timer: null, clock: null, offset: 0, job: null,
    panel: null, prog: null, noticeSig: "", headSig: "", lastAnnounce: "",
    expanded: new Set(),   // item ids whose 자세히 is open
    failMsg: new Map(),    // item id -> why the last save failed
    savingAll: false, stopping: false,
    rows: new Map(),       // item id -> { el, sig } (review cards)
    ringMax: new Map(),    // "<job id>:<item id>" -> highest whole-video fraction shown (cards are rebuilt per step)
    edits: new Map(),      // item id -> edited analysis (deep copy of preview)
    openFeedback: new Set(), // item ids whose feedback input is open
    feedbackText: new Map(),
    busy: new Map(),       // item id -> preview signature at decision time (cleared once the item moves on)
    draft: { role: "ref", links: "", shots: false, force: false, refetch: false, noTranscript: "stt" },
    category: ctx.category,
    check: { timer: null, seq: 0, rows: [], count: 0, pending: false, error: "" },
    f: null,               // form element refs (null while the job view is shown)
  };
}

function emitChange() {
  if (S && S.hooks.onChange) S.hooks.onChange({ open: S.isOpen, active: isActive(S.job) });
}

function paintTitle() {
  const cat = S.category;
  S.title.textContent = cat ? `${cat} 카테고리에 넣습니다` : "카테고리가 없습니다";
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

function stopCheck() {
  if (S) { clearTimeout(S.check.timer); S.check.timer = null; S.check.seq += 1; }
}

function stopClock() {
  if (S && S.clock) { clearInterval(S.clock); S.clock = null; }
}

// Browser time corrected by the server clock offset (refreshed on every poll).
function nowSec() {
  return Date.now() / 1000 + S.offset;
}

function startClock() {
  if (S.clock) return;
  const mine = S;
  mine.clock = setInterval(() => {
    if (S !== mine || !mine.ctx.isCurrent() || !isActive(mine.job)) { clearInterval(mine.clock); mine.clock = null; return; }
    tickClock();
  }, CLOCK_MS);
}

// ---- API --------------------------------------------------------------------

async function fetchJob(id) {
  const data = await S.ctx.api("GET", `/api/jobs/${encodeURIComponent(id)}`);
  return data.job;
}

const POLL_MAX_MS = 15000;

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    if (S !== mine || !mine.ctx.isCurrent()) return;
    try {
      const job = await fetchJob(mine.job.id);
      if (S !== mine || !mine.ctx.isCurrent()) return;
      if (mine.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.pollErrors = 0;
      applyJob(job);
    } catch (e) {
      if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        // Server restarted and forgot the job: stop polling and fall back to whatever the server has now.
        mine.ctx.toast("작업을 찾을 수 없어 처음 화면으로 돌아갑니다", "error");
        mine.pollErrors = 0;
        clearJobPanel();
        emitChange();
        await resume();
        return;
      }
      mine.pollErrors = (mine.pollErrors || 0) + 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (S === mine && mine.ctx.isCurrent() && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors || 0));
}

async function startJob(path, body) {
  const mine = S;
  try {
    const data = await mine.ctx.api("POST", path, body);
    if (S !== mine || !mine.ctx.isCurrent()) return false;
    const appended = Array.isArray(data.appended) ? data.appended.length : 0;
    if (appended) mine.ctx.toast(`진행 중인 작업 뒤에 ${appended}개를 붙였어요`, "info");
    if (!mine.job || mine.job.id !== data.job.id) {
      // Keep state for items the new job adopted (same ids); drop only ids it no longer has.
      // The card cache is rebuilt for the new job (its handlers belong to the old one).
      mine.rows.clear();
      for (const map of [mine.edits, mine.expanded, mine.failMsg, mine.openFeedback, mine.feedbackText]) {
        for (const id of staleIds(map.keys(), data.job)) map.delete(id);
      }
    }
    mine.pollErrors = 0;
    applyJob(data.job);
    if (isActive(mine.job)) schedulePoll();
    return true;
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent()) return false;
    if (e.error === "job_running") {
      mine.ctx.toast("이미 진행 중인 작업이 있습니다. 이어서 보여 드립니다", "error");
      await resume();
    } else if (e.error !== "aborted") {
      mine.ctx.toast(e.message, "error");
    }
    return false;
  }
}

async function resume() {
  const mine = S;
  try {
    const data = await mine.ctx.api("GET", "/api/jobs/current?kind=collect");
    if (S !== mine || !mine.ctx.isCurrent()) return;
    if (needsAttention(data.job)) {
      applyJob(data.job);
      if (isActive(data.job)) schedulePoll();
      if (!mine.isOpen) openArea();
    }
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
  }
}

async function decide(item, payload) {
  const mine = S;
  const key = item.id;
  if (mine.busy.has(key) || mine.savingAll) return;
  const order = reviewItems(mine.job, mine.category).map((i) => i.id);
  mine.busy.set(key, JSON.stringify(item.preview));
  mine.failMsg.delete(key);
  renderJobPanel();
  try {
    const data = await mine.ctx.api(
      "POST",
      `/api/add/${encodeURIComponent(mine.job.id)}/review/${encodeURIComponent(item.id)}`,
      payload,
    );
    if (S !== mine || !mine.ctx.isCurrent()) return;
    if (payload.action === "reanalyze") {
      mine.openFeedback.delete(key);
      mine.feedbackText.delete(key);
    }
    mine.expanded.delete(key);
    applyJob(data.job);
    if (isActive(mine.job) && !mine.timer) schedulePoll();
    if (payload.action === "save" && mine.hooks.onSaved) mine.hooks.onSaved();
    focusAfter(order, key);
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    mine.busy.delete(key);
    mine.failMsg.set(key, e.message || "처리하지 못했어요");
    mine.ctx.toast(e.message, "error");
    // Re-read the server state; the card may be stale (already decided elsewhere).
    try {
      const job = await fetchJob(mine.job.id);
      if (S !== mine || !mine.ctx.isCurrent()) return;
      applyJob(job);
    } catch { renderJobPanel(); }
    if (S === mine && mine.ctx.isCurrent()) focusAct("save", key);
  }
}

// ---- rendering: job panel ---------------------------------------------------
// Order: one-line notices for other categories -> progress card (the video being analyzed, big) -> review queue
// (summary cards) -> finished card. The link form below stays usable while a job runs.

function applyJob(job) {
  S.job = job;
  if (typeof job.now === "number") S.offset = job.now - Date.now() / 1000;
  renderJobPanel();
  paintTitle();
  emitChange();
}

function clearJobPanel() {
  S.job = null;
  renderJobPanel();
}

// Keeps the elements that are already in place (focus and scroll survive) and only moves what changed.
function reconcile(parent, els) {
  els.forEach((el, i) => {
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
  while (parent.children.length > els.length) parent.lastElementChild.remove();
}

function ensurePanel() {
  if (S.panel) return S.panel;
  const live = h("div", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
  const notices = h("div", { class: "other-cats" });
  const progSlot = h("div", { class: "prog-slot" });
  const head = h("div", { class: "review-head", tabindex: "-1" });
  const list = h("ul", { class: "review-list", "aria-label": "확인 대기 영상" });
  const queue = h("section", { class: "review-queue", "aria-label": "확인 대기", hidden: true }, head, list);
  const foot = h("div", { class: "job-foot" });
  S.jobEl.replaceChildren(live, notices, progSlot, queue, foot);
  S.lastAnnounce = "";
  S.noticeSig = "";
  S.headSig = "";
  S.prog = { el: null, id: null, sig: "" };
  S.panel = { live, notices, progSlot, queue, head, list, foot };
  return S.panel;
}

function renderJobPanel() {
  const job = S.job;
  if (!job) {
    S.jobEl.replaceChildren();
    S.panel = null;
    S.prog = null;
    S.rows.clear();
    stopClock();
    return;
  }
  const p = ensurePanel();
  if (isActive(job)) startClock(); else stopClock();
  releaseBusy(job);
  const mine = job.items.filter((i) => itemCategory(i, job) === S.category);
  renderNotices(p, job);
  renderProgress(p, job, mine);
  renderQueue(p, job);
  renderJobFoot(p, job);
  tickClock();
  announce(p, job);
}

function announce(p, job) {
  const waiting = reviewItems(job, S.category).length;
  let text;
  if (!isActive(job)) text = `작업이 끝났습니다. ${FINISHED_TEXT[job.status] || ""}`.trim();
  else if (waiting > 0) text = `확인 대기 ${waiting}개`;
  else text = `${job.items.filter((i) => i.state === "saved").length}개 저장됨`;
  if (text !== S.lastAnnounce) { S.lastAnnounce = text; p.live.textContent = text; }
}

// "travel 카테고리에서 분석 중 · 확인 대기 2개 — 보기" for work that belongs to another category.
function renderNotices(p, job) {
  const rows = [];
  const cats = [...new Set(job.items.map((i) => itemCategory(i, job)).filter((c) => c && c !== S.category))];
  for (const cat of cats) {
    const inCat = job.items.filter((i) => itemCategory(i, job) === cat);
    const analyzing = isActive(job) && inCat.some((i) => i.state === "running" || i.state === "waiting");
    const review = reviewItems(job, cat).length;
    const parts = [analyzing ? "분석 중" : "", review > 0 ? `확인 대기 ${review}개` : ""].filter(Boolean);
    if (parts.length) rows.push({ cat, text: `${cat} 카테고리에서 ${parts.join(" · ")}` });
  }
  const sig = JSON.stringify(rows);
  if (sig === S.noticeSig) return;
  S.noticeSig = sig;
  p.notices.replaceChildren(...rows.map((r) => {
    const go = h("button", { type: "button", class: "btn btn-link" }, "보기");
    go.addEventListener("click", () => {
      S.ctx.setCategory(r.cat);
      if (S.f) S.f.ta.focus();
    });
    return h("p", { class: "other-cat-line" }, h("span", {}, `${r.text} — `), go);
  }));
}

function renderJobFoot(p, job) {
  const leftover = job.items.some((i) => i.ask === "review" && i.state === "awaiting");
  if (isActive(job) || leftover) { p.foot.replaceChildren(); return; }
  const sum = job.summary;
  const box = h("section", { class: "card done-card", "aria-label": "마무리" });
  box.append(h("h2", {}, FINISHED_TEXT[job.status] || "끝났습니다"));
  if (job.message) box.append(h("p", { class: job.status === "done" ? "muted" : "error" }, job.message));
  if (sum) {
    box.append(h("p", { class: "counts" },
      h("span", { class: "count count-ok" }, `저장 ${sum.saved}`),
      h("span", { class: "count" }, `건너뜀 ${sum.skipped}`),
      h("span", { class: "count count-bad" }, `실패 ${sum.failed}`)));
    const lines = (sum.stage_text || "").split("\n").map((l) => l.replace(/^\s*-\s*/, "").trim()).filter(Boolean);
    if (lines.length) box.append(h("ul", { class: "stage-notes" }, ...lines.map((l) => h("li", {}, l))));
  }
  box.append(h("p", { class: "muted" }, "분석이 틀리면 md를 고치고 ", h("code", {}, "reviewed: true"), "로 바꿔 두세요."));
  const again = h("button", { type: "button", class: "btn btn-primary" }, "새로 추가");
  again.addEventListener("click", () => {
    stopPolling();
    stopClock();
    S.rows.clear();
    S.edits.clear();
    clearJobPanel();
    emitChange();
    if (S.f) S.f.ta.focus();
  });
  box.append(again);
  p.foot.replaceChildren(box);
}

// ---- progress card (the video being analyzed) -----------------------------------

function timingSig(timing) {
  if (!timing || !timing.steps) return null;
  return Object.entries(timing.steps).map(([k, r]) => [k, r.started_at ?? null]);
}

function releaseBusy(job) {
  for (const item of job.items) {
    if (!S.busy.has(item.id)) continue;
    if (item.state !== "awaiting" || JSON.stringify(item.preview) !== S.busy.get(item.id)) S.busy.delete(item.id);
  }
}

function stepChips(item) {
  const ol = h("ol", { class: "chips", "aria-label": "단계" });
  const idx = STEPS.findIndex((s) => s.key === item.step);
  if (item.step === "shots") {
    ol.append(h("li", { class: "chip chip-current", "aria-current": "step" }, "샷 분류"));
    return ol;
  }
  STEPS.forEach((s, i) => {
    let cls = "chip";
    let note = "";
    if (i < idx) { cls += " chip-done"; note = " (완료)"; }
    else if (i === idx) { cls += " chip-current"; note = " (지금)"; }
    const li = h("li", { class: cls, "aria-current": i === idx ? "step" : false }, s.name);
    if (note) li.append(h("span", { class: "sr-only" }, note));
    ol.append(li);
  });
  return ol;
}

async function stopJob() {
  const mine = S;
  if (!mine.job || mine.stopping) return;
  mine.stopping = true;
  renderJobPanel();
  try {
    const data = await mine.ctx.api("POST", `/api/jobs/${encodeURIComponent(mine.job.id)}/cancel`, {});
    if (S !== mine || !mine.ctx.isCurrent()) return;
    applyJob(data.job);
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    if (e.error !== "not_running") mine.ctx.toast(e.message, "error");
  }
  if (S === mine && mine.ctx.isCurrent()) {
    mine.stopping = false;
    renderJobPanel();
  }
}

function progressCard(item, line) {
  const card = h("section", { class: "card job-item prog-card is-running", "aria-label": "지금 분석 중인 영상" });
  card.append(h("div", { class: "item-line" },
    h("span", { class: "item-title", title: item ? item.url || "" : "" }, item ? itemLabel(item) : "다음 영상을 준비하는 중"),
    h("span", { class: "state state-running" }, item ? STATE_TEXT.running : "")));
  if (item && item.timing && item.timing.steps) card.append(stepRows(card, item));
  else if (item) card.append(stepChips(item));
  if (item && item.message) card.append(h("p", { class: "muted item-msg" }, item.message));
  const foot = h("div", { class: "prog-foot" });
  if (line) foot.append(h("p", { class: "muted prog-queue" }, line));
  // aria-disabled (not disabled) keeps the button focusable while stopping.
  const stop = h("button", { type: "button", class: "btn", "data-act": "stop", "aria-disabled": String(!!S.stopping) },
    S.stopping ? "멈추는 중…" : "멈추기");
  stop.addEventListener("click", () => { if (!S.stopping) stopJob(); });
  foot.append(stop);
  card.append(foot);
  return card;
}

function renderProgress(p, job, mine) {
  // An item being saved (step "confirm") is not the video in progress.
  const current = mine.find((i) => i.state === "running" && i.step !== "confirm");
  const line = isActive(job) ? queueLine({ items: mine }) : "";
  let sig;
  if (!isActive(job) || mine.length === 0) sig = "none";
  else if (current) {
    sig = JSON.stringify([current.id, current.title, current.step, current.message, timingSig(current.timing), line, !!S.stopping]);
  } else sig = JSON.stringify(["idle", line, !!S.stopping]);
  if (sig === S.prog.sig) return;
  const hadFocus = !!p.progSlot.querySelector('[data-act="stop"]:focus');
  S.prog.sig = sig;
  S.prog.id = current ? current.id : null;
  S.prog.el = sig === "none" ? null : progressCard(current || null, line);
  if (S.prog.el) p.progSlot.replaceChildren(S.prog.el); else p.progSlot.replaceChildren();
  if (hadFocus && S.prog.el) S.prog.el.querySelector('[data-act="stop"]').focus();
}

// Updates only text and bar widths of the progress card; it is never rebuilt here.
function tickClock() {
  if (!S || !S.job || !S.prog || !S.prog.el || !S.prog.el._tick) return;
  const cur = S.job.items.find((i) => i.id === S.prog.id);
  if (cur) S.prog.el._tick(cur);
}

// ---- step rows (progress bars, remaining time) --------------------------------

// Percent shown for a measured row: the displayed (never-decreasing) bar value, so text, bar and aria agree.
function shownPct(row, shown) {
  return typeof row.fraction === "number" && shown !== null ? Math.round(Math.min(1, Math.max(0, shown)) * 100) : null;
}

function rowText(row, v, p) {
  const exp = v.expected;
  const hasExp = exp !== null && exp !== undefined;
  switch (row.state) {
    case "done":
      return `✓ ${fmtDur(v.elapsed)} 걸림${hasExp ? ` (예상 ${fmtDur(exp)})` : ""}`;
    case "failed":
      return v.elapsed !== null ? `실패 ${fmtDur(v.elapsed)}` : "실패";
    case "skipped":
      return "건너뜀";
    case "running": {
      const pct = p === null ? "" : `${p}% · `;
      return hasExp ? `${pct}${fmtDur(v.elapsed)} / 예상 ${fmtDur(exp)}` : `${pct}${fmtDur(v.elapsed)} · 예상 계산 중`;
    }
    default:
      return hasExp ? `예상 ${fmtDur(exp)}` : "예상 계산 중";
  }
}

// Short text inside a step cell; the full sentence (rowText) is the cell's tooltip.
function cellText(row, v, p) {
  switch (row.state) {
    case "done": return `✓ ${fmtDur(v.elapsed)}`;
    case "failed": return "실패";
    case "skipped": return "건너뜀";
    case "running": return p !== null ? `${p}%` : fmtDur(v.elapsed);
    default: return v.expected !== null && v.expected !== undefined ? `~${fmtDur(v.expected)}` : "–";
  }
}

function buildStepRow(key) {
  const name = h("span", { class: "step-name" }, ROW_NAMES[key] || key);
  // The short cell text is for eyes; screen readers get the full sentence (also the hover tooltip).
  const time = h("span", { class: "step-time", "aria-hidden": "true" });
  const full = h("span", { class: "sr-only" });
  const fill = h("span", { class: "bar-fill" });
  const bar = h("span", { class: "bar" }, fill);
  const li = h("li", { class: "step-row" }, h("div", { class: "step-main" }, name, time, full), bar);
  return { li, bar, fill, time, full, maxFrac: null };
}

function paintStepRow(r, key, row, nowS, frozen) {
  const v = rowView(row, nowS);
  const running = row.state === "running";
  // Row state classes are row-* so they never pick up the guide's .step-done/.step-current styles.
  // Returns { phase, timing } for the summary line ("카메라 움직임 확인 중", "이 단계 약 50초 남음"); not running: null.
  r.li.className = `step-row row-${row.state}${running && v.over ? " is-over" : ""}`;
  // The shown bar never moves backward (estimate-based value -> measured fraction, or a grown estimate).
  if (running) r.maxFrac = monotonicFraction(r.maxFrac, v.fraction);
  const shown = running ? r.maxFrac : v.fraction;
  const p = running ? shownPct(row, shown) : null;
  r.time.textContent = cellText(row, v, p);
  r.full.textContent = rowText(row, v, p);
  r.li.title = r.full.textContent;
  r.fill.style.width = `${shown === null ? 0 : Math.round(shown * 100)}%`;
  r.bar.classList.toggle("bar-indet", running && shown === null && !frozen && row.paused_at == null);
  if (!running) {
    for (const a of ["role", "aria-label", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"]) r.bar.removeAttribute(a);
    r.bar.setAttribute("aria-hidden", "true");
    return null;
  }
  r.bar.removeAttribute("aria-hidden");
  r.bar.setAttribute("role", "progressbar");
  r.bar.setAttribute("aria-label", `${ROW_NAMES[key] || key} 진행률`);
  r.bar.setAttribute("aria-valuemin", "0");
  r.bar.setAttribute("aria-valuemax", "100");
  if (p !== null) {
    r.bar.setAttribute("aria-valuenow", String(p));
    r.bar.removeAttribute("aria-valuetext");
  } else {
    r.bar.removeAttribute("aria-valuenow");
    r.bar.setAttribute("aria-valuetext", v.expected !== null
      ? `${spokenDur(v.elapsed)} 경과, 예상 ${spokenDur(v.expected)}`
      : `${spokenDur(v.elapsed)} 경과`);
  }
  // What the step is doing, and its own time note ("" when frozen or unknown).
  let note = "";
  if (!frozen) {
    // Over the estimate: a measured fraction still gives a real remaining time; only unmeasured rows say "soon".
    if (v.over && !v.byFraction) note = "예상보다 길어지는 중 · 곧 끝날 예정";
    else if (v.over && v.remaining !== null) note = `예상보다 길어지는 중 · 이 단계 ${fmtRemain(v.remaining, false)} 남음`;
    else if (v.remaining !== null) note = `이 단계 ${fmtRemain(v.remaining, false)} 남음`;
  }
  return { phase: row.phase_text || "", timing: note };
}

// Builds the progress block once per structural change; card._tick(item) patches texts and widths in place.
// Top: the whole video (ring %, remaining time and finish clock, what is happening now). Below: one cell per step,
// widths proportional to each step's time (stepWeights), then elapsed / expected total.
function stepRows(card, item) {
  const keys = Object.keys(item.timing.steps);
  const ringText = h("span", {});
  const ring = h("div", { class: "prog-ring", role: "progressbar", "aria-label": "이 영상 전체 진행률",
    "aria-valuemin": "0", "aria-valuemax": "100" }, ringText);
  const remain = h("p", { class: "prog-remain" });
  const now = h("p", { class: "prog-now" });
  const hero = h("div", { class: "prog-hero" }, ring, h("div", { class: "prog-text" }, remain, now));
  const ol = h("ol", { class: "steps prog-steps", "aria-label": "단계" });
  const rows = new Map(keys.map((k) => [k, buildStepRow(k)]));
  rows.forEach((r) => ol.append(r.li));
  const detail = h("p", { class: "prog-detail" });
  let cols = "";
  card._tick = (cur) => {
    const timing = cur.timing;
    if (!timing || !timing.steps) return;
    const waiting = cur.state === "awaiting";
    // While the user decides, the clock stays at the last server snapshot.
    const nowS = waiting && S.job && typeof S.job.now === "number" ? S.job.now : nowSec();
    let current = null;
    for (const [k, r] of rows) {
      const row = timing.steps[k];
      if (!row) continue;
      const text = paintStepRow(r, k, row, nowS, waiting);
      if (row.state === "running" && text) current = { name: ROW_NAMES[k] || k, ...text };
    }
    // Widths in 2% steps and written only when they change, so cell edges don't creep every second.
    const w = stepWeights(timing, nowS);
    const next = keys.map((k) => `${(Math.round((w[k] || 0) * 50) / 50).toFixed(2)}fr`).join(" ");
    if (next !== cols) { cols = next; ol.style.gridTemplateColumns = next; }

    const prog = itemProgress(timing, nowS);
    const live = cur.state === "running" || waiting;
    hero.hidden = !live;
    if (live) {
      // The ring never moves backward, also across card rebuilds (a new step or an awaiting <-> running switch
      // rebuilds the card); a grown estimate would otherwise pull it back. It stays at 99% until the video ends.
      const ringKey = `${S.job ? S.job.id : ""}:${cur.id}`;
      let ringMax = S.ringMax.has(ringKey) ? S.ringMax.get(ringKey) : null;
      if (prog && prog.fraction !== null) ringMax = monotonicFraction(ringMax, prog.fraction);
      S.ringMax.set(ringKey, ringMax);
      const pct = ringMax === null ? null : Math.min(99, Math.round(ringMax * 100));
      ring.style.setProperty("--p", String(pct === null ? 0 : pct));
      ringText.textContent = pct === null ? "…" : `${pct}%`;
      if (pct === null) { ring.removeAttribute("aria-valuenow"); ring.setAttribute("aria-valuetext", "계산 중"); }
      else { ring.setAttribute("aria-valuenow", String(pct)); ring.removeAttribute("aria-valuetext"); }
      let saysOverdue = false;
      if (waiting) remain.textContent = "확인을 기다리는 중";
      else if (prog && prog.overdue && prog.remaining !== null && prog.remaining < 60) {
        saysOverdue = true;
        // A step without a measured fraction is past its estimate: its remaining time counts as 0, so a clock
        // would just follow "now". Say what is known instead.
        remain.textContent = "예상보다 길어지는 중 · 곧 끝날 예정";
      } else if (prog && prog.remaining !== null) {
        remain.textContent = `${fmtRemain(prog.remaining, true)} 남음 · ${fmtClock(nowS + prog.remaining)}쯤 끝`;
      } else remain.textContent = "남은 시간 계산 중";
      // The step's own time note is dropped when the big line already says the same thing.
      const detailParts = current ? [current.phase, saysOverdue ? "" : current.timing].filter(Boolean) : [];
      now.textContent = current ? `지금: ${current.name}${detailParts.length ? ` — ${detailParts.join(" · ")}` : ""}` : "";
      now.hidden = !current;
    }
    const parts = [];
    if (prog) parts.push(`걸린 시간 ${fmtDur(prog.elapsed)}`);
    if (live && prog && prog.total !== null) parts.push(`예상 전체 ${fmtDur(prog.total)}`);
    detail.textContent = parts.join(" · ");
    detail.hidden = parts.length === 0;
  };
  card._tick(item);
  return h("div", { class: "step-block" }, hero, ol, detail);
}

// ---- confirm card -----------------------------------------------------------

function workingAnalysis(item) {
  const src = JSON.stringify(item.preview);
  let rec = S.edits.get(item.id);
  if (!rec || rec.src !== src) {
    rec = { src, a: structuredClone(item.preview) };
    S.edits.set(item.id, rec);
  }
  return rec.a;
}

function isEdited(item) {
  const rec = S.edits.get(item.id);
  return !!rec && rec.src === JSON.stringify(item.preview) && JSON.stringify(rec.a) !== rec.src;
}

function timelineBar(sections) {
  const total = Math.max(...sections.map((s) => Number(s.end) || 0), 1);
  const bar = h("div", { class: "tl-bar", role: "img",
    "aria-label": `섹션 ${sections.length}개: ${sections.map((s) => sectionLabelName(s.label)).join(", ")}` });
  for (const s of sections) {
    const w = Math.max(((Number(s.end) - Number(s.start)) / total) * 100, 1.5);
    bar.append(h("span", { class: `tl-seg lb-${s.label}`, style: `flex-grow:${w.toFixed(2)}`, title: `${sectionName(s.label, s.sub)} ${fmtTime(s.start)}` }));
  }
  return bar;
}

function sectionRow(item, a, idx) {
  const s = a.sections[idx];
  const id = `sec-${S.job.id}-${S.job.items.indexOf(item)}-${idx}`;
  const sel = h("select", { id, class: "label-select", "aria-label": `${fmtTime(s.start)} 섹션 라벨` });
  const labels = LABELS.includes(s.label) ? LABELS : [s.label, ...LABELS];
  for (const l of labels) {
    const opt = h("option", { value: l }, sectionLabelName(l));
    if (l === s.label) opt.selected = true;
    sel.append(opt);
  }
  const bullet = h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" });
  const sub = h("span", { class: "sec-sub muted" }, bodySubName(s.sub));
  sel.addEventListener("change", () => {
    s.label = sel.value;
    if (s.label !== "body") delete s.sub;
    bullet.className = `lb-dot lb-${s.label}`;
    sub.textContent = bodySubName(s.sub);
    li.classList.toggle("edited", s.label !== item.preview.sections[idx].label);
    const rc = li.closest(".review-card");
    if (rc && rc._repaintBand) rc._repaintBand();
    const warn = li.closest(".confirm");
    if (warn) {
      const note = warn.querySelector(".edit-note");
      if (note) note.hidden = !isEdited(item);
    }
  });
  const li = h("li", { class: "sec-row" + (s.label !== item.preview.sections[idx].label ? " edited" : "") });
  li.append(
    h("span", { class: "sec-time" }, `${fmtTime(s.start)}–${fmtTime(s.end)}`),
    h("span", { class: "sec-pick" }, bullet, sel, sub),
    h("span", { class: "sec-content" }, s.content || ""),
  );
  return li;
}

function frameStrip(item) {
  const frames = item.frames || [];
  if (!frames.length) return null;
  const wrap = h("div", { class: "frames", role: "list", "aria-label": `프레임 ${frames.length}장`, tabindex: "0" });
  frames.forEach((f, i) => {
    wrap.append(h("img", { src: filesUrl(f), alt: `프레임 ${i + 1}/${frames.length}`, loading: "lazy", role: "listitem" }));
  });
  return wrap;
}

// ---- review queue (summary cards first, details folded) ----------------------------

function focusAct(act, id) {
  const el = S.jobEl.querySelector(`[data-act="${act}"][data-id="${CSS.escape(id)}"]`);
  if (el && !el.disabled) { el.focus(); return true; }
  return false;
}

// After a card leaves the queue: the next card's 저장, else the queue head (else the link box).
function focusAfter(order, key) {
  const left = new Set(reviewItems(S.job, S.category).map((i) => i.id));
  const at = order.indexOf(key);
  const next = order.slice(at + 1).find((id) => left.has(id));
  if (next && focusAct("save", next)) return;
  if (!S.panel.queue.hidden) S.panel.head.focus();
  else if (S.f) S.f.ta.focus();
}

function renderQueue(p, job) {
  const items = reviewItems(job, S.category);
  p.queue.hidden = items.length === 0;
  const stopped = !isActive(job);
  const headSig = JSON.stringify([items.length, S.savingAll, stopped]);
  if (headSig !== S.headSig) {
    S.headSig = headSig;
    const all = h("button", { type: "button", class: "btn btn-primary", disabled: S.savingAll || items.length === 0 },
      S.savingAll ? "저장하는 중…" : "모두 저장");
    all.addEventListener("click", saveAll);
    p.head.replaceChildren(
      h("div", { class: "review-head-line" }, h("h2", {}, `확인 대기 ${items.length}개`), all),
      h("p", { class: "muted review-notice" }, "저장하지 않은 결과는 UI 서버를 끄면 사라져요"),
      ...(stopped ? [h("p", { class: "muted review-notice" }, "작업이 멈췄어요. 확인 대기 영상은 저장하거나 건너뛸 수 있어요")] : []));
  }
  const seen = new Set();
  const els = items.map((item) => {
    seen.add(item.id);
    const sig = reviewSig(item);
    const row = S.rows.get(item.id);
    if (row && row.sig === sig) return row.el;
    const el = reviewCard(item);
    S.rows.set(item.id, { el, sig });
    return el;
  });
  for (const key of [...S.rows.keys()]) if (!seen.has(key)) S.rows.delete(key);
  reconcile(p.list, els);
}

function reviewSig(item) {
  return JSON.stringify([item.id, item.title, item.message, item.preview, S.expanded.has(item.id),
    S.openFeedback.has(item.id), S.busy.has(item.id), S.savingAll, S.failMsg.get(item.id) || "", isActive(S.job)]);
}

async function saveAll() {
  const mine = S;
  if (mine.savingAll || !mine.job) return;
  mine.savingAll = true;
  mine.failMsg.clear();
  renderJobPanel();
  try {
    // One request per card through the review route, so edited labels travel with their card and a failure
    // leaves that card (and its edits) in place.
    const items = reviewItems(mine.job, mine.category);
    const edited = {};
    for (const item of items) if (isEdited(item)) edited[item.id] = mine.edits.get(item.id).a;
    const results = [];
    let lastJob = null;
    for (const step of saveAllPlan(items, edited)) {
      try {
        const data = await mine.ctx.api("POST",
          `/api/add/${encodeURIComponent(mine.job.id)}/review/${encodeURIComponent(step.id)}`, step.body);
        if (S !== mine || !mine.ctx.isCurrent()) return;
        lastJob = data.job;
        results.push({ id: step.id, ok: true });
      } catch (e) {
        if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
        results.push({ id: step.id, ok: false, message: e.message });
      }
    }
    const failed = results.filter((r) => !r.ok);
    const saved = results.length - failed.length;
    for (const r of failed) mine.failMsg.set(r.id, r.message || "저장하지 못했어요");
    mine.savingAll = false;
    const fresh = lastJob || await fetchJob(mine.job.id);
    if (S !== mine || !mine.ctx.isCurrent()) return;
    applyJob(fresh);
    if (isActive(mine.job) && !mine.timer) schedulePoll();
    if (saved > 0 && mine.hooks.onSaved) mine.hooks.onSaved();
    if (failed.length) mine.ctx.toast(`${saved}개 저장, ${failed.length}개는 저장하지 못했어요. 카드에 이유를 적어 두었어요`, "error");
    else if (saved > 0) mine.ctx.toast(`${saved}개를 저장했어요`, "info");
    if (!(failed.length && focusAct("save", failed[0].id))) focusAfter([], "");
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
  } finally {
    if (S === mine && mine.savingAll) { mine.savingAll = false; if (mine.ctx.isCurrent()) renderJobPanel(); }
  }
}

function lengthOf(a) {
  const ends = (a.sections || []).map((s) => Number(s.end) || 0);
  return ends.length ? Math.max(...ends) : 0;
}

function reviewCard(item) {
  const a = workingAnalysis(item);
  const busy = S.busy.has(item.id) || S.savingAll;
  const expanded = S.expanded.has(item.id);
  const open = S.openFeedback.has(item.id);
  const canRedo = isActive(S.job);
  const li = h("li", { class: "review-card", "data-id": item.id });
  const detailId = `rv-detail-${S.job.id}-${item.id}`;

  const meta = [lengthOf(a) > 0 ? fmtTime(lengthOf(a)) : "", item.channel || a.channel || ""].filter(Boolean).join(" · ");
  li.append(h("div", { class: "review-title-line" },
    h("h3", { class: "review-title", title: item.url || "" }, itemLabel(item)),
    meta ? h("span", { class: "muted review-meta" }, meta) : null));

  const bandSlot = h("div", { class: "review-band" });
  const paintBand = () => {
    const band = sectionBand(a.sections || [], lengthOf(a));
    if (band) bandSlot.replaceChildren(band); else bandSlot.replaceChildren();
  };
  paintBand();
  li._repaintBand = paintBand;
  li.append(bandSlot);

  const tags = h("p", { class: "meta-line" });
  if (a.hook_type) tags.append(h("span", { class: "tag" }, `훅 ${hookName(a.hook_type)}`));
  if (a.narrative_type) tags.append(h("span", { class: "tag" }, `서사 ${narrativeName(a.narrative_type)}`));
  const fmt = item.format || a.format;
  if (fmt) tags.append(h("span", { class: "tag" }, `형식 ${formatName(fmt)}`));
  if (tags.childNodes.length) li.append(tags);
  if (a.summary) li.append(h("p", { class: "review-summary" }, a.summary));
  if (item.message) li.append(h("p", { class: "review-warn" }, item.message));
  const fail = S.failMsg.get(item.id);
  if (fail) li.append(h("p", { class: "error review-fail", role: "alert" }, `저장하지 못했어요: ${fail}`));

  const savePayload = (action) => (isEdited(item) ? { action, analysis: S.edits.get(item.id).a } : { action });
  const save = h("button", { type: "button", class: "btn btn-primary", "data-act": "save", "data-id": item.id, disabled: busy }, "저장");
  const redoNote = h("span", { class: "muted review-why", id: `${detailId}-why` }, "작업이 멈춰 다시 분석할 수 없어요");
  const redo = h("button", { type: "button", class: "btn", "data-act": "redo", "data-id": item.id,
    "aria-expanded": String(open && canRedo), disabled: busy || !canRedo, "aria-describedby": canRedo ? false : redoNote.id },
  "고칠 점 적고 다시 분석");
  const skip = h("button", { type: "button", class: "btn", "data-act": "skip", "data-id": item.id, disabled: busy }, "건너뛰기");
  const more = h("button", { type: "button", class: "btn", "data-act": "more", "data-id": item.id,
    "aria-expanded": String(expanded), "aria-controls": detailId }, "자세히");
  save.addEventListener("click", () => decide(item, savePayload("save")));
  skip.addEventListener("click", () => decide(item, { action: "skip" }));
  redo.addEventListener("click", () => {
    if (S.openFeedback.has(item.id)) S.openFeedback.delete(item.id); else S.openFeedback.add(item.id);
    renderJobPanel();
    const ta = S.jobEl.querySelector(`[data-fb="${CSS.escape(item.id)}"]`);
    if (ta) ta.focus(); else focusAct("redo", item.id);
  });
  more.addEventListener("click", () => {
    if (S.expanded.has(item.id)) S.expanded.delete(item.id); else S.expanded.add(item.id);
    renderJobPanel();
    focusAct("more", item.id);
  });
  li.append(h("div", { class: "btn-row" }, save, redo, skip, more));
  if (!canRedo) li.append(redoNote);

  if (open && canRedo) {
    const fbId = `fb-${S.job.id}-${item.id}`;
    const ta = h("textarea", { id: fbId, rows: "3", "data-fb": item.id,
      placeholder: "예: 2:10 이후는 데모가 아니라 비교입니다" });
    ta.value = S.feedbackText.get(item.id) || "";
    ta.addEventListener("input", () => S.feedbackText.set(item.id, ta.value));
    const go = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "다시 분석 요청");
    go.addEventListener("click", () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); S.ctx.toast("고칠 점을 입력해 주세요", "error"); return; }
      decide(item, { action: "reanalyze", feedback: text });
    });
    li.append(h("div", { class: "feedback" },
      h("label", { for: fbId }, "고칠 점"), ta,
      h("p", { class: "muted" }, "다시 분석하면 Claude 호출이 한 번 더 일어납니다."), go));
  }

  const panel = h("div", { class: "confirm review-detail", id: detailId, hidden: !expanded });
  if (expanded) fillDetail(panel, item, a);
  li.append(panel);
  return li;
}

// The fold-out part: synopsis, section table with label editing, techniques, frames.
function fillDetail(card, item, a) {
  if (a.synopsis) card.append(h("p", { class: "muted synopsis" }, a.synopsis));
  const secs = a.sections || [];
  if (secs.length) {
    card.append(h("h4", {}, "섹션"), timelineBar(secs));
    const ul = h("ul", { class: "sections" });
    secs.forEach((_, i) => ul.append(sectionRow(item, a, i)));
    card.append(ul);
  }
  const techs = a.techniques || [];
  if (techs.length) {
    card.append(h("h4", {}, "기법"));
    card.append(h("ul", { class: "techs" }, ...techs.map((t) => h("li", {},
      h("span", { class: "tag" }, t.kind), " ", h("strong", {}, t.name),
      t.at !== undefined && t.at !== null ? h("span", { class: "muted" }, ` ${typeof t.at === "number" ? fmtTime(t.at) : t.at}`) : null,
      t.desc ? h("span", { class: "muted" }, ` — ${t.desc}`) : null))));
  }
  const strip = frameStrip(item);
  if (strip) card.append(h("h4", {}, "프레임"), strip);
  card.append(h("p", { class: "edit-note muted", hidden: !isEdited(item), role: "status" }, "고친 라벨이 저장에 반영됩니다."));
}

// ---- link check (debounced, newest answer wins) ------------------------------

function checkRowsToSend() {
  return S.check.rows.filter((r) => r.status === "new" || r.status === "reanalyze").map((r) => r.link);
}

function scheduleCheck() {
  const mine = S;
  const c = mine.check;
  clearTimeout(c.timer);
  c.seq += 1;                       // an answer still on its way is now stale
  const links = splitLinks(mine.draft.links);
  if (!mine.category || links.length === 0) {
    c.rows = []; c.count = 0; c.pending = false; c.error = "";
    paintCheck();
    return;
  }
  c.pending = true;
  paintCheck();
  c.timer = setTimeout(() => runCheck(mine, links), CHECK_DEBOUNCE_MS);
}

async function runCheck(mine, links) {
  const c = mine.check;
  const seq = ++c.seq;
  try {
    const data = await mine.ctx.api("POST", "/api/add/check", {
      category: mine.category, role: mine.draft.role, links, force: mine.draft.force,
    });
    if (S !== mine || !mine.ctx.isCurrent() || seq !== c.seq) return;
    c.rows = Array.isArray(data.rows) ? data.rows : [];
    c.count = Number(data.count) || 0;
    c.error = "";
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || seq !== c.seq || e.error === "aborted") return;
    c.rows = []; c.count = 0;
    c.error = e.message || "링크를 확인하지 못했습니다";
  }
  c.pending = false;
  paintCheck();
}

function linkRow(row) {
  const name = row.title || row.id || row.link;
  const x = h("button", { type: "button", class: "btn link-x", "aria-label": `${name} 줄 지우기` }, "×");
  x.addEventListener("click", () => {
    S.f.ta.value = removeLine(S.f.ta.value, row.link);
    S.draft.links = S.f.ta.value;
    scheduleCheck();
    S.f.ta.focus();
  });
  return h("li", { class: `link-row link-${row.status}` },
    h("span", { class: `badge link-badge link-badge-${row.status}` }, STATUS_TEXT[row.status] || row.status),
    h("span", { class: "link-name", title: row.link }, name),
    x);
}

function paintCheck() {
  const f = S && S.f;
  if (!f) return;
  const c = S.check;
  const text = c.error || summaryText(c.rows);
  if (f.summary.textContent !== text) f.summary.textContent = text;
  f.summary.classList.toggle("error", !!c.error);
  f.list.replaceChildren(...c.rows.map(linkRow));
  f.list.hidden = c.rows.length === 0;
  f.list.classList.toggle("is-pending", c.pending);
  f.list.setAttribute("aria-busy", String(c.pending));
  f.start.textContent = startLabel(c.count);
  f.start.disabled = c.pending || c.count === 0 || !!f.sending;
}

// ---- rendering: form --------------------------------------------------------

function checkField(key, text, hint) {
  const cid = `add-opt-${key}`;
  const hid = `${cid}-d`;
  const cb = h("input", { type: "checkbox", id: cid, "aria-describedby": hint ? hid : false });
  cb.checked = !!S.draft[key];
  cb.addEventListener("change", () => {
    S.draft[key] = cb.checked;
    if (key === "force") scheduleCheck();
  });
  return h("div", { class: "check-wrap" },
    h("label", { class: "check", for: cid }, cb, h("span", {}, text)),
    hint ? h("p", { class: "muted hint check-hint", id: hid }, hint) : null);
}

function renderForm() {
  stopCheck();
  S.formEl.replaceChildren();
  S.f = null;
  paintTitle();
  if (!S.category) {
    S.formEl.append(h("p", { class: "muted" }, "먼저 ", h("code", {}, "/yt-coach:yt-init"), " 또는 홈에서 폴더 만들기를 진행하세요."));
    return;
  }

  const form = h("form", { class: "add-form", novalidate: "" });

  const fs = h("fieldset", { class: "field seg" }, h("legend", {}, "구분"));
  for (const [val, text] of [["ref", "레퍼런스"], ["mine", "내 영상"]]) {
    const rid = `add-role-${val}`;
    const input = h("input", { type: "radio", name: "role", id: rid, value: val });
    input.checked = S.draft.role === val;
    input.addEventListener("change", () => { S.draft.role = val; scheduleCheck(); });
    fs.append(h("label", { class: "seg-opt", for: rid }, input, h("span", {}, text)));
  }
  form.append(fs);

  const ta = h("textarea", { id: "add-links", rows: "5", placeholder: "유튜브 링크를 한 줄에 하나씩 붙여 넣으세요",
    spellcheck: "false", "aria-describedby": "add-check-summary" });
  ta.value = S.draft.links;
  ta.addEventListener("input", () => { S.draft.links = ta.value; scheduleCheck(); });
  const summary = h("p", { class: "check-summary", id: "add-check-summary", "aria-live": "polite" });
  const list = h("ul", { class: "link-list", "aria-label": "링크 확인 결과", hidden: true });
  form.append(h("div", { class: "field" }, h("label", { for: "add-links" }, "링크"), ta, summary, list));

  form.append(checkField("shots", "샷 분류도 함께", "영상마다 Claude 호출이 한 번 더 일어나요"));

  const adv = h("details", { class: "adv" }, h("summary", {}, "고급"));
  adv.open = !!S.draft.advOpen;
  adv.addEventListener("toggle", () => { S.draft.advOpen = adv.open; });
  adv.append(
    checkField("force", "다시 분석", "이미 분석한 영상도 처음부터 다시 분석합니다"),
    checkField("refetch", "자막·컷 새로 받기", "요청이 늘어 차단될 수 있어요"));
  const nt = h("fieldset", { class: "field radios" }, h("legend", {}, "자막이 없을 때"));
  for (const [val, text] of [["stt", "음성 인식(기본)"], ["screen", "화면만 보고 분석"], ["skip", "건너뛰기"]]) {
    const rid = `add-nt-${val}`;
    const input = h("input", { type: "radio", name: "no_transcript", id: rid, value: val });
    input.checked = S.draft.noTranscript === val;
    input.addEventListener("change", () => { S.draft.noTranscript = val; });
    nt.append(h("label", { class: "check", for: rid }, input, h("span", {}, text)));
  }
  adv.append(nt);
  form.append(adv);

  const start = h("button", { type: "submit", class: "btn btn-primary" }, startLabel(0));
  start.disabled = true;
  form.append(h("div", { class: "btn-row" }, start));
  form.append(h("p", { class: "muted hint" }, "영상은 하나씩 처리합니다. 분석마다 Claude 호출이 일어납니다."));

  S.f = { form, ta, summary, list, start, sending: false };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mine = S;
    const links = checkRowsToSend();
    if (links.length === 0 || mine.check.pending || mine.f.sending) return;
    mine.f.sending = true;
    paintCheck();
    const f = mine.f;
    const ok = await startJob("/api/add", {
      category: mine.category, role: mine.draft.role, links,
      shots: mine.draft.shots, force: mine.draft.force, refetch: mine.draft.refetch,
      no_transcript: mine.draft.noTranscript,
    });
    if (S !== mine || !mine.ctx.isCurrent()) return;
    f.sending = false;
    if (ok && S.f === f) {
      // The form stays for more links; what was sent is cleared.
      mine.draft.links = "";
      f.ta.value = "";
      mine.check.rows = []; mine.check.count = 0; mine.check.pending = false;
    }
    if (S.f === f) paintCheck();
  });
  S.formEl.append(form);
  if (S.draft.links) scheduleCheck();
}

// ---- component contract -----------------------------------------------------

function setOpen(on, focus) {
  if (!S || S.isOpen === on) return;
  S.isOpen = on;
  S.area.hidden = !on;
  emitChange();
  if (on && focus && S.f) S.f.ta.focus();
}

function openArea() { setOpen(true, false); }

// opts: { open, onSaved(), onChange({ open, active }) }. Returns a handle that refs.js drives.
export function mountAddArea(el, ctx, opts = {}) {
  stopPolling();
  stopClock();
  stopCheck();
  S = freshState(el, ctx, { onSaved: opts.onSaved, onChange: opts.onChange }, !!opts.open);
  paintTitle();
  renderForm();
  resume();      // an unfinished collect job opens the area and is shown here
  return {
    open: (o) => setOpen(true, !(o && o.focus === false)),
    close: () => setOpen(false, false),
    unmount,
    onCategory,
    startShotsOnly,
  };
}

async function startShotsOnly() {
  if (!S || !S.category) return false;
  openArea();
  return startJob("/api/add/shots-only", { category: S.category });
}

export function unmount() {
  stopPolling();
  stopClock();
  stopCheck();
  S = null;
}

export function onCategory(category) {
  if (!S) return;
  S.category = category;
  paintTitle();
  if (!S.f || !category) renderForm();   // category list was empty before, or just became empty
  else scheduleCheck();
  renderJobPanel();
}
