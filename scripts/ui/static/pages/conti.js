// 콘티 화면
//   #/conti            기획안 목록 + 콘티 만들기(작업 폴링 → 미리보기 → 저장)
//   #/conti/<draft>    기획안 하나(콘티가 있으면 그 콘티로 바로 이동)
//   #/conti/<conti>    편집: 장면 도구 줄(합치기 · 사진 설명 · 사진 다시 고르기 · 장면 그림, conti-scenes.js), 도구 줄(검색 ·
//                      구간 칩 · 카드/리스트), 장면 카드 보드(conti-board.js) 또는 컷 목록, 패널(장면 그림·참고 사진 · 레퍼런스
//                      실제 장면 · 대사 대안 · 직접 고치기)
// 모든 컷 저장은 마지막으로 받은 hash를 보낸다. 파일이 밖에서 바뀌면 서버가 conti_changed로 거절한다.
import { h, filesUrl } from "/static/dom.js";
import { button } from "/static/ui.js";
import { sectionLabelName } from "/static/labels.js";
import {
  SHOT_SIZES, alignCuts, artCaption, canRemove, changeText, collapseChanges, dropTarget, fieldText, filterCuts, fmtClock, fmtDelta, fmtWhen,
  gridMove, idOfN, refCaption, remapByCutId, sceneVisual, sectionGroups, snapSec, stepMove, versionLabel,
} from "/static/conti-utils.js";
import { noMatch, readView, sceneCard, toolRow, writeView } from "/static/conti-board.js";
import { mountSceneActions } from "/static/conti-scenes.js";
import { mountFootageArea } from "/static/footage-area.js";
import { KIND_TEXT, kindGroup } from "/static/footage-utils.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const ALT_COUNT = 3;
const NARROW = "(max-width: 900px)";
const HANDLED_KEY = "ytc.conti.handled";
const TRASH_WARNING = "콘티와 스토리보드, 이 콘티로 만든 자막(SRT·자막 영상)을 휴지통으로 옮깁니다. 기획안은 그대로 둡니다";
const RECREATE_WARNING = "다시 만들면 화면에서 바꾼 대사·가이드·고정한 참고 프레임이 사라지고 내 메모만 남습니다";
const SAVED_TOAST = "저장했습니다 · 스토리보드도 다시 그렸습니다";
const CHANGED_TEXT = "파일이 밖에서 바뀌었습니다 — 다시 불러오기";
const MATCH_TEXT = { pinned: "참고(고정)", content: "참고", composition: "구도만 참고" };
const UNDO_MS = 8000;
const INSERT_LABEL = "여기에 장면 넣기";
const LAST_CUT_TEXT = "섹션에 장면이 하나는 있어야 해요";
const STALE_TEXT = "콘티가 바뀌었어요 — 다시 받아 주세요";
const EMPTY_TEXT = { guide: "가이드는 비울 수 없습니다.", action: "장면 설명은 비울 수 없습니다." };
const SEC_TEXT = "길이는 0.5~600초 사이로 적어 주세요 (0.5초 단위)";
// kind: text (default) | area | select | number. required fields cannot be saved empty.
const FIELDS = [
  { key: "line", label: "대사", kind: "area", rows: "2" },
  { key: "action", label: "장면 설명", kind: "area", rows: "2", required: true },
  { key: "guide", label: "촬영 가이드", kind: "area", rows: "3", required: true },
  { key: "size", label: "샷 크기", kind: "select", pair: true },
  { key: "sec", label: "길이(초)", kind: "number", pair: true },
  { key: "caption", label: "자막", kind: "text" },
  { key: "sfx", label: "효과음", kind: "text" },
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

// Inside the video workspace: the conti changed here (scene count, total length), so the workspace repaints its tab bar
// summary, shoot count and progress card. Outside it ctx has no contiChanged.
function contiChanged(mine) {
  if (alive(mine) && typeof mine.ctx.contiChanged === "function") mine.ctx.contiChanged();
}

// Inside the video workspace (params.embedded) the workspace shows the title, crumb and 인쇄: the page keeps a
// hidden heading only.
const embedded = () => !!(S && S.params && S.params.embedded);
const pageHeading = (text) => (embedded() ? h("h2", { class: "sr-only" }, "콘티") : h("h1", {}, text));
// Way out of an error state: the conti list, or (in the workspace, where that list is gone) the video list.
const listLink = () => (embedded() ? h("a", { class: "btn", href: "#/videos" }, "내 영상")
  : h("a", { class: "btn", href: "#/conti" }, "콘티 목록"));

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
    drafts: [], job: null, creating: false, saving: false, forceSave: false, recreateOpen: null, trashOpen: null, trashing: false,
    data: null, hash: null, sel: null, conflict: false,
    scenes: new Map(), alts: new Map(), forms: new Map(),
    nodes: {}, live: null, lastAnnounce: "",
    editing: false, undo: null, menu: null, drag: null, hist: freshHist(),
    footage: null, verdicts: new Map(), actions: null,
    view: readView(), q: "", section: "all",
  };
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
  if (S && S.fitTimer) { clearTimeout(S.fitTimer); S.fitTimer = null; }
  if (S && S.cvTimer) { clearTimeout(S.cvTimer); S.cvTimer = null; }
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
  S.el.replaceChildren(pageHeading(title), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
}

function loadError(e, retryFn) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) retryFn(); });
  S.el.replaceChildren(
    pageHeading("콘티"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry, listLink()),
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
    if (only && mine.params.recreate) mine.recreateOpen = only;   // workspace "다시 만들기": the card's confirm
    else if (only) {
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
  if (only && !embedded()) S.el.append(h("p", { class: "crumb" }, h("a", { href: "#/conti" }, "← 콘티 목록")));
  S.el.append(pageHeading("콘티"));
  if (!only) {
    S.el.append(h("p", { class: "lead" },
      "기획안으로 컷별 대사·가이드가 들어간 콘티를 만듭니다. 콘티를 열고 장면을 누르면 레퍼런스 실제 장면과 대사 대안을 볼 수 있습니다."));
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
        h("p", { class: "muted" }, "기획안이 지워졌거나 내용을 읽을 수 없습니다."),
        listLink());
    } else {
      card.append(h("h2", {}, "기획안이 아직 없습니다"),
        h("p", { class: "muted" }, "콘티는 기획안에서 만듭니다. 먼저 기획안을 만드세요."),
        h("a", { class: "btn", href: "#/draft/new" }, "기획안 만들기"));
    }
    list.append(card);
    return;
  }
  for (const d of drafts) list.append(draftCard(d));
}

