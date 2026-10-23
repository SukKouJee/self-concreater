// 콘티 화면
//   #/conti            기획안 목록 + 콘티 만들기(작업 폴링 → 미리보기 → 저장)
//   #/conti/<draft>    기획안 하나(콘티가 있으면 그 콘티로 바로 이동)
//   #/conti/<conti>    편집: 왼쪽 컷 목록, 오른쪽 패널(레퍼런스 실제 장면 · 대사 대안 · 직접 고치기)
// 모든 컷 저장은 마지막으로 받은 hash를 보낸다. 파일이 밖에서 바뀌면 서버가 conti_changed로 거절한다.
import { h, filesUrl } from "/static/dom.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const ALT_COUNT = 3;
const AUTO_ALT_DELAY_MS = 600;   // auto alternatives fire only if the cut is still selected after this delay
const NARROW = "(max-width: 900px)";
const HANDLED_KEY = "ytc.conti.handled";
const RECREATE_WARNING = "다시 만들면 화면에서 바꾼 대사·가이드·고정한 참고 프레임이 사라지고 내 메모만 남습니다";
const SAVED_TOAST = "저장했습니다 · 스토리보드도 다시 그렸습니다";
const CHANGED_TEXT = "파일이 밖에서 바뀌었습니다 — 다시 불러오기";
const MATCH_TEXT = { exact: "일치", approx: "근사", pinned: "고정" };
const FIELDS = [
  { key: "line", label: "대사", multiline: true },
  { key: "guide", label: "촬영 가이드", multiline: true },
  { key: "caption", label: "자막", multiline: false },
  { key: "sfx", label: "효과음", multiline: false },
];
const ACTIVE = new Set(["running", "awaiting"]);

// ---- helpers ----------------------------------------------------------------

const isContiPath = (p) => /-conti\.md$/i.test(p);
const contiHref = (p) => `#/conti/${encodeURIComponent(p)}`;
const isActive = (job) => !!job && ACTIVE.has(job.status);
// Same rule as conti._norm on the server: count letters and digits only (spaces and punctuation are free).
const speechChars = (t) => (t || "").replace(/[^\p{L}\p{N}\p{M}]+/gu, "").length;
const fmtSec = (s) => `${Number(s || 0).toFixed(1).replace(/\.0$/, "")}초`;

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved (or dismissed) in this browser is not shown again after a reload.
function markHandled(jobId) {
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

function isHandled(jobId) {
  return readHandled().includes(jobId);
}

// Sketch SVG comes from the server as markup. Parse it in an inert document (the HTML parser puts <svg> into the
// SVG namespace; nothing in a DOMParser document runs), keep only plain shape elements, drop every on* handler,
// href, style and url()/javascript: value, then import just the <svg>. script, foreignObject, use, image, a and
// style elements are not on the allow list, so they are removed with their children.
const SVG_TAGS = new Set(["svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"]);

function sanitizeSvg(markup) {
  if (typeof markup !== "string" || !markup) return null;
  const doc = new DOMParser().parseFromString(markup, "text/html");
  const root = doc.body.querySelector(":scope > svg");
  if (!root) return null;
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (!SVG_TAGS.has(el.localName) || el.localName === "script" || el.localName === "foreignObject") {
      el.remove();
      continue;
    }
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      const value = attr.value.toLowerCase().replace(/\s+/g, "");
      if (name.startsWith("on") || name === "style" || name.endsWith("href")
        || value.includes("javascript:") || value.includes("url(")) {
        el.removeAttribute(attr.name);
      }
    }
  }
  const svg = document.importNode(root, true);
  svg.removeAttribute("role");
  svg.removeAttribute("aria-label");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  return svg;
}

// ---- page state ---------------------------------------------------------------

let S = null;

function freshState(el, params, ctx) {
  return {
    el, params, ctx, timer: null, pollErrors: 0,
    drafts: [], job: null, creating: false, saving: false, forceSave: false, recreateOpen: null,
    data: null, hash: null, sel: null, conflict: false,
    scenes: new Map(), alts: new Map(), forms: new Map(),
    nodes: {}, live: null, lastAnnounce: "",
    altTimer: null, altSkipped: null,
  };
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

function clearAutoAlts() {
  if (S && S.altTimer) { clearTimeout(S.altTimer); S.altTimer = null; }
}

function announce(text) {
  if (!S || !S.live || text === S.lastAnnounce) return;
  S.lastAnnounce = text;
  S.live.textContent = text;
}

function liveRegion() {
  S.live = h("div", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
  S.lastAnnounce = "";
  return S.live;
}

function loading(title) {
  S.el.replaceChildren(h("h1", {}, title), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
}

function loadError(e, retryFn) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) retryFn(); });
  S.el.replaceChildren(
    h("h1", {}, "콘티"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry, h("a", { class: "btn", href: "#/conti" }, "콘티 목록")),
  );
}

// ---- list / draft view ------------------------------------------------------------

// Latest conti job worth showing: still running, a preview not yet saved/dismissed, or a failure not yet dismissed.
function pendingJob(job) {
  if (!job || job.kind !== "conti" || isHandled(job.id)) return null;
  if (isActive(job) || job.status === "failed" || job.status === "stopped") return job;
  const item = job.items && job.items[0];
  return item && item.state === "awaiting" && item.preview ? job : null;
}

