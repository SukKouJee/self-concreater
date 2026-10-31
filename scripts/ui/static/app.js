// Page contract (pages/<name>.js):
//   export function mount(el, params, ctx)   // may be async
//   export function unmount()                // optional, called before the next route
//   export function onCategory(category)     // optional; called when the category changes while this page is
//                                            // open. Non-home pages are NOT remounted (keeps in-progress state).
//   export function update(params)           // optional; a new route of the same page: return true when the page
//                                            // took it in place (no unmount/remount, ctx stays current). The router
//                                            // does not re-read the sidebar then: the page shares its /api/status
//                                            // answer through ctx.statusSeen
//   export function unsavedNote()            // optional; text naming unsaved input ("저장하지 않은 입력(컷 2)") or
//                                            // null. The router asks before leaving (hash put back meanwhile).
//   export function onStay()                 // optional; the user stayed after the leave question (repaint state)
// ctx: { category, setCategory(name), refreshCategories(), toast(msg, kind, action?), api(method, path, body),
//        signal (AbortSignal, aborted when the user navigates away), isCurrent() (false once navigated away),
//        confirmLeave(note, go, stayed) (the leave question: go() when the user leaves, stayed() when they stay),
//        navigate(hash, { replace, force }) (replace: no new history entry; force: no leave question),
//        jobStarted(kind) (a long job started: the header status chip asks for it now; api() answers that carry
//        a job reach the chip by themselves, so this is only needed when a page learns of a job another way),
//        statusSeen(category, answer) (a page read GET /api/status: the sidebar's videos and ref count follow it) }
//   toast action: { href, label } adds a link ("결과 보기") to the toast.
// Rule: after EVERY await in mount/handlers, `if (!ctx.isCurrent()) return;` before touching the DOM.
import { api, showBanner } from "/static/api.js";
import { h } from "/static/dom.js";
import { loadIsAdmin } from "/static/admin-utils.js";
import { loadFeatureInfo, watchFeatures } from "/static/features.js";
import { createHelpPanel, helpPage } from "/static/help-panel.js";
import { createStatusChip } from "/static/status-chip.js";
import { legacyRoute, parseVideoHash } from "/static/video-utils.js";
import {
  PHONE_TABS, SIDEBAR, isActive, isVideoActive, phoneTab, sidebarVideos, subnavItems, videoLinks, videoProgress,
} from "/static/menu-utils.js";

const CATEGORY_KEY = "ytc.category";
const SIDEBAR_VIDEOS = 5;   // unfinished videos listed in the sidebar; the rest sit behind "전체 N편"

// Every screen with its name (browser tab title). The help chatbot gets the same list (api_help.MENU).
// The sidebar and the phone tabs are defined in menu-utils.js.
// Order follows the sidebar groups (영감 / 내 영상 / 성장). 기획안·콘티·진단 are the tabs of one video
// (#/video/<id>/<tab>); their old routes redirect (#/draft, #/conti, #/diagnose -> 내 영상 or that video's tab).
const SCREENS = [
  { name: "홈", route: "#/home" },
  { name: "레퍼런스", route: "#/refs" },
  { name: "공용 라이브러리", route: "#/library" },
  { name: "유형", route: "#/typify" },
  { name: "모티브", route: "#/motif" },
  { name: "내 영상", route: "#/videos" },
  { name: "기획안", route: "#/draft" },
  { name: "콘티", route: "#/conti" },
  { name: "진단", route: "#/diagnose" },
  { name: "오늘 미션", route: "#/mission" },
  { name: "주간 리포트", route: "#/report" },
  { name: "가이드", route: "#/guide" },
];
// One video's workspace (not in the help chatbot's menu). The video page names the browser tab after the video
// once it is loaded.
const VIDEO_SCREENS = [
  { route: "#/video", name: "영상" },
];
// Server edition, admins only (not in the help chatbot's menu either): reached from the header / help menu.
const ADMIN_SCREENS = [
  { route: "#/admin", name: "관리" },
];