function draftCard(d) {
  const busy = S.creating || isActive(S.job);
  const titleId = `draft-${S.drafts.indexOf(d)}`;
  const li = h("li", { class: "card draft-card", "aria-labelledby": titleId, "data-draft": d.path });
  li.append(h("div", { class: "card-head" },
    h("h2", { id: titleId }, d.title),
    h("span", { class: `state ${d.conti ? "state-saved" : ""}` }, d.conti ? "콘티 있음" : "콘티 없음")));
  const meta = h("p", { class: "meta-line muted" }, h("time", { datetime: d.date }, d.date));
  if (d.category) meta.append(h("span", { class: "tag" }, d.category));
  li.append(meta);

  const row = h("div", { class: "btn-row" });
  if (!d.conti) {
    const make = h("button", { type: "button", class: "btn", disabled: busy, "data-tab-primary": "" }, "콘티 만들기");
    make.addEventListener("click", () => startCreate(d, false));
    row.append(make);
  } else {
    row.append(h("a", { class: "btn", href: contiHref(d.conti) }, "열기"));
    if (d.html) row.append(h("a", { class: "btn", href: filesUrl(d.html), target: "_blank", rel: "noopener" }, "스토리보드 HTML"));
    const redo = h("button", { type: "button", class: "btn", disabled: busy || S.trashing, "aria-expanded": String(S.recreateOpen === d.path) }, "다시 만들기");
    redo.addEventListener("click", () => {
      S.recreateOpen = S.recreateOpen === d.path ? null : d.path;
      S.trashOpen = null;
      renderDraftList();
      const yes = S.el.querySelector("[data-recreate-yes]");
      if (yes) yes.focus();
    });
    const del = h("button", { type: "button", class: "btn", disabled: busy || S.trashing,
      "aria-expanded": String(S.trashOpen === d.path), "data-trash": d.path }, "콘티 삭제");
    del.addEventListener("click", () => {
      S.trashOpen = S.trashOpen === d.path ? null : d.path;
      S.recreateOpen = null;
      renderDraftList();
      const yes = S.el.querySelector("[data-trash-yes]");
      if (yes) yes.focus();
    });
    row.append(redo, del);
  }
  if (busy && isActive(S.job)) row.append(h("span", { class: "muted hint" }, "다른 콘티를 만드는 중입니다"));
  li.append(row);

  if (d.conti && S.recreateOpen === d.path) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-recreate-yes": "", disabled: busy || S.trashing }, "다시 만들기");
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
  if (d.conti && S.trashOpen === d.path) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-trash-yes": "", disabled: busy || S.trashing }, "삭제");
    const no = h("button", { type: "button", class: "btn", disabled: S.trashing }, "취소");
    yes.addEventListener("click", () => trashConti(d));
    no.addEventListener("click", () => {
      S.trashOpen = null;
      renderDraftList();
      focusCardButton(d.path, "[data-trash]");
    });
    li.append(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, TRASH_WARNING),
      h("p", { class: "muted hint" }, "잘못 지웠으면 Claude에게 “휴지통에서 되살려줘”라고 말하세요."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  return li;
}

function focusCardButton(path, selector) {
  const card = [...S.el.querySelectorAll(".draft-card")].find((c) => c.dataset.draft === path);
  const target = card && card.querySelector(selector);
  if (target) target.focus();
}

// Moves the conti and its outputs to workspace/trash/ (server side); the draft stays.
async function trashConti(d) {
  const mine = S;
  if (mine.trashing) return;
  mine.trashing = true;
  renderDraftList();
  try {
    await mine.ctx.api("POST", "/api/conti/trash", { path: d.conti });
    if (!alive(mine)) return;
    mine.trashOpen = null;
    mine.ctx.toast("콘티를 휴지통으로 옮겼습니다");
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
    if (e.error === "not_found") mine.trashOpen = null;      // already gone outside the UI
  }
  // The list is reloaded whether or not the move worked, so it never shows a file that is gone.
  try {
    const fresh = await mine.ctx.api("GET", "/api/drafts");
    if (alive(mine)) mine.drafts = fresh.drafts || [];
  } catch { /* keep the old list */ } finally {
    if (alive(mine)) {
      mine.trashing = false;
      renderDraftList();
      focusCardButton(d.path, mine.trashOpen === d.path ? "[data-trash-yes]" : "button, a");
    }
  }
}

// ---- create job -------------------------------------------------------------------

async function startCreate(d, force) {
  const mine = S;
  if (mine.creating || mine.trashing || isActive(mine.job)) return;
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
  return h("span", { class: "sec-tag" }, h("span", { class: `lb-dot lb-${label}`, "aria-hidden": "true" }), sectionLabelName(label));
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
    h("p", { class: "muted" }, `장면 ${cuts.length}개 · 저장하기 전에는 파일이 바뀌지 않습니다.`));
  if (overwrite) card.append(h("p", { class: "warn-text" }, RECREATE_WARNING));
  if (warnings.length) {
    card.append(h("ul", { class: "warn-list", "aria-label": `경고 ${warnings.length}개` },
      ...warnings.map((w) => h("li", {}, w.message))));
  }
  const table = h("table", { class: "preview-table" },
    h("caption", { class: "sr-only" }, "콘티 미리보기: 장면별 섹션, 샷, 길이, 대사, 가이드"),
    h("thead", {}, h("tr", {}, ...["장면", "섹션", "샷", "길이", "대사", "가이드"].map((t) => h("th", { scope: "col" }, t)))));
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
    mine.ctx.toast(`콘티를 저장했습니다 · 장면 ${res.cuts}개`, "info");
    // in the workspace: the same video's conti tab again, without a new history entry
    if (embedded() && mine.ctx.navigate) mine.ctx.navigate(contiHref(res.path), { replace: true });
    else location.hash = contiHref(res.path);
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
    else { renderHeader(); renderBoardTools(); renderCutList(); renderPanel(); if (mine.footage) mine.footage.refresh(); }
    if (keep) contiChanged(mine);         // a reload after a save, restore or outside change
    if (!mine.fitChecked) { mine.fitChecked = true; resumeFit(); }
    if (!mine.cvChecked) { mine.cvChecked = true; loadCaptionVideo(); }
    refreshHistory();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (keep && mine.nodes.board) { mine.ctx.toast(e.message, "error"); return; }
    if (e.error === "conti_not_found") {
      S.el.replaceChildren(pageHeading("콘티"),
        h("p", { class: "error", role: "alert" }, e.message),
        listLink());
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
  S.el.append(liveRegion());
  if (!embedded()) S.el.append(h("p", { class: "crumb" }, h("a", { href: "#/conti" }, "← 콘티 목록")));
  S.nodes.header = h("div", { class: "conti-head" });
  S.nodes.conflict = h("div", { class: "conflict-slot" });
  S.nodes.srt = h("div", { class: "srt-slot" });
  const vertical = S.data.fm.format === "shorts";
  S.nodes.tools = h("div", { class: "cut-tools" });
  S.nodes.list = h("ol", { class: "cut-list", "aria-label": "장면 목록" });
  S.nodes.grid = h("ul", { class: "cb-grid", "aria-label": "장면 카드" });
  S.nodes.gridCol = h("div", { class: "cb-col" }, S.nodes.grid);
  S.nodes.boardTools = h("div", { class: "cb-tools-slot" });
  S.nodes.panel = h("section", { class: "cut-panel", "aria-labelledby": "panel-title" });
  S.nodes.board = h("div", { class: `conti-board ${vertical ? "is-vertical" : "is-wide"}` },
    h("div", { class: "cut-col" }, S.nodes.tools, S.nodes.list), S.nodes.gridCol, S.nodes.panel);
  S.el.append(S.nodes.header, S.nodes.conflict, sceneActions().el, footageArea().el, S.nodes.boardTools, S.nodes.board, S.nodes.srt);
  S.nodes.list.addEventListener("keydown", onListKey);
  S.nodes.grid.addEventListener("keydown", onGridKey);
  wireDrag(S.nodes.list);
  S.nodes.list.parentElement.addEventListener("scroll", closeMenu, { passive: true });
  if (typeof ResizeObserver === "function") {
    const mine = S;
    const ro = new ResizeObserver(() => {
      if (S !== mine) { ro.disconnect(); return; }
      mine.nodes.list.parentElement.style.setProperty("--tools-h", `${mine.nodes.tools.hidden ? 0 : mine.nodes.tools.offsetHeight}px`);
    });
    ro.observe(S.nodes.tools);
  }
  renderHeader();
  renderBoardTools();
  renderCutList();
  renderPanel();
  mountHistory();
}

// ---- scene actions (장면으로 합치기 · 사진 설명 만들기 · 사진 다시 고르기) ----------------------------------
// Mounted once per editor visit like the footage area. After a merge the editor redraws from the answer: drafts and
// alternatives follow their scene by id (merged-away cuts drop theirs), scene lists are dropped.

function sceneActions() {
  if (S.actions) return S.actions;
  const mine = S;
  S.actions = mountSceneActions(S.ctx, {
    path: S.data.path,
    category: S.data.fm.category || null,
    getData: () => mine.data,
    getHash: () => mine.hash,
    isBusy: () => mine.editing || mine.saving || mine.conflict,
    hasDrafts: () => unsavedCuts().length > 0,
    onMerged: (res) => {
      if (!alive(mine)) return;
      const before = mine.data;
      const selId = idOfN(before, mine.sel);
      remapByCutId(mine.forms, before, res);
      remapByCutId(mine.alts, before, res);
      mine.scenes.clear();
      mine.data = res;
      mine.hash = res.hash;
      mine.conflict = false;
      const keep = selId && res.cuts.find((c) => c.id === selId);
      mine.sel = keep ? keep.n : null;
      renderHeader();
      renderBoardTools();
      renderCutList();
      renderPanel();
      refreshHistory();
      if (mine.footage) mine.footage.refresh();
      announce(`장면 ${res.cuts.length}개로 합쳤습니다`);
      contiChanged(mine);
    },
    // a pick or drawing finishing while a cut save/edit is in flight: reload once that is done (flushReload)
    onReload: () => { if (!alive(mine)) return undefined; mine.reloadPending = true; return flushReload(mine); },
    onArtChange: () => { if (alive(mine)) refreshArt(); },
  });
  return S.actions;
}

// Runs a reload asked for while the editor was busy. Not over a conflict: the user reloads from the banner.
// Returns the reload's promise when it ran now.
function flushReload(mine) {
  if (!alive(mine) || !mine.reloadPending || mine.editing || mine.saving || mine.conflict) return undefined;
  mine.reloadPending = false;
  return loadEditor(true);
}

// ---- board tools: search, section chips, card / list --------------------------------------------

const SEARCH_MS = 120;

function shownCuts() {
  return filterCuts(S.data.cuts, { q: S.q, section: S.section });
}

function renderBoardTools() {
  const slot = S.nodes.boardTools;
  if (!slot) return;
  const keep = slot.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
  const cuts = S.data.cuts;
  const groups = sectionGroups(S.data.fm.script, cuts);
  if (S.section !== "all" && !groups.some((g) => g.key === S.section)) S.section = "all";
  slot.replaceChildren(toolRow({
    q: S.q, section: S.section, view: S.view, total: cuts.length, shown: shownCuts().length, groups,
    onQuery: (q) => {
      S.q = q;
      updateCount();
      clearTimeout(S.searchTimer);
      const mine = S;
      S.searchTimer = setTimeout(() => { if (alive(mine)) renderCutList(); }, SEARCH_MS);   // big contis: no redraw per key
    },
    onSection: (key) => { S.section = key; renderBoardTools(); renderCutList(); },
    onView: (view) => { S.view = view; writeView(view); renderBoardTools(); renderCutList(); renderPanel(); },
  }));
  S.nodes.board.classList.toggle("is-cards", S.view === "cards");
  S.nodes.board.classList.toggle("is-list", S.view === "list");
  if (keep) { const again = slot.querySelector(`[data-fk="${CSS.escape(keep)}"]`); if (again) again.focus(); }
}

// Typing in the search box only updates the "3/40개" count (the box itself is not rebuilt).
function updateCount() {
  const count = S.nodes.boardTools.querySelector(".cb-count");
  const shown = shownCuts().length;
  if (count) count.textContent = shown === S.data.cuts.length ? "" : `${shown}/${S.data.cuts.length}개`;
}

function clearFilter() {
  S.q = "";
  S.section = "all";
  renderBoardTools();
  renderCutList();
  const box = S.nodes.boardTools.querySelector(".cb-search");
  if (box) box.focus();
}

// The cut's button in the view on screen (card or list row).
function navNode(id) {
  if (!id) return null;
  return S.view === "cards" ? S.nodes.grid.querySelector(`[data-nav="card:${CSS.escape(id)}"]`)
    : S.nodes.list.querySelector(`[data-nav="cut:${CSS.escape(id)}"]`);
}

// ---- footage area (촬영 영상) -------------------------------------------------------------
// Mounted once per editor visit; renderEditor re-appends the same element. The area reports verdicts per cut id
// (dots in the cut list) and asks for an editor reload after it inserted a suggested cut.

function footageArea() {
  if (S.footage) return S.footage;
  const mine = S;
  S.footage = mountFootageArea(S.ctx, {
    path: S.data.path,
    getConti: () => mine.data,
    getHash: () => mine.hash,
    // Inserting a cut while a cut save/edit is in flight (or the file changed outside) would race the editor's hash.
    getEditorBusy: () => mine.editing || mine.saving || mine.conflict,
    onVerdicts: (map) => {
      const same = map.size === mine.verdicts.size
        && [...map].every(([id, v]) => { const o = mine.verdicts.get(id); return o && o.kind === v.kind; });
      mine.verdicts = map;
      if (!same && alive(mine) && mine.nodes.list) renderCutList();
    },
    onContiChanged: ({ cutId } = {}) => reloadAfterFootageInsert(mine, cutId),
    onSelectCut: (id) => {
      if (!alive(mine)) return;
      const cut = mine.data.cuts.find((c) => c.id === id);
      if (!cut) return;
      selectCut(cut.n);
      const row = navNode(id);
      if (row) { row.scrollIntoView({ block: "nearest" }); if (!window.matchMedia(NARROW).matches) row.focus({ preventScroll: true }); }
    },
  });
  return S.footage;
}

// After the footage area inserted a cut: like the editor's own insert, drafts, alternatives and the selection follow
// their cut by id (numbers shifted), scene lists are dropped. The area already has its new view: no footage refresh.
async function reloadAfterFootageInsert(mine, cutId) {
  if (!alive(mine)) return;
  try {
    const data = await mine.ctx.api("GET", `/api/conti?path=${encodeURIComponent(mine.params.path)}`);
    if (!alive(mine)) return;
    const before = mine.data;
    const selId = idOfN(before, mine.sel);
    remapByCutId(mine.forms, before, data);
    remapByCutId(mine.alts, before, data);
    mine.scenes.clear();
    mine.data = data;
    mine.hash = data.hash;
    mine.conflict = false;
    const keep = selId && data.cuts.find((c) => c.id === selId);
    mine.sel = keep ? keep.n : null;
    contiChanged(mine);
    const added = cutId && data.cuts.find((c) => c.id === cutId);
    if (added) { showEdited(added.id, true, false); return; }
    renderHeader();
    renderBoardTools();
    renderCutList();
    renderPanel();
    refreshHistory();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
  }
}

const DOT_TEXT = { filmed: "촬영: 찍음", review: "촬영: 확인 필요", remaining: "촬영: 남은 장면" };

function verdictDot(c) {
  const v = S.verdicts.get(c.id);
  if (!v) return null;
  const group = kindGroup(v.kind);
  const label = v.kind === "not_filmed" ? `촬영: ${KIND_TEXT.not_filmed}` : DOT_TEXT[group];
  return h("span", { class: `verdict-dot vd-${group}`, title: label },
    h("span", { class: "sr-only" }, label));
}

function renderHeader() {
  const d = S.data;
  const fm = d.fm;
  const head = S.nodes.header;
  const meta = h("p", { class: "meta-line muted" },
    h("span", { class: "tag" }, fm.format === "shorts" ? "쇼츠" : "롱폼"),
    h("span", {}, `장면 ${d.cuts.length}개`),
    h("span", { class: "total-delta" }, fmtDelta(d.total_sec, d.target_sec)),
    h("span", {}, `말 속도 ${fm.speech_cps}자/초`));
  if (fm.category) meta.append(h("span", { class: "tag" }, fm.category));
  const actions = h("div", { class: "btn-row" });
  actions.append(historyButton());
  // the subtitle tools are folded under the board: this opens them and goes there
  const srtLink = h("button", { type: "button", class: "btn btn-link btn-s", "data-fk": "srt-link", "aria-controls": "srt-box" }, "자막 내보내기");
  srtLink.addEventListener("click", () => {
    S.srtOpen = true;
    const box = S.el.querySelector("#srt-box");
    if (!box) return;
    box.open = true;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    box.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    box.querySelector("summary").focus({ preventScroll: true });
  });
  actions.append(srtLink);
  if (d.html && !embedded()) actions.append(h("a", { class: "btn", href: filesUrl(d.html), target: "_blank", rel: "noopener" }, "스토리보드 HTML"));
  head.replaceChildren(h("div", { class: "conti-title" }, pageHeading(fm.title || "콘티"), actions), meta);
  if (S.nodes.srt) S.nodes.srt.replaceChildren(srtBox());
  const warns = d.warnings || [];
  if (warns.length) {
    head.append(h("details", { class: "warn-box" },
      h("summary", {}, `경고 ${warns.length}개`),
      h("ul", { class: "warn-list" }, ...warns.map((w) => h("li", {}, w.message)))));
  }
  renderConflict();
  if (S.actions) S.actions.refresh();
}

const SRT_HINT = "CapCut PC: 캡션 → 자막 불러오기에서 받은 자막 파일을 고르세요. 콘티 시간 기준이라 실제 편집과 어긋나면 자막 칸을 끌어 맞추세요";
const TRACK_TEXT = { caption: "자막", line: "대사" };

// SRT, edited-video fit and the caption video sit under the board, folded (they come after filming and editing).
// The open state survives redraws (S.srtOpen); a stale output note shows on the folded line too.
function srtBox() {
  const stale = S.data.outputs_stale && (S.data.outputs_stale.srt || S.data.outputs_stale.video);
  const box = h("details", { class: "srt-box card", id: "srt-box" },
    h("summary", {}, h("span", { class: "srt-box-title" }, "자막 내보내기"),
      h("span", { class: "muted small" }, " CapCut용 SRT · 움직이는 자막 영상"),
      stale ? h("span", { class: "stale-note srt-box-stale" }, " · 콘티가 바뀌어 다시 받아야 해요") : null),
    srtRow());
  box.open = !!S.srtOpen;
  box.addEventListener("toggle", () => { S.srtOpen = box.open; });
  return box;
}

function srtRow() {
  const note = h("p", { class: "muted srt-note", role: "status", "aria-live": "polite" }, S.srtMsg || "");
  const caption = h("button", { type: "button", class: "btn" }, "CapCut용 자막(SRT) 받기");
  const line = h("button", { type: "button", class: "btn" }, "대사도 따로 받기");
  caption.addEventListener("click", () => exportSrt("caption", [caption, line], note));
  line.addEventListener("click", () => exportSrt("line", [caption, line], note));
  const stale = S.data.outputs_stale && S.data.outputs_stale.srt ? h("p", { class: "stale-note", role: "status" }, STALE_TEXT) : null;
  return h("div", { class: "srt-row" }, h("div", { class: "btn-row" }, caption, line), stale, note, srtFitBox(), captionVideoBox());
}

// ---- srt fit ----------------------------------------------------------------------
// The edited-video path lives only in S (this screen, this visit): it is never stored anywhere.

const FIT_HINT = "CapCut에서 컷 편집을 마친 영상을 자막 없이 내보낸 뒤, 그 파일 경로를 붙여 넣으세요 — 파일은 읽기만 합니다";

function fitActive() {
  return !!S.fitJob && ACTIVE.has(S.fitJob.status);
}

function fitSummary(preview) {
  const r = preview.retime || {};
  return `실제 컷 ${r.detected}개 · 자막 경계 ${r.snapped}/${r.inner}개를 컷에 맞춤 · 길이 비율 ${Number(r.scale).toFixed(2)}`;
}

// Everything the box shows comes from S.fit* so a header re-render (save, reload) rebuilds it unchanged.
function srtFitBox() {
  const busy = fitActive() || !!S.fitStarting;
  const inputId = "srtfit-video";
  const input = h("input", {
    type: "text", id: inputId, class: "srtfit-input", value: S.fitVideo || "", autocomplete: "off", spellcheck: "false",
    placeholder: "편집본 영상 파일의 경로", "aria-describedby": "srtfit-hint",
  });
  input.addEventListener("input", () => { S.fitVideo = input.value; });
  const caption = h("button", { type: "button", class: "btn", disabled: busy }, "실제 컷에 맞춰 SRT 받기");
  const line = h("button", { type: "button", class: "btn", disabled: busy }, "대사도 맞춰 받기");
  caption.addEventListener("click", () => startFit("caption", input));
  line.addEventListener("click", () => startFit("line", input));

  const status = h("div", { class: "srtfit-status", role: "status", "aria-live": "polite" });
  if (busy) {
    const step = S.fitJob && S.fitJob.items && S.fitJob.items[0] && S.fitJob.items[0].step;
    status.append(h("span", {}, h("span", { class: "spinner", "aria-hidden": "true" }),
      step === "write" ? "SRT를 쓰는 중…" : "컷을 찾는 중…"));
  } else if (S.fitDone) {
    const d = S.fitDone;
    if (!d.preview.url) {
      status.append(h("p", { class: "muted srt-note" }, "내보낼 자막이 없습니다"));
    } else {
      const name = d.preview.path.split("/").pop();
      status.append(h("p", { class: "srt-note" }, `${fitSummary(d.preview)} · `,
        h("a", { class: "btn", href: d.preview.url, download: name }, `맞춘 ${TRACK_TEXT[d.track] || "자막"} SRT 받기`)));
    }
    const warns = (d.preview.retime && d.preview.retime.warnings) || [];
    if (warns.length) status.append(h("ul", { class: "warn-list" }, ...warns.map((w) => h("li", {}, w))));
  }
  const err = h("p", { class: "error field-error", role: "alert" }, S.fitError || "");
  err.hidden = !S.fitError;

  return h("div", { class: "srtfit" },
    h("div", { class: "field" }, h("label", { for: inputId }, "편집한 영상 파일 경로"), input),
    h("p", { class: "muted hint", id: "srtfit-hint" }, FIT_HINT),
    h("div", { class: "btn-row" }, caption, line), err, status);
}

function refreshFit() {
  const box = S.el.querySelector(".srtfit");
  if (!box) return;
  const hadFocus = box.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
  box.replaceWith(srtFitBox());
  if (hadFocus) S.el.querySelector(".srtfit-input").focus();
}

function adoptFit(job) {
  S.fitJob = job;
  const item = job.items && job.items[0];
  if (job.status === "done" && item && item.state === "done" && item.preview) {
    S.fitDone = { preview: item.preview, track: S.fitTrack };
    S.fitError = "";
    const r = item.preview.retime || {};
    announce(`자막 시간 맞추기가 끝났습니다. 컷 ${r.snapped ?? 0}/${r.inner ?? 0}개를 맞췄습니다`);
  } else if (job.status === "failed" || job.status === "stopped") {
    S.fitDone = null;
    S.fitError = (item && item.message) || job.message || "시간 맞추기가 중단되었습니다";
    announce(`자막 시간 맞추기 실패: ${S.fitError}`);
  }
}

async function startFit(track, input) {
  const mine = S;
  if (mine.fitStarting || fitActive()) return;
  const video = input.value.trim();
  mine.fitVideo = input.value;
  if (!video) {
    mine.fitError = "편집한 영상 파일 경로를 입력하세요";
    refreshFit();
    S.el.querySelector(".srtfit-input").focus();
    return;
  }
  mine.fitStarting = true;
  mine.fitTrack = track;
  mine.fitError = "";
  mine.fitDone = null;
  refreshFit();
  try {
    const data = await mine.ctx.api("POST", "/api/conti/srt-fit", { path: mine.data.path, track, video });
    if (!alive(mine)) return;
    mine.fitJob = data.job;
    mine.fitErrors = 0;
    adoptFit(mine.fitJob);
    if (fitActive()) scheduleFitPoll();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.fitError = e.message || "시작하지 못했습니다";
  } finally {
    if (alive(mine)) { mine.fitStarting = false; refreshFit(); }
  }
}

function scheduleFitPoll() {
  if (S.fitTimer) clearTimeout(S.fitTimer);
  const mine = S;
  mine.fitTimer = setTimeout(async () => {
    mine.fitTimer = null;
    if (!alive(mine) || !mine.fitJob) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(mine.fitJob.id)}`);
      if (!alive(mine)) return;
      mine.fitErrors = 0;
      adoptFit(data.job);
      refreshFit();
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.fitJob = null;
        mine.fitError = "작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 시도하세요";
        refreshFit();
        return;
      }
      mine.fitErrors = (mine.fitErrors || 0) + 1;
    }
    if (alive(mine) && fitActive()) scheduleFitPoll();
  }, pollDelay(mine.fitErrors || 0));
}

// On entering the screen: bring back the progress of a fit job already running for this conti.
// A finished result is not restored (no auto download, and the user asked for it in an earlier visit).
async function resumeFit() {
  const mine = S;
  try {
    const cur = await mine.ctx.api("GET", "/api/jobs/current?kind=srtfit");
    if (!alive(mine) || mine.fitJob) return;
    const job = cur.job;
    const item = job && job.items && job.items[0];
    if (!job || !item || item.id !== mine.data.path || !ACTIVE.has(job.status)) return;
    mine.fitJob = job;
    mine.fitErrors = 0;
    refreshFit();
    scheduleFitPoll();
  } catch { /* no progress to restore */ }
}
// ---- end srt fit ----

// ---- caption video ----------------------------------------------------------------
// Animated caption video (Remotion, chroma-green MP4). Uses the same edited-video path field as the srt fit
// (S.fitVideo, this visit only, never stored). The install state comes from /api/captions/check.

const CV_SETUP_COMMAND = "ytc captions setup";
const CV_HINT = "CapCut: 이 영상을 맨 위 트랙에 얹고 크로마키로 초록을 빼세요";

function cvActive() {
  return !!S.cvJob && ACTIVE.has(S.cvJob.status);
}

function captionVideoBox() {
  const busy = cvActive() || !!S.cvStarting;
  const box = h("div", { class: "capvideo" }, h("h2", { class: "capvideo-title" }, "움직이는 자막 영상"));
  if (S.data && S.data.outputs_stale && S.data.outputs_stale.video) box.append(h("p", { class: "stale-note", role: "status" }, STALE_TEXT));
  if (S.cvReady === null || S.cvReady === undefined) {
    box.append(h("p", { class: "muted srt-note" }, "설치 상태를 확인하는 중…"));
    return box;
  }
  if (!S.cvReady.ready) {
    const copy = h("button", { type: "button", class: "btn" }, "명령 복사");
    copy.addEventListener("click", copySetupCommand);
    const recheck = h("button", { type: "button", class: "btn" }, "다시 확인");
    recheck.addEventListener("click", recheckCaptionSetup);
    box.append(
      h("p", { class: "muted hint" }, S.cvReady.message || "움직이는 자막을 만들려면 처음 한 번 설치가 필요합니다"),
      h("div", { class: "cmd-row" }, h("code", { class: "cmd" }, CV_SETUP_COMMAND), copy, recheck));
    return box;
  }
  const button = h("button", { type: "button", class: "btn", disabled: busy }, "자막 영상 만들기(초록 배경)");
  button.addEventListener("click", startCaptionVideo);
  const status = h("div", { class: "srtfit-status", role: "status", "aria-live": "polite" });
  if (busy) {
    const item = S.cvJob && S.cvJob.items && S.cvJob.items[0];
    const detecting = item && item.step === "detect";
    const label = detecting ? "컷을 찾는 중…" : `자막 영상을 만드는 중… ${(item && item.message) || ""}`;
    const stop = h("button", { type: "button", class: "btn", disabled: !S.cvJob || !!S.cvStopping },
      S.cvStopping ? "멈추는 중…" : "멈추기");
    stop.addEventListener("click", stopCaptionVideo);
    status.append(h("span", {}, h("span", { class: "spinner", "aria-hidden": "true" }), label, stop));
  } else if (S.cvDone) {
    const p = S.cvDone.preview;
    const name = p.path.split("/").pop();
    status.append(h("p", { class: "srt-note" }, `${p.width}×${p.height} · ${fmtSec(p.duration_sec)} · 자막 ${p.count}개 · `,
      h("a", { class: "btn", href: p.url, download: name }, "자막 영상 받기")),
    h("p", { class: "muted hint" }, CV_HINT));
  }
  const err = h("p", { class: "error field-error", role: "alert" }, S.cvError || "");
  err.hidden = !S.cvError;
  box.append(
    h("p", { class: "muted hint" }, "편집본 경로 칸이 채워져 있으면 그 영상의 컷·비율에 맞춰 만듭니다. 처음 한 번은 Chrome을 내려받느라 오래 걸릴 수 있습니다"),
    h("div", { class: "btn-row" }, button), err, status);
  return box;
}

function refreshCaptionVideo() {
  const box = S.el.querySelector(".capvideo");
  if (!box) return;
  box.replaceWith(captionVideoBox());
  if (S.cvFocus && S.cvDone) {
    S.cvFocus = false;
    const link = S.el.querySelector(".capvideo a[download]");
    if (link) link.focus();
  }
}

async function stopCaptionVideo() {
  const mine = S;
  if (!mine.cvJob || mine.cvStopping) return;
  mine.cvStopping = true;
  refreshCaptionVideo();
  try {
    await mine.ctx.api("POST", `/api/jobs/${encodeURIComponent(mine.cvJob.id)}/cancel`, {});
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error !== "not_running") mine.cvError = e.message || "멈추지 못했습니다";
    mine.cvStopping = false;
    refreshCaptionVideo();
  }
}

async function recheckCaptionSetup() {
  const mine = S;
  mine.cvReady = null;
  refreshCaptionVideo();
  await checkCaptionSetup();
  if (alive(mine)) refreshCaptionVideo();
}

async function copySetupCommand() {
  const mine = S;
  try {
    await navigator.clipboard.writeText(CV_SETUP_COMMAND);
    if (alive(mine)) mine.ctx.toast("명령을 복사했습니다", "info");
  } catch {
    if (alive(mine)) mine.ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
  }
}

function adoptCaptionVideo(job, focusResult = true) {
  S.cvJob = job;
  const item = job.items && job.items[0];
  if (ACTIVE.has(job.status)) return;
  S.cvStopping = false;
  if (job.status === "done" && item && item.state === "done" && item.preview) {
    S.cvDone = { preview: item.preview };
    S.cvError = "";
    S.cvFocus = focusResult;
    announce("자막 영상이 만들어졌습니다");
  } else if (job.status === "stopped") {
    S.cvDone = null;
    S.cvError = "멈췄습니다";
    announce("자막 영상 만들기를 멈췄습니다");
  } else if (job.status === "failed") {
    S.cvDone = null;
    S.cvError = (item && item.message) || job.message || "자막 영상 만들기가 중단되었습니다";
    announce(`자막 영상 만들기 실패: ${S.cvError}`);
  }
}

async function startCaptionVideo() {
  const mine = S;
  if (mine.cvStarting || cvActive()) return;
  const body = { path: mine.data.path };
  const video = (mine.fitVideo || "").trim();
  if (video) body.video = video;
  mine.cvStarting = true;
  mine.cvStopping = false;
  mine.cvError = "";
  mine.cvDone = null;
  refreshCaptionVideo();
  try {
    const data = await mine.ctx.api("POST", "/api/conti/caption-video", body);
    if (!alive(mine)) return;
    mine.cvJob = data.job;
    mine.cvErrors = 0;
    adoptCaptionVideo(mine.cvJob);
    if (cvActive()) scheduleCaptionPoll();
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.cvError = e.message || "시작하지 못했습니다";
  } finally {
    if (alive(mine)) { mine.cvStarting = false; refreshCaptionVideo(); }
  }
}

function scheduleCaptionPoll() {
  if (S.cvTimer) clearTimeout(S.cvTimer);
  const mine = S;
  mine.cvTimer = setTimeout(async () => {
    mine.cvTimer = null;
    if (!alive(mine) || !mine.cvJob) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(mine.cvJob.id)}`);
      if (!alive(mine)) return;
      mine.cvErrors = 0;
      adoptCaptionVideo(data.job);
      refreshCaptionVideo();
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.cvJob = null;
        mine.cvError = "작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 시도하세요";
        refreshCaptionVideo();
        return;
      }
      mine.cvErrors = (mine.cvErrors || 0) + 1;
    }
    if (alive(mine) && cvActive()) scheduleCaptionPoll();
  }, pollDelay(mine.cvErrors || 0));
}