async function loadList() {
  const mine = S;
  loading("콘티");
  try {
    const [d, cur] = await Promise.all([
      mine.ctx.api("GET", "/api/drafts"),
      mine.ctx.api("GET", "/api/jobs/current?kind=conti"),
    ]);
    if (!alive(mine)) return;
    mine.drafts = d.drafts || [];
    const only = mine.params.path;
    if (only) {
      const draft = mine.drafts.find((x) => x.path === only);
      if (draft && draft.conti) {
        location.replace(contiHref(draft.conti));
        return;
      }
    }
    mine.job = pendingJob(cur.job);
    renderList();
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    loadError(e, loadList);
  }
}

async function refreshDrafts() {
  const mine = S;
  try {
    const d = await mine.ctx.api("GET", "/api/drafts");
    if (!alive(mine) || mine.data) return;
    mine.drafts = d.drafts || [];
    renderDraftList();
  } catch { /* keep the old list */ }
}

function renderList() {
  const only = S.params.path;
  S.el.replaceChildren();
  S.el.append(liveRegion());
  if (only) S.el.append(h("p", { class: "crumb" }, h("a", { href: "#/conti" }, "← 콘티 목록")));
  S.el.append(h("h1", {}, "콘티"));
  if (!only) {
    S.el.append(h("p", { class: "lead" },
      "기획안으로 컷별 대사·가이드가 들어간 콘티를 만듭니다. 콘티를 열고 컷을 누르면 레퍼런스 실제 장면과 대사 대안을 볼 수 있습니다."));
  }
  S.nodes.job = h("div", { class: "job-box" });
  S.jobSig = null;
  S.nodes.list = h("ul", { class: "draft-list", "aria-label": "기획안" });
  S.el.append(S.nodes.job, S.nodes.list);
  renderJobBox();
  renderDraftList();
}

function renderDraftList() {
  const list = S.nodes.list;
  if (!list) return;
  const only = S.params.path;
  const drafts = only ? S.drafts.filter((d) => d.path === only) : S.drafts;
  list.replaceChildren();
  if (drafts.length === 0) {
    const card = h("li", { class: "card empty" });
    if (only) {
      card.append(h("h2", {}, "기획안을 찾을 수 없습니다"),
        h("p", { class: "muted" }, `${only} 파일이 없거나 프런트매터가 깨졌습니다.`),
        h("a", { class: "btn", href: "#/conti" }, "콘티 목록"));
    } else {
      card.append(h("h2", {}, "기획안이 아직 없습니다"),
        h("p", { class: "muted" }, "콘티는 기획안(drafts/)에서 만듭니다. 터미널에서 먼저 기획안을 만드세요."),
        h("p", {}, h("code", { class: "cmd" }, "/yt-coach:yt-draft")));
    }
    list.append(card);
    return;
  }
  for (const d of drafts) list.append(draftCard(d));
}