const $ = (id) => document.getElementById(id);
let helpPanel = null;
let statusChip = null;
const state = {
  categories: [], current: null, currentPage: null, navToken: 0, abort: null,
  // sidebar data for the current category: { category, videos, ref, archetypes, motifs } (missing = unknown)
  side: null, sideSeq: 0,
  hash: null,         // the hash the current page was routed for (put back while the leave question is open)
  at: 0,              // history stamp (history.state.ytcAt) of that entry: tells back from forward from a new entry
  restoring: false,   // the router itself is moving back to state.hash (its hashchange is not a route)
  leaveOk: false,     // the user chose to leave despite unsaved input: the next route skips the question
};

// ---- ctx ---------------------------------------------------------------

function readStoredCategory() {
  try { return localStorage.getItem(CATEGORY_KEY); } catch { return null; }
}
function storeCategory(v) {
  try { localStorage.setItem(CATEGORY_KEY, v); } catch { /* storage unavailable */ }
}

// action { href, label }: a link to the result ("결과 보기"). A toast with a link stays longer and does not go away
// while the pointer or the keyboard focus is on it.
function toast(msg, kind = "info", action = null) {
  const link = action && action.href ? h("a", { class: "toast-link", href: action.href }, action.label || "보기") : null;
  const t = h("div", { class: `toast toast-${kind}${link ? " toast-action" : ""}` }, h("span", {}, msg), link);
  $("toasts").append(t);
  const ms = link ? 10000 : kind === "error" ? 7000 : 3500;
  let timer = setTimeout(() => t.remove(), ms);
  if (link) {
    link.addEventListener("click", () => t.remove());
    const hold = () => clearTimeout(timer);
    const resume = () => { clearTimeout(timer); timer = setTimeout(() => t.remove(), 4000); };
    t.addEventListener("mouseenter", hold);
    t.addEventListener("focusin", hold);
    t.addEventListener("mouseleave", resume);
    t.addEventListener("focusout", resume);
  }
}

// Shared (app-level) context. Pages receive a per-mount view of it, see pageCtx().
const ctx = {
  category: null,
  api,
  toast,
  setCategory(name) {
    ctx.category = name || null;
    if (name) storeCategory(name);
    updateTopbar();
    if (helpPanel) helpPanel.onCategory(ctx.category);
    if (state.currentPage === "home") {
      route();
    } else {
      refreshSidebar();
      if (state.current && typeof state.current.onCategory === "function") state.current.onCategory(ctx.category);
    }
  },
  refreshCategories,
};

// Per-mount ctx: adds liveness guards so late async work never touches a page the user left.
function pageCtx(token, signal) {
  return {
    get category() { return ctx.category; },
    setCategory: ctx.setCategory,
    refreshCategories,
    toast,
    api: (method, path, body) => api(method, path, body, signal),
    signal,
    isCurrent: () => token === state.navToken,
    confirmLeave,
    navigate,
    jobStarted: (kind) => { if (statusChip) statusChip.jobStarted(kind); },
    statusSeen: adoptStatus,
  };
}

async function refreshCategories() {
  try {
    const data = await api("GET", "/api/categories");
    state.categories = data.categories || [];
  } catch {
    state.categories = [];
  }
  const names = state.categories.map((c) => c.category);
  const stored = readStoredCategory();
  ctx.category = names.includes(ctx.category) ? ctx.category
    : names.includes(stored) ? stored : (names[0] || null);
  updateTopbar();
}

// ---- icons -----------------------------------------------------------------

// Line icons (24x24, stroke). Built with createElementNS: h() makes HTML elements only.
const ICONS = {
  home: ["M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"],
  spark: ["M9 18h6M10 21h4", "M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3z"],
  film: ["M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z", "M10 9l5 3-5 3z"],
  target: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 12h.01"],
};
const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function svgIcon(name, cls) {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: cls });
  for (const d of ICONS[name]) svg.append(svgEl("path", { d }));
  return svg;
}