async function checkCaptionSetup() {
  const mine = S;
  try {
    const res = await mine.ctx.api("GET", "/api/captions/check");
    if (alive(mine)) mine.cvReady = res;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.cvReady = { ready: false, message: e.message || "설치 상태를 확인하지 못했습니다" };
  }
}

// On entering the screen: check the install state, and bring back the latest caption job for this conti
// (running: progress; done: the download link; failed or stopped: the message).
async function loadCaptionVideo() {
  const mine = S;
  await checkCaptionSetup();
  if (!alive(mine)) return;
  try {
    const cur = await mine.ctx.api("GET", "/api/jobs/current?kind=caption_video");
    const job = cur.job;
    const item = job && job.items && job.items[0];
    if (alive(mine) && !mine.cvJob && item && item.id === mine.data.path) {
      mine.cvErrors = 0;
      adoptCaptionVideo(job, false);
      if (cvActive()) scheduleCaptionPoll();
    }
  } catch { /* nothing to restore */ }
  if (alive(mine)) refreshCaptionVideo();
}
// ---- end caption video ----

async function exportSrt(track, buttons, note) {
  const mine = S;
  if (mine.exporting) return;
  mine.exporting = true;
  for (const b of buttons) b.disabled = true;
  try {
    const res = await mine.ctx.api("POST", "/api/conti/srt", { path: mine.data.path, track });
    if (!alive(mine)) return;
    if (!res.path) {
      mine.srtMsg = "내보낼 자막이 없습니다";
    } else {
      // Relies on the browser's transient user activation from the click (it survives the short fetch above).
      const a = h("a", { href: filesUrl(res.path), download: res.path.split("/").pop() });
      a.hidden = true;
      document.body.append(a);
      a.click();
      a.remove();
      mine.srtMsg = `${TRACK_TEXT[track] || "자막"} SRT를 받았어요 (${res.count}개) · ${SRT_HINT}`;
    }
    note.textContent = mine.srtMsg;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
  } finally {
    mine.exporting = false;
    for (const b of buttons) b.disabled = false;
  }
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
  if (!match || match === "content") return null;
  return h("span", { class: `badge match-${match}` }, MATCH_TEXT[match]);
}