function draftCard(d) {
  const busy = S.creating || isActive(S.job);
  const titleId = `draft-${S.drafts.indexOf(d)}`;
  const li = h("li", { class: "card draft-card", "aria-labelledby": titleId });
  li.append(h("div", { class: "card-head" },
    h("h2", { id: titleId }, d.title),
    h("span", { class: `state ${d.conti ? "state-saved" : ""}` }, d.conti ? "콘티 있음" : "콘티 없음")));
  const meta = h("p", { class: "meta-line muted" }, h("time", { datetime: d.date }, d.date));
  if (d.category) meta.append(h("span", { class: "tag" }, d.category));
  meta.append(h("span", { class: "draft-path" }, d.path));
  li.append(meta);

  const row = h("div", { class: "btn-row" });
  if (!d.conti) {
    const make = h("button", { type: "button", class: "btn", disabled: busy }, "콘티 만들기");
    make.addEventListener("click", () => startCreate(d, false));
    row.append(make);
  } else {
    row.append(h("a", { class: "btn", href: contiHref(d.conti) }, "열기"));
    if (d.html) row.append(h("a", { class: "btn", href: filesUrl(d.html), target: "_blank", rel: "noopener" }, "스토리보드 HTML"));
    const redo = h("button", { type: "button", class: "btn", disabled: busy, "aria-expanded": String(S.recreateOpen === d.path) }, "다시 만들기");
    redo.addEventListener("click", () => {
      S.recreateOpen = S.recreateOpen === d.path ? null : d.path;
      renderDraftList();
      const yes = S.el.querySelector("[data-recreate-yes]");
      if (yes) yes.focus();
    });
    row.append(redo);
  }
  if (busy && isActive(S.job)) row.append(h("span", { class: "muted hint" }, "다른 콘티를 만드는 중입니다"));
  li.append(row);

  if (d.conti && S.recreateOpen === d.path) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-recreate-yes": "", disabled: busy }, "다시 만들기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", () => { S.recreateOpen = null; startCreate(d, true); });
    no.addEventListener("click", () => {
      S.recreateOpen = null;
      renderDraftList();
      const back = [...S.el.querySelectorAll(".draft-card button")].find((b) => b.textContent === "다시 만들기");
      if (back) back.focus();
    });
    li.append(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, RECREATE_WARNING),
      h("p", { class: "muted hint" }, "Claude 호출이 한 번 일어나고, 미리보기를 확인한 뒤 저장할 때 덮어씁니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  return li;
}

// ---- create job -------------------------------------------------------------------

async function startCreate(d, force) {
  const mine = S;
  if (mine.creating || isActive(mine.job)) return;
  mine.creating = true;
  mine.forceSave = false;
  renderDraftList();
  try {
    const data = await mine.ctx.api("POST", "/api/conti/create", { draft: d.path, force });
    if (!alive(mine)) return;
    mine.job = data.job;
    mine.pollErrors = 0;
    renderJobBox();
    if (isActive(mine.job)) schedulePoll();
    focusJobTitle();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
    if (e.error === "conti_exists") refreshDrafts();
  } finally {
    if (alive(mine)) { mine.creating = false; renderDraftList(); }
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
      mine.job = data.job;
      renderJobBox();
      if (wasActive && !isActive(mine.job)) renderDraftList();
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 콘티를 다시 만드세요", "error");
        mine.job = null;
        renderJobBox();
        renderDraftList();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

function jobDraft(job) {
  const item = job.items[0];
  return S.drafts.find((d) => d.path === item.id) || { path: item.id, title: item.title || item.id, conti: null };
}

function renderJobBox() {
  const box = S.nodes.job;
  if (!box) return;
  const job = S.job;
  const sig = job ? JSON.stringify([job.id, job.status, job.message, job.items[0], S.saving, S.forceSave, S.drafts.length]) : "";
  if (sig === S.jobSig) return;
  S.jobSig = sig;
  const hadFocus = box.contains(document.activeElement) || document.activeElement === document.body;
  if (!job) { box.replaceChildren(); return; }
  const item = job.items[0];
  const d = jobDraft(job);
  if (isActive(job)) {
    box.replaceChildren(h("section", { class: "card job-card", "aria-labelledby": "job-title" },
      h("h2", { id: "job-title" }, `콘티 만드는 중 · ${d.title}`),
      h("progress", { class: "job-progress", "aria-label": "콘티 만드는 중" }),
      h("p", { class: "muted" }, "Claude가 대사를 컷으로 나누고 촬영 가이드를 쓰고 있습니다. 보통 1~3분 걸립니다."),
      h("p", { class: "muted hint" }, "다른 화면으로 가도 계속 만듭니다. 돌아오면 미리보기가 기다립니다.")));
    announce("콘티를 만드는 중입니다");
    return;
  }
  if (item.state === "awaiting" && item.preview) {
    box.replaceChildren(previewCard(job, item, d));
    if (hadFocus && !S.saving) focusJobTitle();
    announce("콘티 미리보기가 준비되었습니다. 확인한 뒤 저장하세요");
    return;
  }
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  const close = h("button", { type: "button", class: "btn" }, "닫기");
  retry.addEventListener("click", () => { markHandled(job.id); S.job = null; startCreate(d, !!d.conti); });
  close.addEventListener("click", () => { markHandled(job.id); S.job = null; renderJobBox(); renderDraftList(); });
  box.replaceChildren(h("section", { class: "card job-card is-failed", "aria-labelledby": "job-title" },
    h("h2", { id: "job-title" }, `콘티를 만들지 못했습니다 · ${d.title}`),
    h("p", { class: "error" }, item.message || job.message || "작업이 중단되었습니다"),
    h("div", { class: "btn-row" }, retry, close)));
  announce("콘티 만들기가 실패했습니다");
}

function focusJobTitle() {
  const t = S.nodes.job && S.nodes.job.querySelector("h2");
  if (t) { t.setAttribute("tabindex", "-1"); t.focus(); }
}

function sectionTag(label) {
  return h("span", { class: "sec-tag" }, h("span", { class: `lb-dot lb-${label}`, "aria-hidden": "true" }), label);
}

function previewCard(job, item, d) {
  const p = item.preview;
  const script = p.script || [];
  const cuts = p.cuts || [];
  const warnings = p.warnings || [];
  const warnCuts = new Set(warnings.filter((w) => w.kind === "line_too_long").map((w) => w.cut));
  const overwrite = !!d.conti || S.forceSave;
  const card = h("section", { class: "card preview-card", "aria-labelledby": "job-title" });
  card.append(h("h2", { id: "job-title" }, `미리보기 · ${d.title}`),
    h("p", { class: "muted" }, `컷 ${cuts.length}개 · 저장하기 전에는 파일이 바뀌지 않습니다.`));
  if (overwrite) card.append(h("p", { class: "warn-text" }, RECREATE_WARNING));
  if (warnings.length) {
    card.append(h("ul", { class: "warn-list", "aria-label": `경고 ${warnings.length}개` },
      ...warnings.map((w) => h("li", {}, w.message))));
  }
  const table = h("table", { class: "preview-table" },
    h("caption", { class: "sr-only" }, "콘티 미리보기: 컷별 섹션, 샷, 길이, 대사, 가이드"),
    h("thead", {}, h("tr", {}, ...["컷", "섹션", "샷", "길이", "대사", "가이드"].map((t) => h("th", { scope: "col" }, t)))));
  const tbody = h("tbody");
  for (const c of cuts) {
    const label = (script[c.section] || {}).label || "";
    const warn = warnCuts.has(c.n);
    tbody.append(h("tr", { class: warn ? "is-warn" : "" },
      h("td", { class: "num" }, String(c.n), warn ? h("span", { class: "warn-icon", title: "대사가 길어요" }, " ⚠", h("span", { class: "sr-only" }, "대사가 깁니다")) : null),
      h("td", {}, sectionTag(label)),
      h("td", { class: "nowrap" }, c.size || ""),
      h("td", { class: "nowrap num" }, fmtSec(c.sec)),
      h("td", {}, c.line || h("span", { class: "muted" }, "—")),
      h("td", { class: "muted" }, c.guide || "")));
  }
  table.append(tbody);
  card.append(h("div", { class: "table-wrap", role: "region", "aria-label": "콘티 미리보기 표", tabindex: "0" }, table));

  const save = h("button", { type: "button", class: "btn btn-primary", disabled: S.saving }, overwrite ? "덮어쓰고 저장" : "저장");
  const cancel = h("button", { type: "button", class: "btn", disabled: S.saving }, "취소");
  save.addEventListener("click", () => savePreview(job, item, d, overwrite));
  cancel.addEventListener("click", () => {
    markHandled(job.id);
    S.job = null;
    renderJobBox();
    renderDraftList();
    S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  });
  card.append(h("div", { class: "btn-row" }, save, cancel));
  return card;
}

async function savePreview(job, item, d, force) {
  const mine = S;
  if (mine.saving) return;
  mine.saving = true;
  renderJobBox();
  try {
    const res = await mine.ctx.api("POST", "/api/conti/save", { draft: item.id, conti: item.preview.conti, force });
    if (!alive(mine)) return;
    markHandled(job.id);
    mine.job = null;
    mine.ctx.toast(`콘티를 저장했습니다 · 컷 ${res.cuts}개`, "info");
    location.hash = contiHref(res.path);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error === "conti_exists") mine.forceSave = true;
    mine.ctx.toast(e.message, "error");
  } finally {
    if (alive(mine)) { mine.saving = false; renderJobBox(); }
  }
}

// ---- editor -------------------------------------------------------------------------

async function loadEditor(keep = false) {
  const mine = S;
  if (!keep) loading("콘티");
  try {
    const data = await mine.ctx.api("GET", `/api/conti?path=${encodeURIComponent(mine.params.path)}`);
    if (!alive(mine)) return;
    mine.data = data;
    mine.hash = data.hash;
    mine.conflict = false;
    if (mine.sel !== null && !data.cuts.some((c) => c.n === mine.sel)) mine.sel = null;
    if (!keep || !mine.nodes.board) renderEditor();
    else { renderHeader(); renderCutList(); renderPanel(); }
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (keep && mine.nodes.board) { mine.ctx.toast(e.message, "error"); return; }
    if (e.error === "conti_not_found") {
      S.el.replaceChildren(h("h1", {}, "콘티"),
        h("p", { class: "error", role: "alert" }, e.message),
        h("a", { class: "btn", href: "#/conti" }, "콘티 목록"));
      return;
    }
    loadError(e, () => loadEditor(false));
  }
}

function currentCut() {
  return S.data ? S.data.cuts.find((c) => c.n === S.sel) || null : null;
}

function renderEditor() {
  S.el.replaceChildren();
  S.nodes = {};
  S.el.append(liveRegion(), h("p", { class: "crumb" }, h("a", { href: "#/conti" }, "← 콘티 목록")));
  S.nodes.header = h("div", { class: "conti-head" });
  S.nodes.conflict = h("div", { class: "conflict-slot" });
  const vertical = S.data.fm.format === "shorts";
  S.nodes.list = h("ol", { class: "cut-list", "aria-label": "컷 목록" });
  S.nodes.panel = h("section", { class: "cut-panel", "aria-labelledby": "panel-title" });
  S.nodes.board = h("div", { class: `conti-board ${vertical ? "is-vertical" : "is-wide"}` },
    h("div", { class: "cut-col" }, S.nodes.list), S.nodes.panel);
  S.el.append(S.nodes.header, S.nodes.conflict, S.nodes.board);
  S.nodes.list.addEventListener("keydown", onListKey);
  renderHeader();
  renderCutList();
  renderPanel();
}

function renderHeader() {
  const d = S.data;
  const fm = d.fm;
  const head = S.nodes.header;
  const meta = h("p", { class: "meta-line muted" },
    h("span", { class: "tag" }, fm.format === "shorts" ? "쇼츠" : "롱폼"),
    h("span", {}, `컷 ${d.cuts.length}개`),
    h("span", {}, `${fmtSec(fm.target_sec)}`),
    h("span", {}, `말 속도 ${fm.speech_cps}자/초`));
  if (fm.category) meta.append(h("span", { class: "tag" }, fm.category));
  const actions = h("div", { class: "btn-row" });
  if (d.html) actions.append(h("a", { class: "btn", href: filesUrl(d.html), target: "_blank", rel: "noopener" }, "스토리보드 HTML"));
  head.replaceChildren(h("div", { class: "conti-title" }, h("h1", {}, fm.title || d.path), actions), meta);
  const warns = d.warnings || [];
  if (warns.length) {
    head.append(h("details", { class: "warn-box" },
      h("summary", {}, `경고 ${warns.length}개`),
      h("ul", { class: "warn-list" }, ...warns.map((w) => h("li", {}, w.message)))));
  }
  renderConflict();
}

function renderConflict() {
  const slot = S.nodes.conflict;
  if (!slot) return;
  if (!S.conflict) { slot.replaceChildren(); return; }
  const btn = h("button", { type: "button", class: "btn btn-danger" }, CHANGED_TEXT);
  btn.addEventListener("click", () => loadEditor(true));
  slot.replaceChildren(h("div", { class: "conflict", role: "alert" },
    h("p", {}, "다른 곳(편집기·터미널)에서 이 콘티 파일이 바뀌어 저장하지 않았습니다. 다시 불러온 뒤 고쳐 주세요. 입력해 둔 내용은 남아 있습니다."),
    btn));
}

function warnCutSet() {
  return new Set((S.data.warnings || []).filter((w) => w.kind === "line_too_long").map((w) => w.cut));
}

function refThumb(ref, cls) {
  if (!ref) return h("span", { class: `${cls} ref-none`, "aria-hidden": "true" });
  return h("img", { class: cls, src: filesUrl(ref.frame), alt: "", loading: "lazy" });
}

function matchBadge(match) {
  if (!match || match === "exact") return null;
  return h("span", { class: `badge match-${match}` }, MATCH_TEXT[match]);
}

function renderCutList() {
  const list = S.nodes.list;
  const col = list.parentElement;
  const scroll = col ? col.scrollTop : 0;
  const focusedN = list.contains(document.activeElement) ? Number(document.activeElement.dataset.n) : null;
  const warn = warnCutSet();
  const tabN = S.sel !== null ? S.sel : (S.data.cuts[0] || {}).n;
  list.replaceChildren();
  for (const c of S.data.cuts) {
    const selected = c.n === S.sel;
    const w = warn.has(c.n);
    const btn = h("button", {
      type: "button", class: "cut-item" + (selected ? " is-selected" : ""), "data-n": String(c.n),
      "aria-current": selected ? "true" : false, tabindex: c.n === tabN ? "0" : "-1",
    });
    const sk = h("span", { class: "cut-sketch" });
    const svg = sanitizeSvg(c.sketch);
    if (svg) sk.append(svg);
    const top = h("span", { class: "cut-top" },
      h("span", { class: "cut-n" }, `#${c.n}`),
      sectionTag(c.label),
      h("span", { class: "cut-size" }, c.size),
      h("span", { class: "cut-sec" }, fmtSec(c.sec)));
    if (w) top.append(h("span", { class: "warn-icon", title: "대사가 길어요" }, "⚠", h("span", { class: "sr-only" }, "대사가 깁니다")));
    const body = h("span", { class: "cut-body" }, top,
      h("span", { class: "cut-line" + (c.line ? "" : " muted") }, c.line || "(대사 없음)"));
    const ref = h("span", { class: "cut-ref" }, refThumb(c.ref, "cut-thumb"), matchBadge(c.ref && c.ref.match));
    btn.append(sk, body, ref);
    btn.addEventListener("click", () => selectCut(c.n));
    list.append(h("li", {}, btn));
  }
  if (col) col.scrollTop = scroll;
  if (focusedN !== null) {
    const again = list.querySelector(`[data-n="${focusedN}"]`);
    if (again) again.focus();
  }
}

function onListKey(e) {
  const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
  if (!keys.includes(e.key)) return;
  const btns = [...S.nodes.list.querySelectorAll(".cut-item")];
  const i = btns.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  let j = i;
  if (e.key === "ArrowDown") j = Math.min(i + 1, btns.length - 1);
  else if (e.key === "ArrowUp") j = Math.max(i - 1, 0);
  else if (e.key === "Home") j = 0;
  else j = btns.length - 1;
  btns[i].setAttribute("tabindex", "-1");
  btns[j].setAttribute("tabindex", "0");
  btns[j].focus();
}

function selectCut(n) {
  const narrow = window.matchMedia(NARROW).matches;
  if (S.sel !== n) {
    S.sel = n;
    for (const b of S.nodes.list.querySelectorAll(".cut-item")) {
      const on = Number(b.dataset.n) === n;
      b.classList.toggle("is-selected", on);
      if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    }
    renderPanel();
    const cut = currentCut();
    announce(`컷 ${n} · ${cut ? cut.label : ""}`);
  }
  if (narrow) {
    const title = S.nodes.panel.querySelector("#panel-title");
    if (title) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      S.nodes.panel.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
      title.focus({ preventScroll: true });
    }
  }
}

// ---- panel ---------------------------------------------------------------------------

function renderPanel() {
  const panel = S.nodes.panel;
  const cut = currentCut();
  const focusKey = panel.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.fk : null;
  const caret = focusKey && "selectionStart" in document.activeElement ? document.activeElement.selectionStart : null;
  if (!cut) {
    panel.replaceChildren(h("h2", { id: "panel-title", tabindex: "-1" }, "컷을 고르세요"),
      h("p", { class: "muted" }, "컷 목록에서 컷을 누르면 여기에 레퍼런스 실제 장면과 대사 대안이 나옵니다. 대안은 Claude가 만들기 때문에 10초~1분 걸립니다."));
    return;
  }
  const back = h("button", { type: "button", class: "btn back-to-list" }, "다른 컷 고르기");
  back.addEventListener("click", () => {
    const b = S.nodes.list.querySelector(`[data-n="${cut.n}"]`);
    if (b) { b.scrollIntoView({ block: "center" }); b.focus(); }
  });
  const svg = sanitizeSvg(cut.sketch);
  const compare = h("div", { class: "cut-compare" },
    h("figure", {}, h("div", { class: "fig-box" }, svg || ""), h("figcaption", {}, `도식 · ${cut.size}`)),
    h("figure", {},
      h("div", { class: "fig-box" }, cut.ref
        ? h("img", { src: filesUrl(cut.ref.frame), alt: `참고 프레임: ${cut.ref.title || ""}` })
        : h("span", { class: "muted" }, "참고 프레임 없음")),
      h("figcaption", {}, cut.ref ? `참고 프레임 · ${MATCH_TEXT[cut.ref.match] || ""}` : "참고 프레임")));
  const info = h("div", { class: "cut-info" },
    h("p", { class: "panel-line" + (cut.line ? "" : " muted") }, cut.line ? `“${cut.line}”` : "(대사 없음)"),
    cut.action ? h("p", { class: "muted small" }, `장면 · ${cut.action}`) : null,
    h("p", { class: "muted small" }, `앵글 ${cut.angle || "-"} · ${fmtSec(cut.start)}–${fmtSec(cut.end)}`));

  S.nodes.scenes = h("div", { class: "panel-body" });
  S.nodes.alts = h("div", { class: "panel-body" });
  panel.replaceChildren(
    h("div", { class: "panel-head" },
      h("h2", { id: "panel-title", tabindex: "-1" }, `컷 ${cut.n}`,
        h("span", { class: "panel-sub" }, sectionTag(cut.label), ` ${cut.size} · ${fmtSec(cut.sec)}`)),
      back),
    h("div", { class: "panel-top" }, compare, info),
    h("section", { class: "panel-sec", "aria-labelledby": "sec-scenes" },
      h("h3", { id: "sec-scenes" }, "레퍼런스 실제 장면"), S.nodes.scenes),
    h("section", { class: "panel-sec", "aria-labelledby": "sec-alts" },
      h("h3", { id: "sec-alts" }, `대사 대안 (${fmtSec(cut.sec)} · 최대 ${cut.max_chars}자)`), S.nodes.alts),
    h("section", { class: "panel-sec", "aria-labelledby": "sec-edit" },
      h("h3", { id: "sec-edit" }, "직접 고치기"), editForm(cut)),
  );
  renderScenes(cut.n);
  renderAlts(cut.n);
  if (!S.scenes.has(cut.n)) loadScenes(cut.n);
  scheduleAutoAlts(cut.n);
  if (focusKey) {
    const el = panel.querySelector(`[data-fk="${CSS.escape(focusKey)}"]`);
    if (el) {
      el.focus();
      if (caret !== null && "setSelectionRange" in el) { try { el.setSelectionRange(caret, caret); } catch { /* not a text field */ } }
    }
  }
}

// ---- scenes ----------------------------------------------------------------------------

async function loadScenes(n) {
  const mine = S;
  mine.scenes.set(n, { loading: true, items: [], error: null });
  renderScenes(n);
  try {
    const data = await mine.ctx.api("GET",
      `/api/conti/scenes?path=${encodeURIComponent(mine.params.path)}&n=${encodeURIComponent(n)}`);
    if (!alive(mine)) return;
    mine.scenes.set(n, { loading: false, items: data.scenes || [], error: null });
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.scenes.set(n, { loading: false, items: [], error: e.message });
  }
  if (alive(mine) && mine.sel === n) renderScenes(n);
}

function renderScenes(n) {
  const box = S.nodes.scenes;
  const cut = currentCut();
  if (!box || !cut || cut.n !== n) return;
  const rec = S.scenes.get(n);
  const pinned = cut.ref && cut.ref.match === "pinned" ? cut.ref.frame : null;
  box.replaceChildren();
  if (!rec || rec.loading) { box.append(h("p", { class: "muted", role: "status" }, "장면을 찾는 중…")); return; }
  if (rec.error) {
    const retry = h("button", { type: "button", class: "btn", "data-fk": "scenes-retry" }, "다시 시도");
    retry.addEventListener("click", () => loadScenes(n));
    box.append(h("p", { class: "error" }, rec.error), retry);
    return;
  }
  if (rec.items.length === 0) {
    box.append(h("p", { class: "muted" },
      `이 카테고리 레퍼런스에서 '${cut.size}' 장면을 찾지 못했습니다. 레퍼런스 추가에서 "샷 분류도 함께"로 분석하면 후보가 생깁니다.`));
  } else {
    box.append(h("p", { class: "muted small" }, "같은 샷 크기·섹션의 레퍼런스 프레임입니다. 누르면 이 컷의 참고 프레임으로 고정합니다."));
    const grid = h("ul", { class: "scene-grid" });
    for (const sc of rec.items) {
      const on = sc.frame === pinned;
      const btn = h("button", {
        type: "button", class: "scene" + (on ? " is-pinned" : ""), "aria-pressed": String(on),
        "data-fk": `scene:${sc.frame}`, disabled: S.saving,
        "aria-label": `${sc.title || sc.video} · ${sc.size} · ${sc.section || ""} ${on ? "(고정됨)" : "고정하기"}`,
      },
      h("img", { src: filesUrl(sc.frame), alt: "", loading: "lazy" }),
      h("span", { class: "scene-meta" },
        h("span", { class: "scene-title" }, sc.title || sc.video),
        h("span", { class: "scene-tags" },
          h("span", { class: `badge match-${sc.match}` }, sc.match === "exact" ? "일치" : "근사"),
          on ? h("span", { class: "badge match-pinned" }, "고정됨") : null)));
      btn.addEventListener("click", () => { if (!on) saveCut(n, { ref_frame: sc.frame }, `scene:${sc.frame}`); });
      grid.append(h("li", {}, btn));
    }
    box.append(grid);
  }
  if (pinned) {
    const un = h("button", { type: "button", class: "btn", "data-fk": "unpin", disabled: S.saving }, "고정 해제");
    un.addEventListener("click", () => saveCut(n, { ref_frame: "" }, "unpin"));
    box.append(h("div", { class: "btn-row" }, un,
      pinned && !rec.items.some((s) => s.frame === pinned) ? h("span", { class: "muted small" }, "지금 고정한 프레임은 위 후보에 없는 프레임입니다") : null));
  }
}

// ---- alternatives -------------------------------------------------------------------------

// Opening a cut asks for alternatives automatically, but only after a short pause (skimming cuts costs nothing)
// and only when no other alternatives request is in flight (at most one paid Claude call at a time from auto).
// The buttons (대안 받기 · 다시 시도 · 대안 더 보기) stay manual.
function scheduleAutoAlts(n) {
  clearAutoAlts();
  if (S.alts.has(n)) return;
  const mine = S;
  mine.altTimer = setTimeout(() => {
    mine.altTimer = null;
    if (!alive(mine) || mine.sel !== n || mine.alts.has(n)) return;
    if ([...mine.alts.values()].some((r) => r.loading)) {
      mine.altSkipped = n;
      renderAlts(n);
      return;
    }
    requestAlts(n);
  }, AUTO_ALT_DELAY_MS);
}

async function requestAlts(n) {
  const mine = S;
  const rec = mine.alts.get(n) || { items: [], shown: new Set(), loading: false, error: null, max: null };
  if (rec.loading) return;
  rec.loading = true;
  rec.error = null;
  mine.alts.set(n, rec);
  renderAlts(n);
  const cut = mine.data.cuts.find((c) => c.n === n);
  const exclude = [...rec.shown];
  if (cut && cut.line && !rec.shown.has(cut.line)) exclude.push(cut.line);
  try {
    const data = await mine.ctx.api("POST", "/api/conti/alternatives",
      { path: mine.params.path, n, count: ALT_COUNT, exclude });
    if (!alive(mine)) return;
    for (const a of data.alternatives || []) {
      if (rec.shown.has(a.line)) continue;
      rec.shown.add(a.line);
      rec.items.push(a);
    }
    rec.max = data.max_chars;
    if (mine.sel === n) announce(`대사 대안 ${rec.items.length}개`);
  } catch (e) {
    if (!alive(mine)) return;
    rec.error = e.error === "aborted" ? null : e.message;
  } finally {
    rec.loading = false;
  }
  if (alive(mine) && mine.sel === n) renderAlts(n);
}

function renderAlts(n) {
  const box = S.nodes.alts;
  const cut = currentCut();
  if (!box || !cut || cut.n !== n) return;
  const rec = S.alts.get(n);
  box.replaceChildren();
  if (!rec) {
    if (S.altSkipped === n && !S.altTimer) {
      const ask = h("button", { type: "button", class: "btn", "data-fk": "alts-ask" }, "대안 받기");
      ask.addEventListener("click", () => requestAlts(n));
      box.append(h("p", { class: "muted small" }, "다른 컷의 대안을 만드는 중이라 자동으로 요청하지 않았습니다."),
        h("div", { class: "btn-row" }, ask, h("span", { class: "muted small" }, "누르면 Claude 호출이 한 번 일어납니다")));
    } else {
      box.append(h("p", { class: "muted small" }, "잠시 뒤 대사 대안을 요청합니다…"));
    }
    return;
  }
  const max = rec.max || cut.max_chars;
  if (rec.items.length) {
    const ul = h("ul", { class: "alt-list" });
    rec.items.forEach((a, i) => {
      const same = a.line === cut.line;
      const use = h("button", { type: "button", class: "btn", "data-fk": `alt:${i}`, disabled: same || S.saving }, same ? "지금 대사" : "이걸로 바꾸기");
      use.addEventListener("click", () => saveCut(n, { line: a.line }, `alt:${i}`));
      ul.append(h("li", { class: "alt" + (same ? " is-current" : "") },
        a.line ? h("p", { class: "alt-line" }, a.line) : h("p", { class: "alt-line muted" }, "(대사 없이 화면으로만)"),
        a.intent ? h("p", { class: "muted small" }, a.intent) : null,
        h("div", { class: "alt-foot" },
          h("span", { class: "alt-chars" + (a.over ? " is-over" : "") },
            `${a.chars}/${max}자`, a.over ? h("span", {}, " · 길어요") : null),
          use)));
    });
    box.append(ul);
  }
  if (rec.loading) {
    box.append(h("p", { class: "muted alt-loading", role: "status" },
      h("span", { class: "spinner", "aria-hidden": "true" }),
      rec.items.length ? "대안을 더 만드는 중… (10초~1분)" : "Claude가 대사 대안을 만드는 중… (10초~1분)"));
    return;
  }
  if (rec.error) {
    const retry = h("button", { type: "button", class: "btn", "data-fk": "alts-retry" }, "다시 시도");
    retry.addEventListener("click", () => requestAlts(n));
    box.append(h("div", { class: "alt-error" }, h("p", { class: "error" }, rec.error), retry));
    return;
  }
  const more = h("button", { type: "button", class: "btn", "data-fk": "alts-more" }, "대안 더 보기");
  more.addEventListener("click", () => requestAlts(n));
  box.append(h("div", { class: "btn-row" }, more, h("span", { class: "muted small" }, "누를 때마다 Claude 호출이 한 번 일어납니다")));
}

// ---- edit form ---------------------------------------------------------------------------

function formValues(cut) {
  const draft = S.forms.get(cut.n) || {};
  const out = {};
  for (const f of FIELDS) out[f.key] = f.key in draft ? draft[f.key] : (cut[f.key] || "");
  return out;
}

function changedFields(cut) {
  const vals = formValues(cut);
  const out = {};
  for (const f of FIELDS) if (vals[f.key].trim() !== (cut[f.key] || "").trim()) out[f.key] = vals[f.key];
  return out;
}

function editForm(cut) {
  const vals = formValues(cut);
  const form = h("form", { class: "edit-form", novalidate: "" });
  const err = h("p", { class: "error field-error", role: "alert", hidden: true });
  const save = h("button", { type: "submit", class: "btn btn-primary", "data-fk": "save", disabled: true }, "저장");
  const reset = h("button", { type: "button", class: "btn", "data-fk": "reset", disabled: true }, "되돌리기");
  const sync = () => {
    const dirty = Object.keys(changedFields(cut)).length > 0;
    save.disabled = !dirty || S.saving;
    reset.disabled = !dirty || S.saving;
  };
  for (const f of FIELDS) {
    const id = `edit-${f.key}`;
    const input = f.multiline
      ? h("textarea", { id, rows: f.key === "guide" ? "3" : "2", "data-fk": `f:${f.key}` })
      : h("input", { id, type: "text", "data-fk": `f:${f.key}` });
    input.value = vals[f.key];
    const field = h("div", { class: "field" }, h("label", { for: id }, f.label), input);
    if (f.key === "line") {
      const count = h("p", { class: "muted small char-count", id: "edit-line-count" });
      const upd = () => {
        const n = speechChars(input.value);
        count.textContent = `${n}/${cut.max_chars}자${n > cut.max_chars ? " · 이 길이에는 길어요" : ""}`;
        count.classList.toggle("is-over", n > cut.max_chars);
      };
      input.setAttribute("aria-describedby", "edit-line-count");
      input.addEventListener("input", upd);
      upd();
      field.append(count);
    }
    if (f.key === "guide") input.setAttribute("aria-describedby", "edit-err");
    input.addEventListener("input", () => {
      const d = S.forms.get(cut.n) || {};
      d[f.key] = input.value;
      S.forms.set(cut.n, d);
      err.hidden = true;
      input.removeAttribute("aria-invalid");
      sync();
    });
    form.append(field);
  }
  err.id = "edit-err";
  form.append(err, h("div", { class: "btn-row" }, save, reset,
    h("span", { class: "muted small" }, "바뀐 칸만 저장합니다. 대사를 바꾸면 섹션 전체 대사도 이어서 바뀝니다.")));
  reset.addEventListener("click", () => { S.forms.delete(cut.n); renderPanel(); const f = S.nodes.panel.querySelector("#edit-line"); if (f) f.focus(); });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const fields = changedFields(cut);
    if (!Object.keys(fields).length) return;
    if ("guide" in fields && !fields.guide.trim()) {
      err.textContent = "가이드는 비울 수 없습니다.";
      err.hidden = false;
      const g = form.querySelector("#edit-guide");
      g.setAttribute("aria-invalid", "true");
      g.focus();
      return;
    }
    saveCut(cut.n, fields, "save");
  });
  sync();
  return form;
}