// Logo: red rounded screen with a play mark (mockup), colors from the tokens via CSS classes.
function logo() {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "logo" });
  svg.append(svgEl("rect", { x: "2", y: "5", width: "20", height: "14", rx: "4", class: "logo-box" }),
    svgEl("path", { d: "M10 9l5 3-5 3z", class: "logo-play" }));
  return svg;
}

// ---- sidebar -----------------------------------------------------------------

// Sidebar data for the current category from existing APIs: videos and the ref count (/api/status), the saved
// archetypes (/api/typify) and motif cards (/api/motif). A failed call only leaves its number out.
async function refreshSidebar() {
  const seq = ++state.sideSeq;
  const category = ctx.category;
  if (!category) {
    state.side = null;
    renderSidebar();
    return;
  }
  const q = `?category=${encodeURIComponent(category)}`;
  const [st, ty, mo] = await Promise.allSettled([
    api("GET", `/api/status${q}`), api("GET", `/api/typify${q}`), api("GET", `/api/motif${q}`),
  ]);
  if (seq !== state.sideSeq) return;
  const cat = st.status === "fulfilled" ? (st.value.categories || []).find((c) => c.category === category) : null;
  const applied = ty.status === "fulfilled" ? ty.value.applied : undefined;
  state.side = {
    category,
    videos: cat ? cat.videos || [] : null,
    ref: cat ? cat.ref : undefined,
    archetypes: applied === undefined ? undefined : applied ? (applied.archetypes || []).length : 0,
    motifs: mo.status === "fulfilled" && Array.isArray(mo.value.cards) ? mo.value.cards.length : undefined,
  };
  renderSidebar();
}

// A page's own /api/status answer (the video page reads it for its header): the sidebar's videos and ref count take
// it without a second read. Archetype and motif counts stay. An older answer than a sidebar read in flight is fine:
// that read lands after it.
function adoptStatus(category, answer) {
  if (!answer || !state.side || state.side.category !== category || category !== ctx.category) return;
  const cat = (answer.categories || []).find((c) => c.category === category);
  if (!cat) return;
  state.side = { ...state.side, videos: cat.videos || [], ref: cat.ref };
  renderSidebar();
}

function menuLink(it) {
  const side = state.side || {};
  const n = it.count ? side[it.count] : undefined;
  const label = typeof n === "number" ? `${it.label} ${n}개` : it.label;
  return h("a", { class: "menu-link" + (it.action ? " menu-new" : ""), href: it.href, "data-action": it.action ? "1" : null,
    "data-href": it.href, "aria-label": typeof n === "number" ? label : null },
  h("span", {}, it.label), typeof n === "number" ? h("span", { class: "menu-count", "aria-hidden": "true" }, String(n)) : null);
}

function videoRow(v) {
  const { pct, stage } = videoProgress(v);
  const fill = h("span", { style: `width: ${pct}%` });
  return h("li", {}, h("a", { class: "menu-link vid-link", href: videoLinks(v.path).view, "data-video": v.path },
    h("span", { class: "vid-name" }, v.title),
    h("span", { class: "vid-prog" },
      h("span", { class: "vid-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": String(pct), "aria-valuetext": `${stage} 단계 · ${pct}%`, "aria-label": "진행" }, fill),
      h("span", { class: "vid-stage" }, stage))));
}

function videoRows() {
  const side = state.side;
  if (!ctx.category) return [h("li", { class: "menu-note" }, "카테고리를 먼저 만들어요")];
  if (!side || side.videos === null) return [];
  const { shown, total } = sidebarVideos(side.videos, SIDEBAR_VIDEOS);
  const rows = shown.map(videoRow);
  if (shown.length === 0) rows.push(h("li", { class: "menu-note" }, total ? "만드는 중인 영상이 없어요" : "아직 영상이 없어요"));
  if (total > shown.length) rows.push(h("li", {}, h("a", { class: "menu-link menu-all", href: "#/videos", "data-href": "#/videos", "data-exact": "1" }, `전체 ${total}편`)));
  return rows;
}

// Key of a sidebar link (video path or href) so focus can return to the same link after a re-render.
function linkKey(a) {
  return a && a.classList && a.classList.contains("menu-link") ? (a.dataset.video ? `v:${a.dataset.video}` : `h:${a.dataset.href}`) : null;
}