// Cut list: one group per section (header with name and time range); in a group a row per cut and an
// "insert here" slot above the first cut and under every cut. Rows keep the old roving tab stop (one Tab stop
// for the list, arrow keys inside); the slots are reached with the arrow keys too.
function selectedId() {
  const c = currentCut();
  return c ? c.id : null;
}

// Redraws the view on screen: the card grid, or the cut list. A search or section filter hides scenes in both; the
// list then has no insert slots and no drag handles (the slots would sit between scenes that are not neighbours).
function renderCutList(focusKey = null) {
  closeMenu(false);
  // only the view on screen holds scene buttons (no hidden duplicates of every data-nav)
  if (S.view === "cards") { S.nodes.list.replaceChildren(); renderCards(focusKey); return; }
  S.nodes.grid.replaceChildren();
  S.nodes.gridCol.querySelector(".cb-empty")?.remove();
  const list = S.nodes.list;
  const col = list.parentElement;
  const scroll = col ? col.scrollTop : 0;
  const active = document.activeElement;
  const keep = focusKey || (list.contains(active) && active.dataset ? active.dataset.nav || null : null);
  const warn = warnCutSet();
  const cuts = S.data.cuts;
  const shown = new Set(shownCuts().map((c) => c.id));
  const filtered = shown.size !== cuts.length;
  const tabId = [selectedId(), S.tabId, ...cuts.map((c) => c.id)].find((id) => id && shown.has(id));
  list.replaceChildren();
  list.classList.toggle("is-filtered", filtered);
  S.data.fm.script.forEach((sec, si) => {
    const all = cuts.filter((c) => c.section === si);
    const mine = all.filter((c) => shown.has(c.id));
    if (filtered && !mine.length) return;
    const head = h("div", { class: "sec-head", "data-section": String(si), "data-gap": String(all.length ? cuts.indexOf(all[0]) : cuts.length) },
      sectionTag(sec.label),
      h("span", { class: "sec-range num" }, `${fmtClock(sec.start)}–${fmtClock(sec.end)}`),
      h("span", { class: "sec-count muted" }, filtered ? `장면 ${mine.length}/${all.length}개` : `장면 ${all.length}개`));
    const rows = h("ul", { class: "sec-cuts", "aria-label": `${sectionLabelName(sec.label)} 장면` });
    if (mine.length && !filtered) rows.append(gapItem(cuts.indexOf(mine[0]), si, null));
    for (const c of mine) {
      rows.append(cutRow(c, warn.has(c.n), c.id === tabId, !filtered));
      if (!filtered) rows.append(gapItem(cuts.indexOf(c) + 1, si, c));
    }
    list.append(h("li", { class: "sec-group" }, head, rows));
  });
  if (!shown.size) list.append(h("li", {}, noMatch(clearFilter)));
  if (col) col.scrollTop = scroll;
  if (keep) {
    const again = list.querySelector(`[data-nav="${CSS.escape(keep)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
  renderTools();
}

function gapItem(at, section, after) {
  const btn = h("button", { type: "button", class: "gap-btn", tabindex: "-1", "data-nav": `gap:${at}:${section}` },
    h("span", { "aria-hidden": "true" }, "＋ "), INSERT_LABEL,
    h("span", { class: "sr-only" }, after ? ` (장면 ${after.n} 아래)` : " (섹션 맨 앞)"));
  btn.addEventListener("click", () => insertCut(at, section));
  return h("li", { class: "cut-gap", "data-gap": String(at), "data-section": String(section) }, btn);
}

function cutRow(c, w, isTab, movable = true) {
  const selected = c.n === S.sel;
  const btn = h("button", {
    type: "button", class: "cut-item" + (selected ? " is-selected" : ""), "data-n": String(c.n), "data-id": c.id,
    "data-nav": `cut:${c.id}`, "aria-current": selected ? "true" : false, tabindex: isTab ? "0" : "-1",
  });
  // the photo is the picture; the shot sketch only when the scene has no photo
  const sk = h("span", { class: "cut-sketch" });
  const svg = c.ref ? null : sanitizeSvg(c.sketch);
  if (svg) sk.append(svg);
  const top = h("span", { class: "cut-top" },
    h("span", { class: "cut-n" }, `#${c.n}`),
    h("span", { class: "cut-size" }, c.size),
    h("span", { class: "cut-sec" }, fmtSec(c.sec)));
  const dot = verdictDot(c);
  if (dot) top.append(dot);
  if (w) top.append(h("span", { class: "warn-icon", title: "대사가 길어요" }, "⚠", h("span", { class: "sr-only" }, "대사가 깁니다")));
  const body = h("span", { class: "cut-body" }, top,
    h("span", { class: "cut-line" + (c.line ? "" : " muted") }, c.line || "(대사 없음)"));
  const ref = h("span", { class: "cut-ref" }, refThumb(c.ref, "cut-thumb"), matchBadge(c.ref && c.ref.match));
  btn.append(sk, body, ref);
  btn.addEventListener("click", () => selectCut(c.n));

  const handle = movable ? h("span", { class: "cut-handle", draggable: "true", "aria-hidden": "true", title: "끌어서 순서 바꾸기" }, "⋮⋮")
    : h("span", { class: "cut-handle is-off", "aria-hidden": "true" });
  return h("li", { class: "cut-row", "data-id": c.id }, handle, btn, moreButton(c, isTab));
}

function moreButton(c, isTab) {
  const more = h("button", {
    type: "button", class: "cut-more", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": `장면 ${c.n} 메뉴`,
    "data-nav": `more:${c.id}`, tabindex: isTab ? "0" : "-1",
  }, "⋯");
  more.addEventListener("click", () => (S.menu && S.menu.id === c.id ? closeMenu() : openMenu(more, c)));
  return more;
}

// ---- card grid ------------------------------------------------------------------------------
// One Tab stop for the grid (the selected card, else the first); arrow keys move between cards. ⋯ on each card has
// the same menu as the list (넣기 · 복제 · 삭제, plus 앞으로 / 뒤로 옮기기 here since cards have no drag).

function renderCards(focusKey = null) {
  const grid = S.nodes.grid;
  const active = document.activeElement;
  const keep = focusKey ? focusKey.replace(/^cut:/, "card:")
    : (grid.contains(active) && active.dataset ? active.dataset.nav || null : null);
  const warn = warnCutSet();
  const shown = shownCuts();
  const tabId = [selectedId(), S.tabId, ...shown.map((c) => c.id)].find((id) => id && shown.some((c) => c.id === id));
  closeMenu(false);
  grid.replaceChildren(...shown.map((c) => sceneCard(c, {
    selected: c.n === S.sel, isTab: c.id === tabId, warn: warn.has(c.n), dot: verdictDot(c), artBusy: artBusy(c.id),
    onOpen: () => selectCut(c.n), more: moreButton(c, c.id === tabId),
  })));
  S.nodes.gridCol.querySelector(".cb-empty")?.remove();
  if (!shown.length) S.nodes.gridCol.append(noMatch(clearFilter));
  if (keep) {
    const again = grid.querySelector(`[data-nav="${CSS.escape(keep)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

function onGridKey(e) {
  const cur = document.activeElement;
  if (!cur.classList.contains("cb-open")) return;
  const cards = [...S.nodes.grid.querySelectorAll(".cb-open")];
  const i = cards.indexOf(cur);
  // rows come from the layout: the cards are positioned, so their offset tops would all be 0
  const j = i < 0 ? null : gridMove(cards.map((b) => b.getBoundingClientRect().top), i, e.key);
  if (j === null) return;
  e.preventDefault();
  S.tabId = cards[j].dataset.id;
  for (const b of cards) {
    b.setAttribute("tabindex", "-1");
    b.closest(".cb-card").querySelector(".cut-more").setAttribute("tabindex", "-1");
  }
  cards[j].setAttribute("tabindex", "0");
  cards[j].closest(".cb-card").querySelector(".cut-more").setAttribute("tabindex", "0");
  cards[j].focus();
  cards[j].scrollIntoView({ block: "nearest" });
}

function onListKey(e) {
  const keys = ["ArrowDown", "ArrowUp", "Home", "End", "ArrowRight", "ArrowLeft"];
  if (!keys.includes(e.key)) return;
  const cur = document.activeElement;
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    if (e.key === "ArrowRight" && cur.classList.contains("cut-item")) {
      e.preventDefault();
      cur.closest(".cut-row").querySelector(".cut-more").focus();
    } else if (e.key === "ArrowLeft" && cur.classList.contains("cut-more")) {
      e.preventDefault();
      cur.closest(".cut-row").querySelector(".cut-item").focus();
    }
    return;
  }
  const stops = [...S.nodes.list.querySelectorAll(".cut-item, .gap-btn")];
  let i = stops.indexOf(cur);
  if (i < 0 && cur.classList.contains("cut-more")) i = stops.indexOf(cur.closest(".cut-row").querySelector(".cut-item"));
  if (i < 0) return;
  e.preventDefault();
  let j = i;
  if (e.key === "ArrowDown") j = Math.min(i + 1, stops.length - 1);
  else if (e.key === "ArrowUp") j = Math.max(i - 1, 0);
  else if (e.key === "Home") j = 0;
  else j = stops.length - 1;
  const items = stops.filter((b) => b.classList.contains("cut-item"));
  for (const b of items) {
    b.setAttribute("tabindex", "-1");
    b.closest(".cut-row").querySelector(".cut-more").setAttribute("tabindex", "-1");
  }
  const tab = stops[j].classList.contains("cut-item")
    ? stops[j]
    : stops.slice(0, j).reverse().find((b) => b.classList.contains("cut-item")) || items[0];
  tab.setAttribute("tabindex", "0");
  tab.closest(".cut-row").querySelector(".cut-more").setAttribute("tabindex", "0");
  stops[j].focus();
}

// Tools above the list for the selected cut: 위로 / 아래로 (crossing a section edge changes its section).
function renderTools() {
  const box = S.nodes.tools;
  const cut = currentCut();
  if (!box) return;
  if (!cut) { box.replaceChildren(); box.hidden = true; return; }
  box.hidden = false;
  const cuts = S.data.cuts;
  const mk = (label, dir) => {
    const step = stepMove(cuts, cut.id, dir);
    const stuck = !!step && step.crosses && !canRemove(cuts, cut.id);
    const btn = h("button", { type: "button", class: "btn btn-sm", "data-tool": dir < 0 ? "up" : "down", disabled: !step || stuck || S.editing }, label);
    if (!step) btn.title = dir < 0 ? "맨 앞 장면입니다" : "맨 뒤 장면입니다";
    else if (stuck) btn.title = LAST_CUT_TEXT;
    else if (step.crosses) btn.title = "섹션 경계를 넘으면 장면의 섹션이 바뀝니다";
    btn.addEventListener("click", () => moveStep(cut.id, dir));
    return btn;
  };
  box.replaceChildren(h("span", { class: "cut-tools-label" }, `장면 ${cut.n}`), mk("위로", -1), mk("아래로", 1));
}

// ⋯ menu: 위에 넣기 · 아래에 넣기 · 복제 · 삭제. Position is fixed so the scrolling list never clips it.
function closeMenu(restoreFocus = true) {
  const m = S && S.menu;
  if (!m) return;
  S.menu = null;
  m.el.remove();
  document.removeEventListener("pointerdown", m.outside, true);
  window.removeEventListener("resize", closeMenu);
  m.btn.setAttribute("aria-expanded", "false");
  if (restoreFocus === true && m.btn.isConnected) m.btn.focus();
}

function openMenu(btn, cut) {
  closeMenu(false);
  const cuts = S.data.cuts;
  const at = cuts.findIndex((c) => c.id === cut.id);
  const removable = canRemove(cuts, cut.id);
  const entries = [
    { label: "위에 넣기", run: () => insertCut(at, cut.section) },
    { label: "아래에 넣기", run: () => insertCut(at + 1, cut.section) },
    { label: "복제", run: () => duplicateCut(cut.id) },
    { label: "삭제", danger: true, disabled: !removable, title: LAST_CUT_TEXT, run: () => deleteCut(cut.id) },
  ];
  if (S.view === "cards") {
    const step = (dir) => {
      const st = stepMove(cuts, cut.id, dir);
      return { disabled: !st || (st.crosses && !removable), title: !st ? (dir < 0 ? "맨 앞 장면입니다" : "맨 뒤 장면입니다") : LAST_CUT_TEXT };
    };
    entries.splice(2, 0,
      { label: "앞으로 옮기기", ...step(-1), run: () => moveStep(cut.id, -1) },
      { label: "뒤로 옮기기", ...step(1), run: () => moveStep(cut.id, 1) });
  }
  const menu = h("div", { class: "cut-menu", role: "menu", "aria-label": `장면 ${cut.n} 메뉴` });
  const items = entries.map((en) => {
    const it = h("button", {
      type: "button", role: "menuitem", class: "cut-menu-item" + (en.danger ? " is-danger" : ""), tabindex: "-1",
      "aria-disabled": en.disabled ? "true" : false, title: en.disabled ? en.title : false,
    }, en.label);
    it.addEventListener("click", () => {
      if (en.disabled) return;
      closeMenu(false);
      const row = navNode(cut.id);
      if (row) row.focus({ preventScroll: true });
      en.run();
    });
    return it;
  });
  menu.append(...items);
  document.body.append(menu);
  const r = btn.getBoundingClientRect();
  const mh = menu.offsetHeight;
  const below = r.bottom + 4 + mh <= window.innerHeight;
  const top = below ? r.bottom + 4 : r.top - 4 - mh;
  menu.style.top = `${Math.max(4, Math.min(top, window.innerHeight - mh - 4))}px`;
  menu.style.left = `${Math.max(4, Math.min(window.innerWidth - menu.offsetWidth - 4, r.right - menu.offsetWidth))}px`;
  const outside = (ev) => { if (!menu.contains(ev.target) && ev.target !== btn) closeMenu(false); };
  document.addEventListener("pointerdown", outside, true);
  window.addEventListener("resize", closeMenu);
  S.menu = { id: cut.id, el: menu, btn, outside };
  btn.setAttribute("aria-expanded", "true");
  menu.addEventListener("keydown", (e) => {
    const i = items.indexOf(document.activeElement);
    const go = (j) => { e.preventDefault(); items[(j + items.length) % items.length].focus(); };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? items.length - 1 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(items.length - 1);
    else if (e.key === "Escape") { e.preventDefault(); closeMenu(); }
    else if (e.key === "Tab") closeMenu(false);
  });
  items[0].focus();
}

function selectCut(n) {
  const narrow = window.matchMedia(NARROW).matches;
  if (S.sel !== n) {
    S.sel = n;
    for (const b of S.nodes.board.querySelectorAll(".cut-item, .cb-open")) {
      const on = Number(b.dataset.n) === n;
      b.classList.toggle("is-selected", on);
      const card = b.closest(".cb-card");
      if (card) card.classList.toggle("is-selected", on);
      if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    }
    renderPanel();
    renderTools();
    const cut = currentCut();
    announce(`장면 ${n} · ${cut ? sectionLabelName(cut.label) : ""}`);
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
  S.nodes.board.classList.toggle("has-sel", !!cut);
  if (!cut) {
    panel.replaceChildren(h("h2", { id: "panel-title", tabindex: "-1" }, "장면을 고르세요"),
      h("p", { class: "muted" }, "장면을 누르면 여기에 레퍼런스 실제 장면과 대사 대안이 나옵니다. 대안은 Claude가 만들기 때문에 10초~1분 걸립니다."));
    return;
  }
  // list view: on narrow screens, back up to the list. card view: close the panel and go back to the card.
  const cards = S.view === "cards";
  const back = h("button", { type: "button", class: cards ? "btn btn-s panel-close" : "btn back-to-list", "data-fk": "panel-close" },
    cards ? "닫기" : "다른 장면 고르기");
  back.addEventListener("click", () => {
    const id = cut.id;
    if (cards) { S.sel = null; S.tabId = id; renderPanel(); renderCards(); }
    const b = navNode(id);
    if (b) { b.scrollIntoView({ block: "center" }); b.focus(); }
  });
  // Claude's drawing next to the reference photo (a pinned photo hides the drawing); the shot sketch only when the
  // scene has neither
  const svg = cut.art || cut.ref ? null : sanitizeSvg(cut.sketch);
  S.nodes.artFig = sceneVisual(cut).kind === "art" ? artFigure(cut) : null;
  const compare = h("div", { class: "cut-compare" }, S.nodes.artFig, cut.ref
    ? h("figure", {},
      h("div", { class: "fig-box" }, h("img", { src: filesUrl(cut.ref.frame), alt: `참고 사진: ${cut.ref.desc || ""}` })),
      h("figcaption", {}, refCaption(cut.ref)))
    : svg ? h("figure", {}, h("div", { class: "fig-box" }, svg), h("figcaption", {}, `도식 · ${cut.size} · 참고 사진 없음`)) : null);
  const info = h("div", { class: "cut-info" },
    h("p", { class: "panel-line" + (cut.line ? "" : " muted") }, cut.line ? `“${cut.line}”` : "(대사 없음)"),
    cut.action ? h("p", { class: "muted small" }, `장면 · ${cut.action}`) : null,
    h("p", { class: "muted small" }, `앵글 ${cut.angle || "-"} · ${fmtSec(cut.start)}–${fmtSec(cut.end)}`));

  S.nodes.scenes = h("div", { class: "panel-body" });
  S.nodes.alts = h("div", { class: "panel-body" });
  panel.replaceChildren(
    h("div", { class: "panel-head" },
      h("h2", { id: "panel-title", tabindex: "-1" }, `장면 ${cut.n}`,
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
  if (focusKey) {
    const el = panel.querySelector(`[data-fk="${CSS.escape(focusKey)}"]`);
    if (el) {
      el.focus();
      if (caret !== null && "setSelectionRange" in el) { try { el.setSelectionRange(caret, caret); } catch { /* not a text field */ } }
    }
  }
}

// ---- scene drawings (장면 그림) -----------------------------------------------------------------
// The drawing is only ever an <img src> (the server serves the sanitized SVG file); never markup in the page.

// A drawing job runs on this conti, or the scene waits for its auto redraw.
const artBusy = (id) => !!(S && S.actions && S.actions.artBusy(id));

// The panel's drawing with its caption. An old drawing offers 다시 그리기 while nothing is drawing.
function artFigure(cut) {
  const busy = artBusy(cut.id);
  const cap = h("figcaption", { "data-art-cap": cut.id, tabindex: "-1" }, artCaption(cut.art, busy));
  if (cut.art.state === "stale" && !busy) {
    cap.append(button("다시 그리기", {
      size: "s", variant: "link", "data-fk": "art-redraw", "aria-label": `장면 ${cut.n} 그림 다시 그리기`,
      onClick: () => S.actions.redraw([cut.id]),
    }));
  }
  return h("figure", { class: "art-fig" },
    h("div", { class: "fig-box is-art" }, h("img", { src: cut.art.url, alt: "장면 그림", loading: "lazy" })), cap);
}

// A drawing job started or ended, or scenes wait to be drawn again: captions on the cards and the panel follow
// without redrawing the board (the edit form keeps its input).
function refreshArt() {
  if (!S || !S.data || !S.nodes.grid) return;
  for (const el of S.nodes.grid.querySelectorAll("[data-art-cap]")) {
    const c = S.data.cuts.find((x) => x.id === el.dataset.artCap);
    if (c && c.art) el.textContent = artCaption(c.art, artBusy(c.id));
  }
  const cut = currentCut();
  const old = S.nodes.artFig;
  if (!cut || !old || !old.isConnected || sceneVisual(cut).kind !== "art") return;
  const had = old.contains(document.activeElement);
  const next = artFigure(cut);
  old.replaceWith(next);
  S.nodes.artFig = next;
  if (had) {
    const b = next.querySelector('[data-fk="art-redraw"]');
    (b || next.querySelector("figcaption")).focus({ preventScroll: true });
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
      `이 카테고리 레퍼런스에서 '${cut.size}' 장면을 찾지 못했습니다. 레퍼런스 화면에서 "샷 분류도 함께"로 분석하면 후보가 생깁니다.`));
  } else {
    box.append(h("p", { class: "muted small" }, "같은 샷 크기·섹션의 레퍼런스 프레임입니다. 누르면 이 장면의 참고 프레임으로 고정합니다."));
    const grid = h("ul", { class: "scene-grid" });
    for (const sc of rec.items) {
      const on = sc.frame === pinned;
      const btn = h("button", {
        type: "button", class: "scene" + (on ? " is-pinned" : ""), "aria-pressed": String(on),
        "data-fk": `scene:${sc.frame}`, disabled: S.saving,
        "aria-label": `${sc.desc ? `${sc.desc} · ` : ""}${sc.title || sc.video} · ${sc.size} · ${sc.section || ""} ${on ? "(고정됨)" : "고정하기"}`,
      },
      h("img", { src: filesUrl(sc.frame), alt: "", loading: "lazy" }),
      h("span", { class: "scene-meta" },
        sc.desc ? h("span", { class: "scene-desc" }, sc.desc) : null,
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

// Every alternatives request is a paid Claude call, so all of them start from a button
// (대안 받기 · 다시 시도 · 대안 더 보기). Opening a cut never calls Claude.

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
    const ask = h("button", { type: "button", class: "btn", "data-fk": "alts-ask" }, "대안 받기");
    ask.addEventListener("click", () => requestAlts(n));
    box.append(h("div", { class: "btn-row" }, ask, h("span", { class: "muted small" }, "누르면 Claude 호출이 한 번 일어납니다")));
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
  if (!rec.items.length) {
    const retry = h("button", { type: "button", class: "btn", "data-fk": "alts-retry" }, "다시 시도");
    retry.addEventListener("click", () => requestAlts(n));
    box.append(h("div", { class: "btn-row" }, h("span", { class: "muted small" }, "대안을 만들지 못했습니다 —"), retry));
    return;
  }
  const more = h("button", { type: "button", class: "btn", "data-fk": "alts-more" }, "대안 더 보기");
  more.addEventListener("click", () => requestAlts(n));
  box.append(h("div", { class: "btn-row" }, more, h("span", { class: "muted small" }, "누를 때마다 Claude 호출이 한 번 일어납니다")));
}

// ---- edit form ---------------------------------------------------------------------------

const str = (v) => (v === null || v === undefined ? "" : String(v));

function formValues(cut) {
  const draft = S.forms.get(cut.n) || {};
  const out = {};
  for (const f of FIELDS) out[f.key] = f.key in draft ? draft[f.key] : str(cut[f.key]);
  return out;
}

function changedFields(cut) {
  const vals = formValues(cut);
  const out = {};
  for (const f of FIELDS) {
    const v = vals[f.key].trim();
    const same = f.kind === "number" ? v !== "" && Number(v) === Number(cut[f.key]) : v === str(cut[f.key]).trim();
    if (!same) out[f.key] = vals[f.key];
  }
  return out;
}

function fieldControl(f, cut, value) {
  const id = `edit-${f.key}`;
  const fk = `f:${f.key}`;
  if (f.kind === "area") return h("textarea", { id, rows: f.rows, "data-fk": fk }, value);
  if (f.kind === "select") {
    const sizes = SHOT_SIZES.includes(cut.size) ? SHOT_SIZES : [...SHOT_SIZES, cut.size];
    const sel = h("select", { id, "data-fk": fk }, ...sizes.map((s) => h("option", { value: s }, s)));
    sel.value = value;
    return sel;
  }
  const input = h("input", { id, type: f.kind === "number" ? "number" : "text", "data-fk": fk });
  if (f.kind === "number") {
    input.min = "0.5";
    input.max = "600";
    input.step = "0.5";
    input.setAttribute("inputmode", "decimal");
  }
  input.value = value;
  return input;
}

function editForm(cut) {
  const vals = formValues(cut);
  const form = h("form", { class: "edit-form", novalidate: "" });
  const err = h("p", { class: "error field-error", role: "alert", hidden: true, id: "edit-err" });
  const save = h("button", { type: "submit", class: "btn btn-primary", "data-fk": "save", disabled: true }, "저장");
  const reset = h("button", { type: "button", class: "btn", "data-fk": "reset", disabled: true }, "되돌리기");
  const sync = () => {
    const dirty = Object.keys(changedFields(cut)).length > 0;
    save.disabled = !dirty || S.saving;
    reset.disabled = !dirty || S.saving;
  };
  let pairRow = null;
  for (const f of FIELDS) {
    const id = `edit-${f.key}`;
    const input = fieldControl(f, cut, vals[f.key]);
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
    if (f.required) input.setAttribute("aria-describedby", "edit-err");
    if (f.kind === "number") field.append(h("p", { class: "muted small" }, "0.5초 단위, 0.5~600초"));
    const onChange = () => {
      const d = S.forms.get(cut.n) || {};
      d[f.key] = input.value;
      S.forms.set(cut.n, d);
      err.hidden = true;
      for (const el of form.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
      sync();
    };
    input.addEventListener(f.kind === "select" ? "change" : "input", onChange);
    if (f.pair) {
      if (!pairRow) { pairRow = h("div", { class: "field-row" }); form.append(pairRow); }
      pairRow.append(field);
    } else {
      form.append(field);
    }
  }
  form.append(err, h("div", { class: "btn-row" }, save, reset,
    h("span", { class: "muted small" }, "바뀐 칸만 저장합니다. 대사를 바꾸면 섹션 전체 대사도 이어서 바뀝니다.")));
  reset.addEventListener("click", () => { S.forms.delete(cut.n); renderPanel(); const f = S.nodes.panel.querySelector("#edit-line"); if (f) f.focus(); });
  const fail = (key, message) => {
    err.textContent = message;
    err.hidden = false;
    const el = form.querySelector(`#edit-${key}`);
    el.setAttribute("aria-invalid", "true");
    el.setAttribute("aria-describedby", "edit-err");
    el.focus();
  };
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const fields = changedFields(cut);
    for (const f of FIELDS) {
      if (f.required && f.key in fields && !fields[f.key].trim()) { fail(f.key, EMPTY_TEXT[f.key]); return; }
    }
    if ("sec" in fields) {
      const raw = fields.sec.trim();
      const n = Number(raw);
      if (raw === "" || !Number.isFinite(n) || n < 0.5 || n > 600) { fail("sec", SEC_TEXT); return; }
      fields.sec = snapSec(n);
      if (fields.sec === Number(cut.sec)) delete fields.sec;
    }
    if (!Object.keys(fields).length) return;
    saveCut(cut.n, fields, "save");
  });
  sync();
  return form;
}