// ---- save one cut --------------------------------------------------------------------------

function setPanelBusy(busy) {
  for (const b of S.nodes.panel.querySelectorAll("button, input, textarea")) {
    if (busy) { if (!b.disabled) { b.disabled = true; b.dataset.busy = "1"; } }
    else if (b.dataset.busy) { b.disabled = false; delete b.dataset.busy; }
  }
  S.nodes.panel.setAttribute("aria-busy", String(busy));
}

async function saveCut(n, fields, focusKey) {
  const mine = S;
  if (mine.saving) return;
  if (mine.conflict) { mine.ctx.toast("파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요", "error"); return; }
  mine.saving = true;
  setPanelBusy(true);
  try {
    const res = await mine.ctx.api("POST", "/api/conti/cut", { path: mine.data.path, n, fields, hash: mine.hash });
    if (!alive(mine)) return;
    mine.hash = res.hash;
    const d = mine.forms.get(n);
    if (d) { for (const k of Object.keys(fields)) delete d[k]; if (!Object.keys(d).length) mine.forms.delete(n); }
    mine.ctx.toast(SAVED_TOAST, "info");
    // Stay "saving" through the reload so a second save cannot race it with a stale hash.
    await loadEditor(true);
    if (!alive(mine)) return;
    mine.saving = false;
    setPanelBusy(false);
    renderPanel();
    const el = focusKey && mine.nodes.panel.querySelector(`[data-fk="${CSS.escape(focusKey)}"]`);
    if (el && !el.disabled) el.focus();
    else if (mine.nodes.panel.contains(document.activeElement) === false) {
      const t = mine.nodes.panel.querySelector("#panel-title");
      if (t) t.focus({ preventScroll: true });
    }
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.saving = false;
    setPanelBusy(false);
    if (e.error === "conti_changed") {
      mine.conflict = true;
      renderConflict();
      const b = mine.nodes.conflict.querySelector("button");
      if (b) b.focus();
    } else {
      mine.ctx.toast(e.message, "error");
    }
  } finally {
    if (alive(mine) && mine.saving) { mine.saving = false; setPanelBusy(false); }
  }
}

// ---- page contract ------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  clearAutoAlts();
  S = freshState(el, params, ctx);
  const path = params.path;
  if (path && isContiPath(path)) await loadEditor(false);
  else await loadList();
}

export function unmount() {
  stopPolling();
  clearAutoAlts();
  S = null;
}