function renderSidebar() {
  const nav = $("menu");
  if (!nav) return;
  const focused = nav.contains(document.activeElement) ? linkKey(document.activeElement) : null;
  nav.replaceChildren(...SIDEBAR.map((g, i) => {
    const id = `menu-g${i}`;
    const ul = h("ul", { class: "menu-list", "aria-labelledby": id });
    if (g.videos) ul.append(...videoRows());
    for (const it of g.items) ul.append(h("li", {}, menuLink(it)));
    return h("div", { class: "menu-group" }, h("h2", { class: "menu-label", id }, g.group), ul);
  }));
  markActive();
  if (focused) {
    const again = [...nav.querySelectorAll("a.menu-link")].find((a) => linkKey(a) === focused);
    if (again) again.focus();
  }
}

function renderTabbar() {
  $("tabbar").replaceChildren(...PHONE_TABS.map((t) => h("a", { class: "tab", href: t.href, "data-tab": t.key },
    svgIcon(t.icon, "tab-icon"), h("span", {}, t.label))));
}

// The 공용 라이브러리 tab is the one subnav link that a disabled feature takes away.
function featureOfLink(item) {
  return item.href === "#/refs?tab=library" ? "library" : null;
}

// Screens that belong to a feature: reached directly (#/library, a help-panel or page link) while that feature is
// disabled, they show the edition's one-line notice instead of the page.
const PAGE_FEATURES = { library: "library" };
let disabledFeatures = {};

function disabledNote(page) {
  const feature = PAGE_FEATURES[page];
  return feature && Object.hasOwn(disabledFeatures, feature) ? { feature, text: disabledFeatures[feature] } : null;
}

function renderSubnav(hash) {
  const nav = $("subnav");
  const items = subnavItems(hash);
  nav.hidden = items.length === 0;
  nav.classList.toggle("subnav-phone", items.length > 0 && items.every((it) => it.phoneOnly));
  nav.replaceChildren(...items.map((it) => h("a", {
    class: "subnav-link" + (it.phoneOnly ? " subnav-phone-item" : ""), href: it.href,
    "aria-current": it.current ? "page" : null, "data-feature": featureOfLink(it),
  }, it.label)));
}