// ---- save one cut --------------------------------------------------------------------------

function setPanelBusy(busy) {
  for (const b of S.nodes.panel.querySelectorAll("button, input, textarea, select")) {
    if (busy) { if (!b.disabled) { b.disabled = true; b.dataset.busy = "1"; } }
    else if (b.dataset.busy) { b.disabled = false; delete b.dataset.busy; }
  }
  S.nodes.panel.setAttribute("aria-busy", String(busy));
}

async function saveCut(n, fields, focusKey) {
  const mine = S;
  if (mine.saving || mine.editing) return;
  if (mine.conflict) { mine.ctx.toast("파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요", "error"); return; }
  mine.saving = true;
  setPanelBusy(true);
  try {
    const res = await mine.ctx.api("POST", "/api/conti/cut", { path: mine.data.path, n, fields, hash: mine.hash });
    if (!alive(mine)) return;
    mine.hash = res.hash;
    if (mine.actions) mine.actions.artStale(res.art_stale);        // drawings this edit made old: redrawn after a wait
    const d = mine.forms.get(n);
    if (d) { for (const k of Object.keys(fields)) delete d[k]; if (!Object.keys(d).length) mine.forms.delete(n); }
    if ("size" in fields) mine.scenes.delete(n);
    if ("sec" in fields) { const alt = mine.alts.get(n); if (alt) alt.max = null; }
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
    flushReload(mine);
  }
}

