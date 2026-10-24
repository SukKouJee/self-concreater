// 리포트 화면
//   탭 1 "카테고리 리포트": "리포트 만들기"(Claude 없음) → 목록 → 선택한 리포트를 sandbox iframe으로 보기.
//   탭 2 "주간 회고": 주 고르기 → 숫자(Claude 없음) → "회고 쓰기"(Claude 1회) → 편집 → "저장" → 프로필 반영(체크한 섹션만).
//   Claude는 "회고 쓰기"/"다시 쓰기" 클릭 때만 부른다. 저장·프로필 반영은 각각 버튼 클릭 뒤에만.
import { h, fill } from "/static/dom.js";
import { renderMarkdown } from "/static/md.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.review.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const INIT_COMMAND = "/yt-coach:yt-init";
const WEEK_RE = /^\d{4}-W\d{2}$/;
const STAGES = ["관찰", "모방", "제작", "변형"];
const STAGE_SECTION = "성장 단계";
const DIFF_KO = { easy: "쉬움", right: "적당", hard: "어려움" };
const STEP_TEXT = { stats: "숫자 모으는 중", claude: "Claude가 회고 쓰는 중" };
const TABS = [["report", "카테고리 리포트"], ["review", "주간 회고"]];

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const ignorable = (e) => !e || e.error === "aborted";
const itemOf = (job) => (job && job.items && job.items[0] ? job.items[0] : null);

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved or discarded in this browser is not restored after a reload.
function markHandled(jobId) {
  if (!jobId) return;
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

// Display only: the server escapes lines starting with "#" or "|" with one leading backslash.
function unescapeLines(text) {
  return String(text || "").split("\n").map((line) => line.replace(/^\\(?=[#|])/, "")).join("\n");
}

function focusNode(node) {
  if (!node) return;
  if (!/^(INPUT|TEXTAREA|BUTTON|SELECT|A)$/.test(node.tagName)) node.setAttribute("tabindex", "-1");
  node.focus({ preventScroll: true });
}

function btn(label, cls, onClick, attrs = {}) {
  const b = h("button", { type: "button", class: `btn ${cls}`.trim(), ...attrs }, label);
  b.addEventListener("click", onClick);
  return b;
}

const errorNode = (text, extra = {}) => (text ? h("p", { class: "error", role: "alert", tabindex: "-1", ...extra }, text) : null);

function tabHash(tab, week) {
  if (tab === "report") return "#/report";
  return week ? `#/report/review/${week}` : "#/report/review";
}

// ---- page state ---------------------------------------------------------------

let S = null;

function freshState(el, ctx, params) {
  return {
    el, ctx, tab: params && params.tab === "review" ? "review" : "report", nodes: {}, timer: null, pollErrors: 0,
    R: { category: null, list: null, hasTypify: true, selected: null, loading: true, busy: false, error: null, seq: 0 },
    V: {
      week: params && WEEK_RE.test(params.week || "") ? params.week : null, current: null, weeks: [], data: null,
      loading: true, loadError: null, seq: 0, job: null, jobId: null, draft: null, busy: false, runError: null,
      saveError: null, confirmDiscard: false, checked: new Set(), applying: false, applyError: null, recorded: true,
      started: false,
    },
  };
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

// ---- shell: heading + tabs ----------------------------------------------------

function render() {
  const mine = S;
  mine.nodes.reportPanel = h("div", { role: "tabpanel", id: "rp-panel-report", "aria-labelledby": "rp-tab-report", tabindex: "0", class: "rp-panel" });
  mine.nodes.reviewPanel = h("div", { role: "tabpanel", id: "rp-panel-review", "aria-labelledby": "rp-tab-review", tabindex: "0", class: "rp-panel" });
  const tabs = TABS.map(([key, label]) => {
    const t = h("button", { type: "button", role: "tab", id: `rp-tab-${key}`, "aria-controls": `rp-panel-${key}`, class: "rp-tab", "data-tab": key }, label);
    t.addEventListener("click", () => selectTab(key, false));
    t.addEventListener("keydown", (ev) => onTabKey(ev, key));
    return t;
  });
  mine.nodes.tabs = tabs;
  fill(mine.el, h("h1", {}, "리포트"), h("div", { role: "tablist", "aria-label": "리포트 종류", class: "rp-tabs" }, ...tabs),
    mine.nodes.reportPanel, mine.nodes.reviewPanel);
  applyTab();
  renderReport();
  renderReview();
}

function applyTab() {
  const mine = S;
  mine.nodes.tabs.forEach((t) => {
    const on = t.dataset.tab === mine.tab;
    t.setAttribute("aria-selected", String(on));
    t.setAttribute("tabindex", on ? "0" : "-1");
  });
  mine.nodes.reportPanel.hidden = mine.tab !== "report";
  mine.nodes.reviewPanel.hidden = mine.tab !== "review";
}

function selectTab(key, focus) {
  if (S.tab === key) { if (focus) focusNode(S.nodes.tabs.find((t) => t.dataset.tab === key)); return; }
  S.tab = key;
  applyTab();
  try { history.replaceState(null, "", tabHash(key, S.V.week)); } catch { /* ignore */ }
  if (focus) focusNode(S.nodes.tabs.find((t) => t.dataset.tab === key));
  if (key === "review" && !S.V.started) loadReviewTab();
}

function onTabKey(ev, key) {
  const keys = TABS.map(([k]) => k);
  const i = keys.indexOf(key);
  let next = null;
  if (ev.key === "ArrowRight") next = keys[(i + 1) % keys.length];
  else if (ev.key === "ArrowLeft") next = keys[(i - 1 + keys.length) % keys.length];
  else if (ev.key === "Home") next = keys[0];
  else if (ev.key === "End") next = keys[keys.length - 1];
  if (!next) return;
  ev.preventDefault();
  selectTab(next, true);
}

// ---- tab 1: category report ---------------------------------------------------

async function loadReports(selectPath) {
  const mine = S;
  const R = mine.R;
  const category = mine.ctx.category;
  const seq = ++R.seq;
  R.category = category;
  if (!category) { R.loading = false; R.list = []; renderReport(); return; }
  R.loading = true;
  R.error = null;
  renderReport();
  try {
    const data = await mine.ctx.api("GET", `/api/report/list?category=${encodeURIComponent(category)}`);
    if (!alive(mine) || seq !== R.seq) return;
    R.list = data.reports || [];
    R.hasTypify = data.has_typify !== false;
    const keep = selectPath || (R.selected && R.list.some((r) => r.path === R.selected) ? R.selected : null);
    R.selected = keep || (R.list[0] ? R.list[0].path : null);
  } catch (e) {
    if (!alive(mine) || seq !== R.seq || ignorable(e)) return;
    R.list = [];
    R.error = e.message || "리포트 목록을 불러오지 못했습니다";
  }
  R.loading = false;
  renderReport();
}

async function buildReport() {
  const mine = S;
  const R = mine.R;
  if (R.busy || !mine.ctx.category) return;
  const category = mine.ctx.category;
  R.busy = true;
  R.error = null;
  renderReport();
  let built = null;
  try {
    built = await mine.ctx.api("POST", "/api/report/build", { category });
    if (!alive(mine)) return;
    mine.ctx.toast("리포트를 만들었습니다", "info");
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    R.error = e.message || "리포트를 만들지 못했습니다";
  }
  R.busy = false;
  if (built && category === mine.ctx.category) await loadReports(built.path);
  else renderReport("[data-rp-error]");
  if (!alive(mine)) return;
  if (!built) renderReport("[data-rp-error]");
}

function renderReport(focusSel) {
  const box = S.nodes.reportPanel;
  if (!box) return;
  const R = S.R;
  const category = S.ctx.category;
  const kids = [h("h2", {}, category ? `${category} 리포트` : "카테고리 리포트")];
  if (!category) {
    kids.push(h("p", { class: "muted" }, "카테고리를 먼저 만들어 주세요."));
    fill(box, ...kids);
    return;
  }
  if (!R.hasTypify) {
    kids.push(h("div", { class: "rp-note" },
      h("p", {}, "이 카테고리는 아직 유형화 결과가 없습니다. 유형화를 먼저 하면 리포트가 채워집니다."),
      h("a", { class: "btn", href: "#/typify" }, "유형화 화면 열기")));
  }
  const build = btn(R.busy ? "만드는 중…" : "리포트 만들기", "btn-primary", buildReport, { disabled: R.busy });
  kids.push(h("div", { class: "btn-row" }, build, h("span", { class: "muted small" }, "Claude 없이 지금 기록으로 만듭니다")));
  if (R.busy) kids.push(h("div", { class: "job-status", role: "status" }, h("progress", { class: "job-progress", "aria-label": "리포트 만드는 중" }), h("span", {}, "리포트 만드는 중…")));
  kids.push(errorNode(R.error, { "data-rp-error": "" }));
  if (R.loading && !R.list) {
    kids.push(h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  } else if (R.list && R.list.length === 0) {
    kids.push(h("p", { class: "muted" }, "만든 리포트가 아직 없습니다. \"리포트 만들기\"를 눌러 보세요."));
  } else if (R.list) {
    kids.push(reportList(R));
    const sel = R.list.find((r) => r.path === R.selected);
    if (sel) kids.push(reportViewer(category, sel));
  }
  fill(box, ...kids);
  if (focusSel) focusNode(box.querySelector(focusSel));
}

function reportList(R) {
  const items = R.list.map((r) => {
    const on = r.path === R.selected;
    const b = h("button", { type: "button", class: `btn${on ? " btn-primary" : ""}`, "aria-pressed": String(on), "aria-label": `${r.date} 리포트 보기` }, on ? `${r.date} · 보는 중` : `${r.date} · 보기`);
    b.addEventListener("click", () => { R.selected = r.path; renderReport(); });
    return h("li", {}, b);
  });
  return h("ul", { class: "rp-list", "aria-label": "만든 리포트 목록" }, ...items);
}

function reportViewer(category, r) {
  const url = typeof r.url === "string" && r.url.startsWith("/files/") ? r.url : null;
  if (!url) return h("p", { class: "error", role: "alert" }, "리포트 주소가 올바르지 않습니다");
  const frame = h("iframe", { class: "rp-frame", sandbox: "", src: url, title: `${category} ${r.date} 리포트`, loading: "lazy" });
  return h("section", { class: "rp-viewer", "aria-label": `${r.date} 리포트` },
    h("div", { class: "btn-row" }, h("a", { class: "btn", href: url, target: "_blank", rel: "noopener noreferrer" }, "새 탭에서 열기")),
    frame);
}

export function onCategory() {
  if (!S) return;
  S.R.selected = null;
  S.R.list = null;
  loadReports();
}

// ---- tab 2: weekly review - loading -------------------------------------------

async function loadReviewTab() {
  const mine = S;
  const V = mine.V;
  V.started = true;
  V.loading = true;
  V.loadError = null;
  renderReview();
  try {
    const weeks = await mine.ctx.api("GET", "/api/review/weeks");
    if (!alive(mine)) return;
    V.weeks = weeks.weeks || [];
    V.current = weeks.current;
    if (!V.week) V.week = weeks.current;
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    V.loading = false;
    V.loadError = e.message || "불러오지 못했습니다";
    renderReview();
    return;
  }
  await loadWeek();
}

async function refreshWeeks() {
  const mine = S;
  try {
    const weeks = await mine.ctx.api("GET", "/api/review/weeks");
    if (!alive(mine)) return;
    mine.V.weeks = weeks.weeks || [];
    mine.V.current = weeks.current;
  } catch { /* the list is only a convenience */ }
}

async function loadWeek() {
  const mine = S;
  const V = mine.V;
  const seq = ++V.seq;
  const week = V.week;
  V.loading = true;
  V.loadError = null;
  renderReview();
  try {
    const [data, jb] = await Promise.all([
      mine.ctx.api("GET", `/api/review?week=${encodeURIComponent(week)}`),
      mine.ctx.api("GET", "/api/jobs/current?kind=review"),
    ]);
    if (!alive(mine) || seq !== V.seq) return;
    V.data = data;
    V.loading = false;
    reenter(mine, jb.job);
    renderReview();
    if (isActive(V.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || seq !== V.seq || ignorable(e)) return;
    V.loading = false;
    if (e.error === "bad_request" && V.current && week !== V.current) {
      V.week = V.current;
      loadWeek();
      return;
    }
    V.loadError = e.message || "불러오지 못했습니다";
    renderReview();
  }
}

function previewDraft(job) {
  const item = itemOf(job);
  const rv = item && item.state === "awaiting" && item.preview ? item.preview.review : null;
  if (!rv || typeof rv !== "object" || typeof rv.summary !== "string") return null;
  return {
    summary: rv.summary,
    weak: Array.isArray(rv.repeated_weakness) ? rv.repeated_weakness.map(String) : [],
    next: Array.isArray(rv.next_week) ? rv.next_week.map(String) : [],
    proposal: rv.profile_proposal && typeof rv.profile_proposal === "object" ? { ...rv.profile_proposal } : {},
  };
}

// Restore the job for this week: an active one, then a finished preview, then a failure (shown once).
function reenter(mine, job) {
  const V = mine.V;
  const item = itemOf(job);
  V.job = null;
  if (!job || !item || item.id !== V.week || isHandled(job.id)) return;
  if (isActive(job)) { V.job = job; V.jobId = job.id; return; }
  if (job.status === "done") {
    const draft = previewDraft(job);
    if (draft) { V.job = job; V.jobId = job.id; V.draft = draft; return; }
    markHandled(job.id);
    return;
  }
  V.runError = item.message || job.message || "작업을 마치지 못했습니다";
  markHandled(job.id);
}

// ---- tab 2: job polling -------------------------------------------------------

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    const V = mine.V;
    if (!alive(mine) || !V.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(V.job.id)}`);
      if (!alive(mine) || !V.job) return;
      if (mine.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.pollErrors = 0;
      const was = isActive(V.job);
      V.job = data.job;
      if (was && !isActive(V.job)) { finishJob(mine); return; }
      updateProgress();
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 시도하세요", "error");
        V.job = null;
        renderReview();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.V.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

function finishJob(mine) {
  const V = mine.V;
  const job = V.job;
  const item = itemOf(job);
  if (job.status === "done") {
    const draft = previewDraft(job);
    if (draft) {
      V.draft = draft;
      V.jobId = job.id;
      renderReview("[data-rv-summary]");
      return;
    }
    markHandled(job.id);
    V.runError = "회고 결과를 받지 못했습니다. 다시 시도해 주세요";
  } else {
    V.runError = (item && item.message) || job.message || "작업을 마치지 못했습니다";
    markHandled(job.id);
  }
  V.job = null;
  V.jobId = null;
  renderReview("[data-rv-run-error]");
}

function updateProgress() {
  const node = S.nodes.progressText;
  if (!node || !S.V.job) return;
  const item = itemOf(S.V.job);
  const text = `${(item && STEP_TEXT[item.step]) || "준비 중"}…`;
  if (node.textContent !== text) node.textContent = text;
}

function progressBox(starting) {
  const item = itemOf(S.V.job);
  const step = starting ? "시작하는 중" : (item && STEP_TEXT[item.step]) || "준비 중";
  S.nodes.progressText = h("span", {}, `${step}…`);
  return h("div", { class: "job-status", role: "status", "data-rv-progress": "", tabindex: "-1" },
    h("progress", { class: "job-progress", "aria-label": "회고 쓰기 진행" }), S.nodes.progressText);
}

async function startWrite() {
  const mine = S;
  const V = mine.V;
  if (V.busy || isActive(V.job) || V.draft) return;
  V.busy = true;
  V.runError = null;
  V.saveError = null;
  renderReview("[data-rv-progress]");
  try {
    const res = await mine.ctx.api("POST", "/api/review/write", { week: V.week });
    if (!alive(mine)) return;
    mine.pollErrors = 0;
    V.job = res.job;
    V.jobId = res.job.id;
    V.busy = false;
    renderReview("[data-rv-progress]");
    if (isActive(V.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    V.busy = false;
    V.runError = e.message || "회고를 쓰지 못했습니다";
    renderReview("[data-rv-run-error]");
  }
}

// ---- tab 2: save / discard ----------------------------------------------------

function cleanList(list) {
  return list.map((s) => s.trim()).filter(Boolean);
}

function buildPayload(d) {
  const proposal = {};
  for (const [k, v] of Object.entries(d.proposal)) if (String(v).trim()) proposal[k] = String(v).trim();
  return { summary: d.summary.trim(), repeated_weakness: cleanList(d.weak), next_week: cleanList(d.next), profile_proposal: proposal };
}

async function saveReview() {
  const mine = S;
  const V = mine.V;
  if (V.busy || !V.draft) return;
  const review = buildPayload(V.draft);
  if (!review.summary) { V.saveError = "요약을 써 주세요"; renderReview("[data-rv-summary]"); return; }
  const jobId = V.jobId;
  const week = V.week;
  V.busy = true;
  V.saveError = null;
  V.confirmDiscard = false;
  renderReview();
  try {
    const res = await mine.ctx.api("POST", "/api/review/save", { week, review, job_id: jobId });
    markHandled(jobId);                          // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.ctx.toast("회고를 저장했습니다", "info");
    V.draft = null;
    V.job = null;
    V.jobId = null;
    V.busy = false;
    V.checked = new Set();
    V.recorded = true;
    V.applyError = null;
    if (V.data && V.week === week) V.data.saved = res.saved;
    renderReview("[data-rv-saved]");
    refreshWeeks().then(() => { if (alive(mine)) renderReview(); });
  } catch (e) {
    if (e && e.error === "aborted") markHandled(jobId);   // the server may have saved before the page was left
    if (!alive(mine) || ignorable(e)) return;
    V.busy = false;
    V.saveError = e.message || "저장하지 못했습니다";
    renderReview("[data-rv-save-error]");
  }
}

function discardDraft() {
  const V = S.V;
  if (V.busy) return;
  markHandled(V.jobId);
  V.draft = null;
  V.job = null;
  V.jobId = null;
  V.confirmDiscard = false;
  V.saveError = null;
  S.ctx.toast("버렸습니다. 저장하지 않았습니다", "info");
  renderReview("[data-rv-write]");
}

// ---- tab 2: profile apply -----------------------------------------------------

async function applyProfile() {
  const mine = S;
  const V = mine.V;
  const saved = V.data && V.data.saved;
  if (V.applying || !saved || V.checked.size === 0) return;
  const proposal = saved.fm.proposal || {};
  const sections = {};
  for (const name of V.checked) if (typeof proposal[name] === "string") sections[name] = proposal[name];
  if (!Object.keys(sections).length) return;
  const week = V.week;
  V.applying = true;
  V.applyError = null;
  renderReview();
  try {
    const res = await mine.ctx.api("POST", "/api/review/apply", { week, sections });
    if (!alive(mine)) return;
    mine.ctx.toast("profile.md를 갱신했습니다 — 다음 미션부터 반영됩니다", "info");
    V.applying = false;
    V.recorded = res.recorded !== false;
    if (V.data && V.week === week) {
      V.data.profile = res.profile || V.data.profile;
      const prev = Array.isArray(saved.fm.approved) ? saved.fm.approved : [];
      const done = Array.isArray(res.sections) ? res.sections : Object.keys(sections);
      if (V.recorded) saved.fm.approved = [...prev, ...done.filter((n) => !prev.includes(n))];
      else V.applied = [...(V.applied || []), ...done];
    }
    V.checked = new Set();
    renderReview("[data-rv-apply-status]");
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    V.applying = false;
    V.applyError = e.message || "프로필에 반영하지 못했습니다";
    renderReview("[data-rv-apply-error]");
  }
}

// ---- tab 2: render ------------------------------------------------------------

function renderReview(focusSel) {
  const box = S.nodes.reviewPanel;
  if (!box) return;
  const V = S.V;
  S.nodes.progressText = null;
  const kids = [h("h2", {}, "주간 회고")];
  if (!V.started) {
    fill(box, ...kids, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
    return;
  }
  if (V.loadError) {
    const retry = btn("다시 시도", "", () => (V.weeks.length ? loadWeek() : loadReviewTab()));
    kids.push(h("p", { class: "error", role: "alert" }, V.loadError), h("div", { class: "btn-row" }, retry));
    fill(box, ...kids);
    return;
  }
  if (V.weeks.length) kids.push(weekPicker());
  if (V.loading || !V.data) {
    kids.push(h("p", { class: "muted", role: "status" }, "불러오는 중…"));
    fill(box, ...kids);
    return;
  }
  kids.push(statsView(V.data));
  kids.push(...reviewBody(V.data));
  fill(box, ...kids);
  if (focusSel) focusNode(box.querySelector(focusSel));
}

const locked = () => S.V.busy || isActive(S.V.job) || !!S.V.draft || S.V.applying;

function weekPicker() {
  const V = S.V;
  const weeks = V.weeks.slice();
  if (V.week && !weeks.some((w) => w.week === V.week)) weeks.unshift({ week: V.week, saved: false });
  const sel = h("select", { id: "rv-week", disabled: locked(), "aria-describedby": locked() ? "rv-week-note" : false },
    ...weeks.map((w) => {
      const tag = w.week === V.current ? " (이번 주)" : "";
      const o = h("option", { value: w.week }, `${w.week}${tag}${w.saved ? " · 회고 있음" : ""}`);
      if (w.week === V.week) o.selected = true;
      return o;
    }));
  sel.addEventListener("change", () => {
    if (locked()) return;
    V.week = sel.value;
    V.runError = null;
    V.saveError = null;
    V.applyError = null;
    V.checked = new Set();
    V.applied = [];
    try { history.replaceState(null, "", tabHash("review", V.week)); } catch { /* ignore */ }
    loadWeek();
  });
  return h("div", { class: "field rv-week" }, h("label", { for: "rv-week" }, "주"), sel,
    locked() ? h("p", { class: "muted small", id: "rv-week-note" }, "회고를 쓰거나 고치는 동안에는 주를 바꿀 수 없습니다") : null);
}

function statsView(data) {
  const st = data.stats;
  const m = st.missions;
  const range = Array.isArray(data.range) ? `${data.range[0]} ~ ${data.range[1]}` : "";
  const kids = [h("h3", {}, "이번 주 숫자"), range ? h("p", { class: "muted small" }, range) : null];
  if (!m || !m.total) {
    kids.push(h("p", { class: "muted" }, "이번 주 미션 기록이 없습니다"));
  } else {
    kids.push(h("p", { class: "rv-rate" }, `${m.total}개 중 ${m.submitted}개 제출(${Math.round((m.completion || 0) * 100)}%)`));
    const rows = [];
    for (const [k, n] of Object.entries(st.kinds || {})) rows.push(h("tr", {}, h("th", { scope: "row" }, "종류"), h("td", {}, k), h("td", { class: "num" }, `${n}개`)));
    for (const [k, n] of Object.entries(st.difficulty || {})) rows.push(h("tr", {}, h("th", { scope: "row" }, "난이도"), h("td", {}, DIFF_KO[k] || k), h("td", { class: "num" }, `${n}개`)));
    if (rows.length) {
      kids.push(h("div", { class: "table-wrap" }, h("table", { class: "preview-table rv-table" },
        h("caption", { class: "sr-only" }, "미션 종류와 난이도 분포"),
        h("thead", {}, h("tr", {}, ...["구분", "값", "수"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...rows))));
    }
    if (st.skip_reasons && st.skip_reasons.length) {
      kids.push(h("h4", {}, "건너뛴 사유"), h("ul", { class: "rv-list" }, ...st.skip_reasons.map((r) => h("li", {}, r))));
    }
  }
  kids.push(h("p", {}, `새 모티브 카드 ${st.cards_created || 0}장`));
  const gaps = Array.isArray(st.diagnosis_gaps) ? st.diagnosis_gaps : [];
  if (gaps.length) {
    kids.push(h("h4", {}, `진단 갭 ${st.diagnosis_gap_count || 0}개`),
      h("ul", { class: "rv-list" }, ...gaps.map((g) => h("li", {}, `${g.name}: ${(g.gaps || []).join(", ")}`))));
  } else {
    kids.push(h("p", { class: "muted" }, "이번 주 남은 진단 갭이 없습니다"));
  }
  return h("section", { class: "card rv-stats", "aria-label": "이번 주 숫자" }, ...kids.filter(Boolean));
}

function reviewBody(data) {
  const V = S.V;
  const out = [];
  const saved = data.saved;
  if (V.draft) {
    out.push(editor());
  } else {
    out.push(h("section", { class: "card", "aria-label": "회고 쓰기" }, ...writeArea(saved)));
  }
  if (saved && !V.draft) {
    out.push(savedView(saved, data));
    out.push(profileArea(saved, data));
  }
  return out;
}

function writeArea(saved) {
  const V = S.V;
  const active = isActive(V.job) || V.busy;
  const kids = [h("h3", {}, saved ? "회고 다시 쓰기" : "회고 쓰기")];
  kids.push(h("div", { class: "btn-row" },
    btn(saved ? "다시 쓰기" : "회고 쓰기", "btn-primary", startWrite, { disabled: active, "data-rv-write": "" }),
    h("span", { class: "muted small" }, saved ? "Claude를 한 번 부릅니다. 저장하기 전에는 기존 회고가 그대로입니다." : "Claude를 한 번 부릅니다(수십 초)")));
  if (active) kids.push(progressBox(V.busy && !V.job));
  kids.push(errorNode(V.runError, { "data-rv-run-error": "" }));
  return kids;
}

function listEditor(label, key, d) {
  const V = S.V;
  const items = d[key];
  const rows = items.map((val, i) => {
    const input = h("input", { type: "text", "aria-label": `${label} ${i + 1}`, disabled: V.busy, "data-rv-list": key });
    input.value = val;
    input.addEventListener("input", () => { items[i] = input.value; });
    const del = btn("삭제", "", () => { items.splice(i, 1); renderReview(items.length ? `[data-rv-list="${key}"]` : `[data-rv-add="${key}"]`); },
      { disabled: V.busy, "aria-label": `${label} ${i + 1} 삭제` });
    return h("li", { class: "rv-row" }, input, del);
  });
  const add = btn("추가", "", () => { items.push(""); renderReview(); const all = S.nodes.reviewPanel.querySelectorAll(`[data-rv-list="${key}"]`); focusNode(all[all.length - 1]); }, { disabled: V.busy, "data-rv-add": key });
  return h("fieldset", { class: "field" }, h("legend", {}, label),
    rows.length ? h("ul", { class: "rv-edit-list" }, ...rows) : h("p", { class: "muted small" }, "항목이 없습니다"),
    h("div", { class: "btn-row" }, add));
}

function editor() {
  const V = S.V;
  const d = V.draft;
  const summary = h("textarea", { id: "rv-summary", rows: "4", required: true, disabled: V.busy, "data-rv-summary": "", "aria-describedby": "rv-summary-hint" });
  summary.value = d.summary;
  const hint = h("p", { class: "muted small", id: "rv-summary-hint", role: "status" });
  const save = btn("저장", "btn-primary", saveReview, { disabled: V.busy || !d.summary.trim(), "data-rv-save": "" });
  const syncSave = () => {
    const empty = !d.summary.trim();
    save.disabled = V.busy || empty;
    hint.textContent = empty ? "요약을 써야 저장할 수 있습니다" : "";
  };
  summary.addEventListener("input", () => { d.summary = summary.value; syncSave(); });
  hint.textContent = d.summary.trim() ? "" : "요약을 써야 저장할 수 있습니다";
  const kids = [
    h("h3", { id: "rv-edit-title" }, "회고 확인"),
    h("p", { class: "muted small" }, "저장하기 전에는 파일이 바뀌지 않습니다. 직접 쓸 내용은 저장된 회고의 '## 내 메모' 아래에 적으세요"),
    h("div", { class: "field" }, h("label", { for: "rv-summary" }, "요약"), summary, hint),
    listEditor("반복된 약점", "weak", d),
    listEditor("다음 주 방향", "next", d),
  ];
  const names = Object.keys(d.proposal);
  if (names.length) {
    kids.push(h("fieldset", { class: "field" }, h("legend", {}, "프로필 변경 제안 (비우면 제안하지 않습니다)"),
      ...names.map((name) => proposalField(name, d))));
  }
  const discard = btn("버리기", "", () => { V.confirmDiscard = true; renderReview("[data-rv-discard-yes]"); }, { disabled: V.busy });
  kids.push(h("div", { class: "btn-row" }, save, discard));
  if (V.confirmDiscard) {
    const yes = btn("버리기", "btn-danger", discardDraft, { "data-rv-discard-yes": "" });
    const no = btn("취소", "", () => { V.confirmDiscard = false; renderReview("[data-rv-summary]"); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "쓴 내용이 사라집니다. 저장하지 않고 버릴까요?"), h("div", { class: "btn-row" }, yes, no)));
  }
  kids.push(errorNode(V.saveError, { "data-rv-save-error": "" }));
  return h("section", { class: "card mi-panel preview-card", "aria-labelledby": "rv-edit-title" }, ...kids);
}

function proposalField(name, d) {
  const V = S.V;
  const id = `rv-prop-${Object.keys(d.proposal).indexOf(name)}`;
  let control;
  if (name === STAGE_SECTION) {
    control = h("select", { id, disabled: V.busy },
      h("option", { value: "" }, "(제안 안 함)"),
      ...STAGES.map((s) => h("option", { value: s }, s)));
    control.value = STAGES.includes(String(d.proposal[name]).trim()) ? String(d.proposal[name]).trim() : "";
    control.addEventListener("change", () => { d.proposal[name] = control.value; });
  } else {
    control = h("textarea", { id, rows: "3", disabled: V.busy });
    control.value = d.proposal[name];
    control.addEventListener("input", () => { d.proposal[name] = control.value; });
  }
  return h("div", { class: "field" }, h("label", { for: id }, name), control);
}

function savedView(saved, data) {
  const parts = Array.isArray(saved.body) ? saved.body : [];
  const r = Array.isArray(data.range) ? data.range : [];
  const title = `${data.week} 주간 회고 (${r[0]} ~ ${r[1]})`;
  const nodes = [];
  for (const p of parts) {
    if (!(p.text || "").trim()) continue;
    const md = renderMarkdown(p.text, { dropTitle: title });
    if (!md.childNodes.length) continue;
    nodes.push(h("div", { class: "mi-part" }, p.heading ? h("h4", {}, p.heading) : null, h("div", { class: "md" }, md)));
  }
  return h("section", { class: "card", "aria-labelledby": "rv-saved-title" },
    h("h3", { id: "rv-saved-title", "data-rv-saved": "", tabindex: "-1" }, "저장된 회고"),
    nodes.length ? h("div", {}, ...nodes) : h("p", { class: "muted" }, "회고 본문이 비어 있습니다"));
}

function profileArea(saved, data) {
  const V = S.V;
  const proposal = saved.fm && saved.fm.proposal && typeof saved.fm.proposal === "object" ? saved.fm.proposal : {};
  const names = Object.keys(proposal);
  if (!names.length) return h("section", { class: "card", "aria-label": "프로필 반영" }, h("h3", {}, "프로필 반영"), h("p", { class: "muted" }, "이 회고에는 프로필 변경 제안이 없습니다"));
  if (!data.profile || !data.profile.exists) {
    const copy = btn("명령 복사", "", async () => {
      const mine = S;
      try {
        await navigator.clipboard.writeText(INIT_COMMAND);
        if (alive(mine)) mine.ctx.toast("명령을 복사했습니다", "info");
      } catch {
        if (alive(mine)) mine.ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
      }
    });
    return h("section", { class: "card", "aria-label": "프로필 반영" }, h("h3", {}, "프로필 반영"),
      h("p", { class: "muted" }, "프로필이 없어 제안을 반영할 수 없습니다. 터미널에서 프로필을 먼저 만드세요"),
      h("div", { class: "cmd-row" }, h("code", { class: "cmd" }, INIT_COMMAND), copy));
  }
  const approved = (Array.isArray(saved.fm.approved) ? saved.fm.approved : []).concat(V.applied || []);
  const current = data.profile.sections || {};
  const rows = names.map((name, i) => {
    const id = `rv-ap-${i}`;
    const done = approved.includes(name);
    const cb = h("input", { type: "checkbox", id, checked: V.checked.has(name), disabled: V.applying });
    cb.addEventListener("change", () => {
      if (cb.checked) V.checked.add(name); else V.checked.delete(name);
      renderReview(`#${id}`);
    });
    return h("li", { class: "rv-apply-item" },
      h("div", { class: "rv-apply-head" }, h("label", { class: "check", for: id }, cb, name),
        done ? h("span", { class: "badge mi-submitted" }, "반영함") : null),
      h("div", { class: "rv-compare" },
        h("div", {}, h("p", { class: "rv-cap" }, "지금"), h("p", { class: "rv-val" }, unescapeLines(current[name]) || "(비어 있음)")),
        h("div", {}, h("p", { class: "rv-cap" }, "바꿀 값"), h("p", { class: "rv-val" }, unescapeLines(proposal[name])))));
  });
  const go = btn(V.applying ? "반영하는 중…" : "체크한 항목을 프로필에 반영", "btn-primary", applyProfile, { disabled: V.applying || V.checked.size === 0 });
  return h("section", { class: "card", "aria-labelledby": "rv-apply-title" },
    h("h3", { id: "rv-apply-title" }, "프로필에 반영"),
    h("p", { class: "muted small" }, "체크한 항목만 profile.md에 반영합니다. 눌러야 바뀝니다"),
    h("ul", { class: "rv-apply-list" }, ...rows),
    h("div", { class: "btn-row" }, go),
    h("p", { class: "muted small", role: "status", "data-rv-apply-status": "", tabindex: "-1" }, V.recorded ? "" : "회고를 저장하면 반영 기록이 남습니다"),
    errorNode(V.applyError, { "data-rv-apply-error": "" }));
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  if (S) stopPolling();
  S = freshState(el, ctx, params);
  const mine = S;
  render();
  const first = loadReports();
  if (mine.tab === "review") loadReviewTab();
  await first;
}

export function unmount() {
  stopPolling();
  S = null;
}