function markActive() {
  const hash = location.hash || "#/home";
  for (const a of $("menu").querySelectorAll("a.menu-link")) {
    // action links ("+ 새 영상 시작" -> #/motif) are never current: the 모티브 item is, so one link per screen
    const on = a.dataset.action ? false : a.dataset.video ? isVideoActive(a.dataset.video, hash)
      : a.dataset.exact ? a.dataset.href === hash : isActive(a.dataset.href, hash);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  const tab = phoneTab(hash);
  for (const a of $("tabbar").querySelectorAll("a.tab")) {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  renderSubnav(hash);
}

function screenName(hash) {
  // "#/refs?tab=library" is the 공용 라이브러리 screen; other queries do not change the screen
  const key = helpPage(hash) === "library" ? "#/library" : hash.split("?")[0];
  const s = VIDEO_SCREENS.find((it) => isActive(it.route, key)) || ADMIN_SCREENS.find((it) => isActive(it.route, key))
    || SCREENS.find((it) => isActive(it.route, key)) || SCREENS[0];
  return s.name;
}

// ---- top bar ----------------------------------------------------------------

function buildTopbar() {
  const sel = h("select", { id: "category-select", "aria-label": "카테고리" });
  sel.addEventListener("change", () => ctx.setCategory(sel.value));
  $("topbar").append(
    h("a", { class: "brand", href: "#/home", "aria-label": "yt-coach 홈" }, logo(), h("span", {}, "yt-coach")),
    h("label", { class: "cat-select" }, h("span", { class: "cat-label" }, "카테고리"), sel),
    h("div", { class: "topbar-gap" }),
    // Claude job status chip (status-chip.js): empty (hidden) while nothing runs.
    h("div", { id: "status-chip", class: "status-chip" }),
    // server edition admins only (showAdminEntry): the 관리 screen. Phones reach it from the help menu.
    h("a", { class: "btn btn-ghost topbar-admin", id: "admin-link", href: "#/admin", hidden: true }, "관리"),
    helpMenu(),
  );
  updateTopbar();
}

// Only refresh dynamic parts so focus survives.
function updateTopbar() {
  const sel = $("category-select");
  if (!sel) return;
  const names = state.categories.map((c) => c.category);
  const same = sel.options.length === names.length && names.every((n, i) => sel.options[i].value === n);
  if (!same) {
    sel.replaceChildren();
    if (names.length === 0) sel.append(h("option", { value: "" }, "카테고리 없음"));
    for (const n of names) sel.append(h("option", { value: n }, n));
  }
  sel.disabled = names.length === 0;
  if (ctx.category && sel.value !== ctx.category) sel.value = ctx.category;
}

// ---- help menu: 도움말 열기 / 사용 가이드 / 서버 끄기 (a disclosure popover under the 도움말 button) ----------

function helpMenu() {
  const btn = h("button", {
    type: "button", class: "btn help-button", id: "help-button", "aria-expanded": "false", "aria-controls": "help-pop",
  }, "도움말");
  // tabindex -1: a click on the menu's text keeps focus inside it, so focusout does not close the menu
  const pop = h("div", { class: "help-pop", id: "help-pop", tabindex: "-1", hidden: true });
  btn.addEventListener("click", () => setHelpMenu(pop.hidden));
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); setHelpMenu(false); btn.focus(); }
  });
  // focus moving out of the menu (Tab past the last item) closes it; focus stays where the user moved it
  pop.addEventListener("focusout", (e) => {
    const box = $("help-menu");
    if (box && !(e.relatedTarget && box.contains(e.relatedTarget))) setHelpMenu(false);
  });
  document.addEventListener("click", (e) => {
    const box = $("help-menu");
    if (box && !box.contains(e.target)) setHelpMenu(false);
  });
  fillHelpPop(pop);
  return h("div", { class: "help-menu", id: "help-menu" }, btn, pop);
}

function fillHelpPop(pop) {
  // #help-toggle opens/closes the side panel; help-panel.js keeps its aria-expanded in sync.
  const toggle = h("button", { type: "button", class: "pop-item", id: "help-toggle", "aria-controls": "help-panel",
    "aria-expanded": "false" }, "도움말 열기");
  toggle.addEventListener("click", () => {
    setHelpMenu(false);
    if (helpPanel) helpPanel.toggle();
  });
  const guide = h("a", { class: "pop-item", href: "#/guide" }, "사용 가이드");
  guide.addEventListener("click", () => setHelpMenu(false));
  const admin = h("a", { class: "pop-item", id: "admin-pop", href: "#/admin", hidden: true }, "관리");
  admin.addEventListener("click", () => setHelpMenu(false));
  const off = h("button", { type: "button", class: "pop-item pop-danger" }, "서버 끄기");
  off.addEventListener("click", onShutdownClick);
  // a shutdown confirm (only while a collect job runs) appears here
  const confirm = h("div", { class: "pop-confirm", id: "help-confirm", hidden: true });
  pop.replaceChildren(toggle, guide, admin, h("div", { class: "pop-sep", role: "separator" }), off, confirm);
}

function setHelpMenu(open) {
  const btn = $("help-button");
  const pop = $("help-pop");
  if (!btn || !pop || pop.hidden === !open) return;
  if (open) {
    const confirm = $("help-confirm");      // a cancelled shutdown confirm starts over
    confirm.hidden = true;
    confirm.replaceChildren();
    const toggle = $("help-toggle");
    toggle.setAttribute("aria-expanded", String(document.body.classList.contains("help-open")));
    toggle.textContent = document.body.classList.contains("help-open") ? "도움말 닫기" : "도움말 열기";
  }
  pop.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) pop.querySelector(".pop-item").focus();
}