// ---- structure edits: insert, delete (with undo), move, duplicate -----------------------------
// Every edit is POST /api/conti/cuts with the last hash; the answer has the same shape as GET /api/conti, so the
// screen is redrawn from it. Drafts typed in the edit form and cached alternatives follow their cut by id
// because the cut numbers (n) shift when cuts come and go.

// Returns the answer, or null when nothing was changed (error shown, busy, or the screen was left).
async function editCuts(op) {
  const mine = S;
  if (mine.editing || mine.saving) return null;
  if (mine.conflict) { mine.ctx.toast("파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요", "error"); return null; }
  mine.editing = true;
  mine.nodes.list.setAttribute("aria-busy", "true");
  setPanelBusy(true);
  renderTools();
  try {
    const res = await mine.ctx.api("POST", "/api/conti/cuts", { path: mine.data.path, hash: mine.hash, ...op });
    if (!alive(mine)) return null;
    const before = mine.data;
    const selId = idOfN(before, mine.sel);
    remapByCutId(mine.forms, before, res);
    remapByCutId(mine.alts, before, res);
    mine.scenes.clear();
    mine.data = res;
    mine.hash = res.hash;
    if (mine.actions) mine.actions.artStale(res.art_stale);
    const keep = selId && res.cuts.find((c) => c.id === selId);
    mine.sel = keep ? keep.n : null;
    contiChanged(mine);
    return res;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return null;
    if (e.error === "conti_changed") {
      mine.conflict = true;
      renderConflict();
      const b = mine.nodes.conflict.querySelector("button");
      if (b) b.focus();
    } else {
      mine.ctx.toast(e.message, "error");
    }
    return null;
  } finally {
    if (alive(mine)) {
      mine.editing = false;
      mine.nodes.list.removeAttribute("aria-busy");
      renderTools();
      if (mine.nodes.panel) setPanelBusy(false);
      setTimeout(() => flushReload(mine), 0);    // after the caller drew the edit's answer
    }
  }
}

// Draw the answer and put focus on a cut (the new/moved one, or the one that took the deleted cut's place).
function showEdited(focusId, select, refreshFootage = true) {
  const cut = S.data.cuts.find((c) => c.id === focusId) || S.data.cuts[0];
  if (select && cut) S.sel = cut.n;
  renderHeader();
  renderBoardTools();
  renderCutList(cut ? `cut:${cut.id}` : null);
  renderPanel();
  if (cut) {
    const btn = navNode(cut.id);
    if (btn) {
      for (const b of S.nodes.board.querySelectorAll(".cut-item, .cb-open, .cut-more")) b.setAttribute("tabindex", "-1");
      btn.setAttribute("tabindex", "0");
      btn.closest(".cut-row, .cb-card").querySelector(".cut-more").setAttribute("tabindex", "0");
      btn.focus({ preventScroll: true });
      btn.scrollIntoView({ block: "nearest" });
    }
    announce(`장면 ${cut.n}`);
  }
  refreshHistory();
  if (refreshFootage && S.footage) S.footage.refresh();
}

async function insertCut(at, section) {
  const res = await editCuts({ op: "insert", at, section });
  if (res) showEdited(res.cut_id, true);
}

async function duplicateCut(id) {
  const res = await editCuts({ op: "duplicate", id });
  if (res) showEdited(res.cut_id, true);
}

async function moveCut(id, to, section) {
  const res = await editCuts({ op: "move", id, to, section });
  if (res) showEdited(res.cut_id, false);
}

function moveStep(id, dir) {
  const step = stepMove(S.data.cuts, id, dir);
  if (!step) return;
  moveCut(id, step.to, step.section).then(() => {
    const btn = S.nodes.tools && S.nodes.tools.querySelector(`[data-tool="${dir < 0 ? "up" : "down"}"]`);
    if (btn && !btn.disabled) btn.focus({ preventScroll: true });
    else { const row = navNode(id); if (row) row.focus({ preventScroll: true }); }
  });
}

// The fields a deleted cut needs to be put back. With its id the server keeps the same cut (drafts and
// alternatives keyed by id come back) unless that id is taken by then.
const RESTORE_KEYS = ["id", "size", "angle", "sec", "action", "guide", "line", "caption", "sfx", "ref_frame"];

async function deleteCut(id) {
  const cuts = S.data.cuts;
  const index = cuts.findIndex((c) => c.id === id);
  const cut = cuts[index];
  if (!cut) return;
  if (!canRemove(cuts, id)) { S.ctx.toast(LAST_CUT_TEXT, "error"); return; }
  const fields = {};
  for (const k of RESTORE_KEYS) if (cut[k] !== undefined && cut[k] !== null && cut[k] !== "") fields[k] = cut[k];
  const res = await editCuts({ op: "delete", id });
  if (!res) return;
  const next = res.cuts[Math.min(index, res.cuts.length - 1)];
  showEdited(next ? next.id : null, false);
  const prev = cuts[index - 1];
  const nextCut = cuts[index + 1];
  showUndo({ n: cut.n, index, section: cut.section, fields, prevId: prev ? prev.id : null, nextId: nextCut ? nextCut.id : null });
}

// 8-second toast with a 되돌리기 button. Stays while the pointer or focus is on it. The restored cut keeps its
// id (a new one only if that id is in use again), the same fields and the same place.
function clearUndo() {
  const u = S && S.undo;
  if (!u) return;
  S.undo = null;
  clearTimeout(u.timer);
  u.el.remove();
  document.removeEventListener("keydown", u.onKey);
}

// Where the deleted cut goes back, from the current list: right after its old previous neighbour, else right
// before its old next neighbour, else the old index clamped. The old section is only sent when it still fits
// between the neighbours (otherwise the server infers it).
function undoPlace(data, info) {
  const cuts = data.cuts;
  const pi = info.prevId ? cuts.findIndex((c) => c.id === info.prevId) : -1;
  const ni = info.nextId ? cuts.findIndex((c) => c.id === info.nextId) : -1;
  let at;
  if (pi >= 0) at = pi + 1;
  else if (ni >= 0) at = ni;
  else at = Math.min(info.index, cuts.length);
  const before = cuts[at - 1];
  const after = cuts[at];
  const fits = (!before || before.section <= info.section) && (!after || info.section <= after.section)
    && info.section < data.fm.script.length;
  return fits ? { at, section: info.section } : { at };
}

function showUndo(info) {
  clearUndo();
  const mine = S;
  const undo = h("button", { type: "button", class: "btn btn-sm", title: "Ctrl+Z" }, "되돌리기");
  const el = h("div", { class: "toast toast-undo", role: "status" }, h("span", {}, `${info.n}번 장면을 지웠습니다`), undo);
  const u = { el, timer: null, onKey: null, hold: 0 };
  const arm = () => { clearTimeout(u.timer); u.timer = setTimeout(() => { if (mine.undo === u) clearUndo(); }, UNDO_MS); };
  const hold = () => { clearTimeout(u.timer); };
  el.addEventListener("pointerenter", hold);
  el.addEventListener("pointerleave", arm);
  el.addEventListener("focusin", hold);
  el.addEventListener("focusout", arm);
  const run = async () => {
    if (mine.undo !== u || mine.editing || mine.saving) return;
    const place = undoPlace(mine.data, info);
    hold();
    const res = await editCuts({ op: "insert", ...place, cut: info.fields });
    if (!alive(mine)) return;
    if (res) { if (mine.undo === u) clearUndo(); showEdited(res.cut_id, true); }
    else if (mine.undo === u) arm();
  };
  undo.addEventListener("click", run);
  u.onKey = (e) => {
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); run(); }
  };
  document.addEventListener("keydown", u.onKey);
  S.undo = u;
  (document.getElementById("toasts") || document.body).append(el);
  arm();
}

// ---- drag and drop ----------------------------------------------------------------------------
// Only the ⋮⋮ handle starts a drag. A row takes the drop above or below its middle; a section header takes
// it as "first cut of that section"; an insert slot takes it where it sits. Keyboard and touch users have
// 위로 / 아래로 and the ⋯ menu instead.

function dropPlace(target, y) {
  const row = target.closest(".cut-row");
  if (row) {
    const cuts = S.data.cuts;
    const i = cuts.findIndex((c) => c.id === row.dataset.id);
    if (i < 0) return null;
    const r = row.getBoundingClientRect();
    const after = y > r.top + r.height / 2;
    return { el: row, cls: after ? "drop-after" : "drop-before", gap: i + (after ? 1 : 0), section: cuts[i].section };
  }
  const gap = target.closest(".cut-gap");
  if (gap) return { el: gap, cls: "drop-here", gap: Number(gap.dataset.gap), section: Number(gap.dataset.section) };
  const head = target.closest(".sec-head");
  if (head) return { el: head, cls: "drop-here", gap: Number(head.dataset.gap), section: Number(head.dataset.section) };
  return null;
}

function clearDropMarks() {
  for (const el of S.nodes.list.querySelectorAll(".drop-before, .drop-after, .drop-here")) {
    el.classList.remove("drop-before", "drop-after", "drop-here");
  }
}

function wireDrag(list) {
  list.addEventListener("dragstart", (e) => {
    const handle = e.target.closest && e.target.closest(".cut-handle");
    const row = handle && handle.closest(".cut-row");
    if (!row || S.editing || S.saving) { e.preventDefault(); return; }
    closeMenu(false);
    S.drag = { id: row.dataset.id };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", row.dataset.id);
    e.dataTransfer.setDragImage(row, 16, 16);
    setTimeout(() => row.classList.add("is-dragging"), 0);
  });
  list.addEventListener("dragover", (e) => {
    if (!S.drag) return;
    const place = dropPlace(e.target, e.clientY);
    if (!place) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearDropMarks();
    place.el.classList.add(place.cls);
    S.drag.place = place;
  });
  list.addEventListener("dragleave", (e) => {
    if (!list.contains(e.relatedTarget)) clearDropMarks();
  });
  list.addEventListener("drop", (e) => {
    if (!S.drag) return;
    e.preventDefault();
    const place = dropPlace(e.target, e.clientY) || S.drag.place;
    const id = S.drag.id;
    endDrag();
    if (place) dropCut(id, place);
  });
  list.addEventListener("dragend", endDrag);
}

function endDrag() {
  if (!S || !S.nodes.list) return;
  S.drag = null;
  clearDropMarks();
  for (const el of S.nodes.list.querySelectorAll(".is-dragging")) el.classList.remove("is-dragging");
}

function dropCut(id, place) {
  const cuts = S.data.cuts;
  const target = dropTarget(cuts, id, place.gap, place.section);
  const from = cuts.findIndex((c) => c.id === id);
  if (!target || from < 0) return;
  if (target.to === from && target.section === cuts[from].section) return;
  if (target.section !== cuts[from].section && !canRemove(cuts, id)) { S.ctx.toast(LAST_CUT_TEXT, "error"); return; }
  moveCut(id, target.to, target.section);
}

// ---- history panel ----------------------------------------------------------------------------
// 이력 button in the header opens a panel on the right (a full-width overlay on narrow screens): name the current
// state, pick up to two versions, compare (change list, or two cut lists side by side) and restore. Esc closes
// it and gives the focus back to the button. Server calls: GET /api/conti/history (+ /diff), POST /name, /restore.

const NAME_MAX = 30;
const NAME_IDEAS = ["기획 완료", "촬영 후", "편집 중"];
const RESTORE_NOTE = "지금 상태도 이력에 남아 있어 다시 되돌릴 수 있어요";
const BUSY_TEXT = "저장 중이에요. 잠시 뒤 다시 눌러 주세요";
const STATUS_TEXT = { same: "", changed: "바뀜", moved: "옮김", removed: "빠짐", added: "새 장면" };

function freshHist() {
  return { open: false, versions: null, error: null, pick: [], view: null, mode: "list", confirm: null, busy: false, loadingDiff: false, seq: 0, listSeq: 0 };
}

function histBtn() {
  return S.nodes.header && S.nodes.header.querySelector("[data-hist-btn]");
}

function historyButton() {
  const btn = h("button", {
    type: "button", class: "btn", "data-hist-btn": "1", "aria-expanded": String(S.hist.open), "aria-controls": "hist-panel",
  }, "이력");
  btn.addEventListener("click", () => (S.hist.open ? closeHistory(true) : openHistory()));
  return btn;
}

function mountHistory() {
  const old = S.nodes.hist;
  if (old) old.remove();
  S.nodes.hist = null;
  if (!S.hist.open) { syncInert(); return; }
  const panel = h("aside", { class: "hist-panel", id: "hist-panel", "aria-labelledby": "hist-title" });
  S.nodes.hist = panel;
  S.el.append(panel);
  renderHistory();
}

// Esc closes the panel from anywhere (a menu or field that already used the key calls preventDefault first).
function onHistKey(e) {
  if (e.key === "Escape" && !e.defaultPrevented && S && S.hist.open) { e.preventDefault(); closeHistory(true); }
}

// Narrow screens: the panel covers the page, so the page behind it must not take Tab or screen reader focus.
function syncInert() {
  const panel = S.nodes.hist;
  const on = !!panel && window.matchMedia(NARROW).matches;
  for (const el of S.el.children) {
    if (el === panel || el === S.live) continue;
    if (on) el.setAttribute("inert", ""); else el.removeAttribute("inert");
  }
}

function renderHistory() {
  const panel = S.nodes.hist;
  if (!panel) return;
  const H = S.hist;
  const close = h("button", { type: "button", class: "btn btn-sm", "aria-label": "이력 닫기" }, "닫기");
  close.addEventListener("click", () => closeHistory(true));
  panel.classList.toggle("is-wide", !!H.view && H.mode === "side");
  panel.setAttribute("aria-busy", String(H.busy || H.loadingDiff));
  const kids = [h("div", { class: "hist-head" }, h("h2", { id: "hist-title", tabindex: "-1" }, "이력"), close)];
  if (H.view) kids.push(compareView());
  else kids.push(nameBox(), versionList());
  const focusId = panel.contains(document.activeElement) ? document.activeElement.id : "";
  panel.replaceChildren(...kids);
  const again = focusId && panel.querySelector(`#${CSS.escape(focusId)}`);
  if (again && !again.disabled) again.focus({ preventScroll: true });   // a redraw must not drop the focus
  syncInert();
}

async function openHistory() {
  S.hist.open = true;
  document.addEventListener("keydown", onHistKey);
  const btn = histBtn();
  if (btn) btn.setAttribute("aria-expanded", "true");
  mountHistory();
  const t = S.nodes.hist.querySelector("#hist-title");
  if (t) t.focus({ preventScroll: true });
  await loadHistory();
}

function closeHistory(restoreFocus) {
  if (!S || !S.hist.open) return;
  document.removeEventListener("keydown", onHistKey);
  S.hist = freshHist();
  mountHistory();
  const btn = histBtn();
  if (btn) { btn.setAttribute("aria-expanded", "false"); if (restoreFocus) btn.focus(); }
}

async function loadHistory() {
  const mine = S;
  const H = mine.hist;
  const tok = ++H.listSeq;
  const stale = () => !alive(mine) || mine.hist !== H || !H.open || H.listSeq !== tok;
  try {
    const res = await mine.ctx.api("GET", `/api/conti/history?path=${encodeURIComponent(mine.data.path)}`);
    if (stale()) return;
    mine.hist.versions = res.versions;
    mine.hist.error = null;
    mine.hist.pick = mine.hist.pick.filter((n) => res.versions.some((v) => v.n === n));
  } catch (e) {
    if (e.error === "aborted" || stale()) return;
    mine.hist.error = e.message;
  }
  if (mine.hist.view) return;                       // the compare view does not list versions; keep what is on screen
  renderHistory();
}

// After an edit elsewhere on the screen: the newest version may have changed, and "지금과 비교" is stale.
function refreshHistory() {
  if (!S || !S.hist.open) return;
  loadHistory();
  const v = S.hist.view;
  if (v && v.b === null) loadDiff(v.a, null, true);   // keeps the mode (list or side by side)
}

function nameBox() {
  const H = S.hist;
  const input = h("input", { type: "text", id: "hist-name", class: "hist-name", maxlength: String(NAME_MAX), autocomplete: "off", placeholder: "예: 촬영 후" });
  const save = h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "이름 붙이기");
  const note = h("p", { class: "error hist-err", role: "alert" });
  const ideas = h("div", { class: "hist-ideas", role: "group", "aria-label": "이름 후보" },
    ...NAME_IDEAS.map((t) => {
      const b = h("button", { type: "button", class: "btn btn-sm" }, t);
      b.addEventListener("click", () => { input.value = t; input.focus(); });
      return b;
    }));
  const form = h("form", { class: "hist-form" },
    h("label", { for: "hist-name", class: "hist-label" }, "지금 상태에 이름 붙이기"),
    ideas,
    h("div", { class: "hist-row" }, input, save), note);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) { note.textContent = "이름을 적어 주세요"; input.setAttribute("aria-invalid", "true"); input.focus(); return; }
    note.textContent = "";
    input.removeAttribute("aria-invalid");
    await nameNow(name, save, note);
  });
  save.disabled = H.busy;
  return form;
}

async function nameNow(name, btn, note) {
  const mine = S;
  if (mine.hist.busy) return;
  if (mine.editing || mine.saving) { note.textContent = BUSY_TEXT; return; }
  if (mine.conflict) { note.textContent = "파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요"; return; }
  mine.hist.busy = true;
  btn.disabled = true;
  try {
    const res = await mine.ctx.api("POST", "/api/conti/history/name", { path: mine.data.path, name, hash: mine.hash });
    if (!alive(mine)) return;
    mine.hash = res.hash;
    mine.ctx.toast(`「${name}」 이름을 붙였어요`, "info");
    announce(`이력에 ${name} 버전을 남겼습니다`);
    mine.hist.busy = false;
    await loadHistory();
    const first = mine.nodes.hist && mine.nodes.hist.querySelector("#hist-name");
    if (first) first.focus({ preventScroll: true });
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    const live = (mine.nodes.hist && mine.nodes.hist.querySelector(".hist-form .hist-err")) || note;   // the panel may have been redrawn
    if (e.error === "conti_changed") {
      mine.conflict = true;
      renderConflict();
      live.textContent = STALE_TEXT;
    } else {
      live.textContent = e.message;
    }
  } finally {
    if (alive(mine)) { mine.hist.busy = false; btn.disabled = false; }
  }
}