async function collectBusy() {
  try {
    const data = await api("GET", "/api/jobs/current?kind=collect");
    const job = data.job;
    return !!job && (job.status === "running" || job.status === "awaiting");
  } catch {
    return false;
  }
}

async function onShutdownClick() {
  if (!(await collectBusy())) {
    doShutdown();
    return;
  }
  const box = $("help-confirm");
  const yes = h("button", { type: "button", class: "btn btn-danger btn-s" }, "끄기");
  const no = h("button", { type: "button", class: "btn btn-s" }, "취소");
  box.replaceChildren(h("p", { class: "confirm-text", role: "alert" }, "진행 중인 작업이 멈춥니다. 끌까요?"),
    h("div", { class: "btn-row" }, yes, no));
  box.hidden = false;
  yes.addEventListener("click", doShutdown);
  no.addEventListener("click", () => { setHelpMenu(false); $("help-button").focus(); });
  no.focus();
}

async function doShutdown() {
  try {
    await api("POST", "/api/shutdown", {});
  } catch (e) {
    if (e.error !== "network") { toast(e.message, "error"); return; }
  }
  if (statusChip) statusChip.stop();
  showBanner("서버를 껐습니다. 다시 쓰려면 Claude에게 “화면 열어줘”라고 말하세요.");
  $("main").replaceChildren(h("p", { class: "muted" }, "서버가 꺼졌습니다."));
  setHelpMenu(false);
  $("help-button").disabled = true;
}

// ---- router ----------------------------------------------------------------