function versionList() {
  const H = S.hist;
  if (H.error) {
    const retry = h("button", { type: "button", class: "btn btn-sm" }, "다시 시도");
    retry.addEventListener("click", loadHistory);
    return h("div", { class: "hist-list-box" }, h("p", { class: "error", role: "alert" }, H.error), retry);
  }
  if (!H.versions) return h("p", { class: "muted", role: "status" }, "불러오는 중…");
  if (!H.versions.length) return h("p", { class: "muted" }, "아직 남은 이력이 없어요. 편집하면 자동으로 쌓입니다.");
  const full = H.pick.length >= 2;
  const items = H.versions.map((v) => {
    const checked = H.pick.includes(v.n);
    const box = h("input", { type: "checkbox", id: `hist-pick-${v.n}`, disabled: !checked && full });
    box.checked = checked;
    box.addEventListener("change", () => {
      H.pick = box.checked ? [...H.pick, v.n] : H.pick.filter((n) => n !== v.n);
      H.confirm = null;
      renderHistory();
      const again = S.nodes.hist.querySelector(`#hist-pick-${v.n}`);
      if (again) again.focus();
    });
    const s = v.summary || {};
    const meta = [`장면 ${s.cuts}개`, fmtClock(s.total_sec), s.changed ? `바뀐 장면 ${s.changed}개` : "바뀐 장면 없음"].join(" · ");
    const label = h("label", { for: `hist-pick-${v.n}`, class: "hist-item-label" },
      h("span", { class: "hist-when num" }, fmtWhen(v.at)),
      h("span", { class: "hist-kind" + (v.kind === "named" ? " is-named" : "") }, versionLabel(v)),
      h("span", { class: "hist-meta muted" }, meta));
    return h("li", { class: "hist-item" + (checked ? " is-picked" : "") }, box, label);
  });
  const list = h("ul", { class: "hist-list", "aria-label": "이력 목록, 최신순" }, ...items);
  return h("div", { class: "hist-list-box" },
    h("p", { class: "muted hist-hint" }, full ? "둘을 골랐어요. 다른 버전을 고르려면 하나를 해제하세요" : "하나를 고르면 지금과 비교하거나 되돌릴 수 있고, 둘을 고르면 서로 비교해요"),
    pickBar(), list);
}

function pickBar() {
  const H = S.hist;
  const bar = h("div", { class: "hist-bar", role: "group", "aria-label": "고른 버전으로 할 일" });
  if (!H.pick.length) { bar.hidden = true; return bar; }
  if (H.pick.length === 2) {
    const both = h("button", { type: "button", class: "btn btn-sm btn-primary", disabled: H.loadingDiff }, "둘 비교");
    both.addEventListener("click", () => { const [a, b] = [...H.pick].sort((x, y) => x - y); loadDiff(a, b); });
    bar.append(both);
    return bar;
  }
  const n = H.pick[0];
  const cmp = h("button", { type: "button", class: "btn btn-sm", disabled: H.loadingDiff }, "지금과 비교");
  cmp.addEventListener("click", () => loadDiff(n, null));
  const rest = h("button", { type: "button", class: "btn btn-sm", "data-restore": "1" }, "이 버전으로 되돌리기");
  rest.addEventListener("click", () => {
    H.confirm = n;
    renderHistory();
    const ok = S.nodes.hist.querySelector("[data-restore-ok]");
    if (ok) ok.focus();
  });
  bar.append(cmp, rest);
  if (H.confirm === n) bar.append(restoreConfirm(n));
  return bar;
}

function restoreConfirm(n) {
  const H = S.hist;
  const ok = h("button", { type: "button", class: "btn btn-sm btn-primary", "data-restore-ok": "1" }, "되돌리기");
  ok.id = "hist-restore-ok";
  const no = h("button", { type: "button", class: "btn btn-sm" }, "취소");
  const note = h("p", { class: "error hist-err", role: "alert" });
  ok.addEventListener("click", () => restoreTo(n, [ok, no], note));
  no.addEventListener("click", () => {
    H.confirm = null;
    renderHistory();
    const b = S.nodes.hist.querySelector("[data-restore]");
    if (b) b.focus();
  });
  const unsaved = unsavedCuts();
  return h("div", { class: "hist-confirm" },
    h("p", { class: "hist-confirm-text" }, RESTORE_NOTE),
    unsaved.length ? h("p", { class: "hist-confirm-text", "data-unsaved": "1" }, `저장하지 않은 입력(장면 ${unsaved.join(", ")})은 사라집니다`) : null,
    h("div", { class: "btn-row" }, ok, no), note);
}

async function restoreTo(n, buttons, note) {
  const mine = S;
  if (mine.hist.busy) return;
  if (mine.editing || mine.saving) { note.textContent = BUSY_TEXT; return; }
  if (mine.conflict) { note.textContent = "파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요"; return; }
  mine.hist.busy = true;
  for (const b of buttons) b.disabled = true;
  buttons[0].textContent = "되돌리는 중…";
  try {
    const res = await mine.ctx.api("POST", "/api/conti/history/restore", { path: mine.data.path, n, hash: mine.hash });
    if (!alive(mine)) return;
    mine.hash = res.hash;
    mine.forms.clear();
    mine.alts.clear();
    mine.scenes.clear();
    clearUndo();
    mine.sel = null;
    mine.hist.confirm = null;
    mine.hist.pick = [];
    mine.hist.view = null;
    mine.ctx.toast("되돌렸어요 · 이전 상태도 이력에 남아 있어요", "info");
    announce("이력 버전으로 되돌렸습니다");
    await loadEditor(true);
    if (!alive(mine) || !mine.hist.open) return;
    mine.hist.busy = false;
    await loadHistory();
    const t = mine.nodes.hist && mine.nodes.hist.querySelector("#hist-title");
    if (t) t.focus({ preventScroll: true });
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    for (const b of buttons) b.disabled = false;
    buttons[0].textContent = "되돌리기";
    const live = (mine.nodes.hist && mine.nodes.hist.querySelector(".hist-confirm .hist-err")) || note;
    if (e.error === "conti_changed") {
      mine.conflict = true;
      renderConflict();
      live.textContent = STALE_TEXT;
    } else {
      live.textContent = e.message;
    }
  } finally {
    if (alive(mine)) mine.hist.busy = false;
  }
}

async function loadDiff(a, b, quiet = false) {
  const mine = S;
  const H = mine.hist;
  const tok = ++H.seq;
  const stale = () => !alive(mine) || mine.hist !== H || !H.open || H.seq !== tok;
  H.loadingDiff = true;
  if (!quiet) renderHistory();                      // shows aria-busy and the disabled buttons
  try {
    const q = `path=${encodeURIComponent(mine.data.path)}&a=${a}` + (b === null ? "" : `&b=${b}`);
    const res = await mine.ctx.api("GET", `/api/conti/history/diff?${q}`);
    if (stale()) return;
    H.view = { a, b, diff: res };
    if (!quiet) H.mode = "list";
  } catch (e) {
    if (e.error === "aborted" || stale()) return;
    H.loadingDiff = false;
    mine.ctx.toast(e.message, "error");
    renderHistory();
    return;
  }
  H.loadingDiff = false;
  renderHistory();
  if (!quiet) {
    const t = mine.nodes.hist.querySelector("#hist-title");
    if (t) t.focus({ preventScroll: true });
  }
}

function sideName(n) {
  if (n === null) return "지금";
  const v = (S.hist.versions || []).find((x) => x.n === n);
  return v ? `${fmtWhen(v.at)} ${versionLabel(v)}` : `${n}번 버전`;
}

function compareView() {
  const H = S.hist;
  const { a, b, diff } = H.view;
  const back = h("button", { type: "button", class: "btn btn-sm" }, "← 이력 목록");
  back.addEventListener("click", () => {
    H.seq += 1;                                     // a diff still on its way must not bring the view back
    H.loadingDiff = false;
    H.view = null;
    renderHistory();
    const box = S.nodes.hist.querySelector(`#hist-pick-${a}`);
    if (box) box.focus();
  });
  const mode = h("button", { type: "button", class: "btn btn-sm", "aria-pressed": String(H.mode === "side") }, "나란히 보기");
  mode.addEventListener("click", () => {
    H.mode = H.mode === "side" ? "list" : "side";
    renderHistory();
    const m = S.nodes.hist.querySelector("[aria-pressed]");
    if (m) m.focus();
  });
  // A new cut's section name is only known for "지금" (the labels come from the open conti).
  const sectionName = (change) => {
    if (b !== null) return "";
    const cut = diff.right.find((c) => c.n === change.n_to);
    const sec = cut && S.data.fm.script[cut.section];
    return sec ? sectionLabelName(sec.label) : "";
  };
  const kids = [
    h("div", { class: "hist-cmp-bar" }, back, mode),
    h("p", { class: "hist-cmp-title" }, h("strong", {}, sideName(a)), " → ", h("strong", {}, sideName(b))),
  ];
  if (H.mode === "side") kids.push(sideBySide(diff));
  else if (!diff.changes.length) kids.push(h("p", { class: "muted", role: "status" }, "바뀐 점이 없어요"));
  else {
    kids.push(h("ul", { class: "hist-changes", "aria-label": "바뀐 점" },
      ...collapseChanges(diff.changes).map((c) => h("li", { class: `hist-change is-${c.kind}` }, changeText(c, c.kind === "added" ? sectionName(c) : "")))));
  }
  return h("div", { class: "hist-cmp" }, ...kids);
}

function cellFor(cut, status, side, fields) {
  if (!cut) {
    return h("div", { class: "hist-cell is-empty" }, h("span", { class: "muted" }, side === "l" ? "(이전엔 없음)" : "(없어짐)"));
  }
  let tag = "";
  if (side === "l" && status === "removed") tag = "빠짐";
  else if (side === "r" && status !== "same") tag = STATUS_TEXT[status];
  const mark = (f, text) => h("span", { class: fields.includes(f) && side === "r" ? "is-diff" : "" }, text);
  return h("div", { class: `hist-cell is-${status}` },
    h("div", { class: "hist-cell-top" },
      h("span", { class: "num" }, `#${cut.n}`),
      mark("size", cut.size || ""),
      mark("sec", fmtSec(cut.sec)),
      tag ? h("span", { class: "hist-tag" }, tag) : null),
    h("div", { class: cut.line ? "" : "muted" }, mark("line", cut.line || "(대사 없음)")),
    cut.action ? h("div", { class: "hist-sub muted" }, mark("action", cut.action)) : null,
    cut.caption ? h("div", { class: "hist-sub muted" }, mark("caption", `자막 ${cut.caption}`)) : null);
}

function sideBySide(diff) {
  const rows = alignCuts(diff.left, diff.right, diff.changes);
  if (!rows.length) return h("p", { class: "muted" }, "장면이 없어요");
  return h("div", { class: "hist-side" },
    h("div", { class: "hist-side-head", "aria-hidden": "true" }, h("span", {}, "이전"), h("span", {}, "이후")),
    h("ol", { class: "hist-side-list", "aria-label": "장면 나란히 보기" },
      ...rows.map((r) => {
        const word = STATUS_TEXT[r.status];
        const what = r.fields.length ? `${word}: ${r.fields.map(fieldText).join(", ")}. ` : `${word}. `;
        return h("li", { class: `hist-pair is-${r.status}` },
          word ? h("span", { class: "sr-only" }, what) : null,
          cellFor(r.l, r.status, "l", r.fields), cellFor(r.r, r.status, "r", r.fields));
      })));
}

// ---- page contract ------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  S = freshState(el, params, ctx);
  const path = params.path;
  if (path && isContiPath(path)) await loadEditor(false);
  else await loadList();
}

// Cut numbers with edits typed but not saved (kept per cut in S.forms until save/reset).
function unsavedCuts() {
  return S ? [...S.forms].filter(([, d]) => Object.keys(d).length).map(([k]) => k).sort((a, b) => a - b) : [];
}

// Named so leaving (another tab, another screen) can ask first.
export function unsavedNote() {
  const cuts = unsavedCuts();
  return cuts.length ? `저장하지 않은 입력(장면 ${cuts.join(", ")})` : null;
}

export function unmount() {
  if (S) clearTimeout(S.searchTimer);
  if (S && S.footage) S.footage.unmount();
  if (S && S.actions) S.actions.unmount();
  stopPolling();
  closeMenu(false);
  clearUndo();
  document.removeEventListener("keydown", onHistKey);
  S = null;
}