function parseHash() {
  const hash = location.hash || "#/home";
  const [raw, query] = hash.replace(/^#\/?/, "").split("?");
  const [head, ...rest] = raw.split("/");
  const tail = rest.join("/");
  switch (head) {
    case "videos":
    case "video": {
      // #/video/<id>/shoot is the phone shoot mode: its own page (full screen, no app chrome)
      const params = parseVideoHash(hash) || { mode: "list" };
      if (params.mode === "shoot") return { page: "shoot", params };
      return { page: "video", params };
    }
    case "add": return { page: "refs", params: { add: true } };
    case "library": return { page: "library", params: {} };
    case "admin": return { page: "admin", params: {} };
    case "guide": return { page: "guide", params: { tab: tail === "long" ? "long" : "shorts" } };
    case "typify": return { page: "typify", params: {} };
    case "motif": return { page: "motif", params: {} };
    case "mission": return { page: "mission", params: {} };
    case "report": {
      // #/report = category report tab; #/report/review[/<week>] = weekly review tab.
      const [sub, week] = tail.split("/");
      if (sub === "review") return { page: "report", params: { tab: "review", week: week || null } };
      return { page: "report", params: { tab: "report" } };
    }
    case "draft": {
      // #/draft | #/draft/view/<encoded path> | #/draft/new[/<encoded source id>]; anything else is the list.
      const [sub, ...more] = tail.split("/");
      const arg = more.join("/");
      try {
        if (sub === "view" && arg) return { page: "draft", params: { mode: "view", path: decodeURIComponent(arg) } };
        if (sub === "new") return { page: "draft", params: { mode: "new", source: arg ? decodeURIComponent(arg) : null } };
      } catch { /* undecodable: fall back to the list */ }
      return { page: "draft", params: { mode: "list" } };
    }
    case "conti": {
      let path = null;
      if (tail) { try { path = decodeURIComponent(tail); } catch { path = null; } }
      return { page: "conti", params: { path } };
    }
    case "diagnose": {
      // #/diagnose = target list; #/diagnose/<encoded target path> = one target. Undecodable: the list.
      let path = null;
      if (tail) { try { path = decodeURIComponent(tail); } catch { path = null; } }
      return { page: "diagnose", params: { path } };
    }
    case "refs": {
      // #/refs?tab=library: the 공용 라이브러리 tab of 레퍼런스
      if (new URLSearchParams(query || "").get("tab") === "library") return { page: "library", params: {} };
      if (!tail) return { page: "refs", params: {} };
      try { return { page: "refs", params: { id: decodeURIComponent(tail) } }; } catch { return null; }
    }
    default: return { page: "home", params: {} };
  }
}

// Shell parts that follow the route: menus, tab title, sidebar numbers, help page. Also ends a leave question.
// sidebar false: a page taking the route in place (update) shares its own status read instead (ctx.statusSeen).
function routeChrome(hash, sidebar = true) {
  state.hash = hash;
  state.leaveOk = false;
  state.restoring = false;
  stampEntry();
  closeLeave();
  setHelpMenu(false);
  markActive();
  document.title = `${screenName(hash)} · yt-coach`;
  if (sidebar) refreshSidebar();     // progress and counts change as the user works; cheap local reads
  if (helpPanel) helpPanel.setPage(helpPage(hash));
  // Shoot mode: dark full screen without header, sidebar, sub links and bottom tabs; any other route restores them.
  const shoot = (parseVideoHash(hash) || {}).mode === "shoot";
  document.body.classList.toggle("shoot-mode", shoot);
  document.body.classList.toggle("shoot-dark", shoot);
}

async function route() {
  // Old routes (#/draft…, #/conti…, #/diagnose…, #/library) live on as redirects. replace(): no extra history
  // entry; its hashchange routes the new hash.
  const legacy = legacyRoute(location.hash);
  if (legacy) { location.replace(legacy); return; }
  const hash = location.hash || "#/home";
  // A malformed #/refs/<id> (undecodable) parses to null: show the list instead.
  const { page, params } = parseHash() || { page: "refs", params: {} };
  const cur = state.current;
  const inPlace = !!cur && state.currentPage === page && typeof cur.update === "function";
  routeChrome(hash, !inPlace);
  // Same page, same subject (another tab of the open video): the page switches in place and keeps its ctx.
  if (inPlace && cur.update(params) === true) return;
  if (inPlace) refreshSidebar();     // the page declined: a remount, read the sidebar like any route
  const token = ++state.navToken;
  if (state.abort) state.abort.abort();
  state.abort = new AbortController();
  const signal = state.abort.signal;
  const main = $("main");
  if (cur && typeof cur.unmount === "function") {
    try { cur.unmount(); } catch { /* ignore page cleanup errors */ }
  }
  state.current = null;
  state.currentPage = null;
  const off = disabledNote(page);
  if (off) {
    main.replaceChildren(h("p", { class: "feature-note muted", role: "status", "data-feature-note": off.feature }, off.text));
    keepFocus();
    return;
  }
  main.replaceChildren(h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const mod = await import(`/static/pages/${page}.js`);
    if (token !== state.navToken) return;
    state.current = mod;
    state.currentPage = page;
    main.replaceChildren();
    await mod.mount(main, params, pageCtx(token, signal));
    if (token === state.navToken) keepFocus();
  } catch (e) {
    if (token !== state.navToken) return;
    main.replaceChildren(h("p", { class: "error", role: "alert" }, `화면을 열지 못했습니다: ${e && e.message ? e.message : e}`));
  }
}

// The old page's focused control is gone (replaced by the new page): focus lands on <main> (tabindex -1, no outline)
// instead of <body>, so the next Tab starts in the content and screen readers read from the new page.
function keepFocus() {
  const a = document.activeElement;
  if (!a || a === document.body) $("main").focus({ preventScroll: true });
}

// ---- leaving with unsaved input ------------------------------------------------
// A page with unsaved input (unsavedNote) is asked about before any route change. While the question is open the
// browser goes back to the page's own history entry (no duplicate entries): a new entry (link, code) is dropped with
// back(); a back/forward move is undone with forward()/back(), told apart by the entries' stamps. No browser dialog.

let stampSeq = 0;
// Every routed entry gets a creation stamp; entries keep their order, so stamps compare like positions.
function stampEntry() {
  const s = history.state;
  if (s && typeof s.ytcAt === "number") { state.at = s.ytcAt; return; }
  state.at = Date.now() * 1000 + (stampSeq++ % 1000);
  try { history.replaceState({ ...(s && typeof s === "object" ? s : {}), ytcAt: state.at }, ""); } catch { /* ignore */ }
}

// Go back to the open page's entry without routing (its hashchange is skipped).
function restoreEntry() {
  const s = history.state;
  state.restoring = true;
  if (s && typeof s.ytcAt === "number") {
    if (s.ytcAt < state.at) history.forward(); else history.back();     // the user moved back / forward
  } else {
    history.back();                                                      // a new entry: drop it
  }
}

// In-app navigation for pages: replace = no new history entry; force = the user already chose to leave.
// A replace while the page still has unsaved input (no force) becomes a new entry: the leave question puts the page
// back with back(), and a replaced entry has no page entry of its own below it (the user would bounce out).
function navigate(hash, { replace = false, force = false } = {}) {
  if (force) state.leaveOk = true;
  if (replace && (force || !pageNote())) location.replace(hash); else location.hash = hash;
}

function pageNote() {
  const cur = state.current;
  if (!cur || typeof cur.unsavedNote !== "function") return null;
  try { return cur.unsavedNote() || null; } catch { return null; }
}

function closeLeave() {
  const box = $("leave-ask");
  if (box) { box.hidden = true; box.replaceChildren(); }
}

// The leave question (also ctx.confirmLeave for in-page moves such as the video page's 다시 만들기).
function confirmLeave(note, go, stayed) {
  let box = $("leave-ask");
  if (!box) {
    box = h("section", { id: "leave-ask", class: "leave-ask", "aria-labelledby": "leave-ask-t", hidden: true });
    $("main").before(box);
  }
  const back = document.activeElement;
  const leave = h("button", { type: "button", class: "btn btn-danger btn-s" }, "버리고 이동");
  const stay = h("button", { type: "button", class: "btn btn-s" }, "계속 편집");
  leave.addEventListener("click", () => { closeLeave(); go(); });
  stay.addEventListener("click", () => {
    closeLeave();
    if (typeof stayed === "function") stayed();
    if (back && back.isConnected && back !== document.body) back.focus();
    else $("main").focus();
  });
  box.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); stay.click(); } };
  box.replaceChildren(
    h("p", { class: "leave-ask-text", id: "leave-ask-t", role: "alert" }, `${note}은 사라집니다. 이동할까요?`),
    h("div", { class: "btn-row" }, leave, stay));
  box.hidden = false;
  stay.focus();
}

function onHashChange() {
  // Ignore non-route hashes (e.g. the skip link's "#main"); only "#/..." drives the router.
  if (!(location.hash === "" || location.hash.startsWith("#/"))) return;
  const target = location.hash || "#/home";
  if (state.restoring && target === state.hash) { state.restoring = false; return; }
  if (!state.leaveOk && state.hash && target !== state.hash) {
    const note = pageNote();
    if (note) {
      restoreEntry();
      if (state.current && typeof state.current.onStay === "function") state.current.onStay();
      confirmLeave(note, () => navigate(target, { force: true }));
      return;
    }
  }
  route();
}

// The 관리 entry (header link + help menu item) for server-edition admins. The server checks again on every call.
function showAdminEntry(on) {
  for (const id of ["admin-link", "admin-pop"]) {
    const el = $(id);
    if (el) el.hidden = !on;
  }
}

// ---- start ----------------------------------------------------------------

async function start() {
  buildTopbar();
  renderTabbar();
  renderSidebar();
  helpPanel = createHelpPanel($("help-panel"), () => ctx.category);
  statusChip = createStatusChip({ el: $("status-chip"), api, toast });
  window.addEventListener("hashchange", onHashChange);
  document.querySelector(".skip").addEventListener("click", (e) => {
    e.preventDefault();
    $("main").focus();
  });
  const features = await loadFeatureInfo(api);
  disabledFeatures = features.disabled;
  watchFeatures(document.body, disabledFeatures);   // hides what this edition cannot do (/api/features)
  showAdminEntry(await loadIsAdmin(api, features.edition));   // server edition: GET /api/me (local: no request)
  await refreshCategories();
  await route();
}

start();
