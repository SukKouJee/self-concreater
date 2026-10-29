// Page contract (pages/<name>.js):
//   export function mount(el, params, ctx)   // may be async
//   export function unmount()                // optional, called before the next route
//   export function onCategory(category)     // optional; called when the category changes while this page is
//                                            // open. Non-home pages are NOT remounted (keeps in-progress state).
// ctx: { category, setCategory(name), refreshCategories(), toast(msg, kind), api(method, path, body),
//        signal (AbortSignal, aborted when the user navigates away), isCurrent() (false once navigated away) }
// Rule: after EVERY await in mount/handlers, `if (!ctx.isCurrent()) return;` before touching the DOM.
import { api, showBanner } from "/static/api.js";
import { h } from "/static/dom.js";
import { createHelpPanel, helpPage } from "/static/help-panel.js";
import {
  PHONE_TABS, SIDEBAR, isActive, isVideoActive, phoneTab, sidebarVideos, subnavItems, videoLinks, videoProgress,
} from "/static/menu-utils.js";

const CATEGORY_KEY = "ytc.category";
const SIDEBAR_VIDEOS = 5;   // unfinished videos listed in the sidebar; the rest sit behind "전체 N편"

// Every screen with its name (browser tab title). The help chatbot gets the same list (api_help.MENU).
// The sidebar and the phone tabs are defined in menu-utils.js.
const SCREENS = [
  { name: "홈", route: "#/home" },
  { name: "레퍼런스", route: "#/refs" },
  { name: "공용 라이브러리", route: "#/library" },
  { name: "유형화", route: "#/typify" },
  { name: "리포트", route: "#/report" },
  { name: "모티브", route: "#/motif" },
  { name: "기획안", route: "#/draft" },
  { name: "콘티", route: "#/conti" },
  { name: "진단", route: "#/diagnose" },
  { name: "미션", route: "#/mission" },
  { name: "가이드", route: "#/guide" },
];

const $ = (id) => document.getElementById(id);
let helpPanel = null;
const state = {
  categories: [], current: null, currentPage: null, navToken: 0, abort: null,
  // sidebar data for the current category: { category, videos, ref, archetypes, motifs } (missing = unknown)
  side: null, sideSeq: 0,
};

// ---- ctx ---------------------------------------------------------------

function readStoredCategory() {
  try { return localStorage.getItem(CATEGORY_KEY); } catch { return null; }
}
function storeCategory(v) {
  try { localStorage.setItem(CATEGORY_KEY, v); } catch { /* storage unavailable */ }
}

function toast(msg, kind = "info") {
  const t = h("div", { class: `toast toast-${kind}` }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 3500);
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
  refreshSidebar();
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

function menuLink(it) {
  const side = state.side || {};
  const n = it.count ? side[it.count] : undefined;
  const label = typeof n === "number" ? `${it.label} ${n}개` : it.label;
  return h("a", { class: "menu-link" + (it.href === "#/draft/new" ? " menu-new" : ""), href: it.href,
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
        "aria-valuenow": String(pct), "aria-label": "진행" }, fill),
      h("span", { class: "vid-stage" }, stage))));
}

function videoRows() {
  const side = state.side;
  if (!ctx.category) return [h("li", { class: "menu-note" }, "카테고리를 먼저 만들어요")];
  if (!side || side.videos === null) return [];
  const { shown, total } = sidebarVideos(side.videos, SIDEBAR_VIDEOS);
  const rows = shown.map(videoRow);
  if (shown.length === 0) rows.push(h("li", { class: "menu-note" }, total ? "만드는 중인 영상이 없어요" : "아직 영상이 없어요"));
  if (total > shown.length) rows.push(h("li", {}, h("a", { class: "menu-link menu-all", href: "#/draft", "data-href": "#/draft" }, `전체 ${total}편`)));
  return rows;
}

function renderSidebar() {
  const nav = $("menu");
  if (!nav) return;
  nav.replaceChildren(...SIDEBAR.map((g, i) => {
    const id = `menu-g${i}`;
    const ul = h("ul", { class: "menu-list", "aria-labelledby": id });
    if (g.videos) ul.append(...videoRows());
    for (const it of g.items) ul.append(h("li", {}, menuLink(it)));
    return h("div", { class: "menu-group" }, h("h2", { class: "menu-label", id }, g.group), ul);
  }));
  markActive();
}

function renderTabbar() {
  $("tabbar").replaceChildren(...PHONE_TABS.map((t) => h("a", { class: "tab", href: t.href, "data-tab": t.key },
    svgIcon(t.icon, "tab-icon"), h("span", {}, t.label))));
}

function renderSubnav(hash) {
  const nav = $("subnav");
  const items = subnavItems(hash);
  nav.hidden = items.length === 0;
  nav.classList.toggle("subnav-phone", items.length > 0 && items.every((it) => it.phoneOnly));
  nav.replaceChildren(...items.map((it) => h("a", {
    class: "subnav-link" + (it.phoneOnly ? " subnav-phone-item" : ""), href: it.href,
    "aria-current": it.current ? "page" : null,
  }, it.label)));
}

function markActive() {
  const hash = location.hash || "#/home";
  for (const a of $("menu").querySelectorAll("a.menu-link")) {
    const on = a.dataset.video ? isVideoActive(a.dataset.video, hash) : isActive(a.dataset.href, hash);
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
  const s = SCREENS.find((it) => isActive(it.route, hash)) || SCREENS[0];
  return s.name;
}

// ---- top bar ----------------------------------------------------------------

function buildTopbar() {
  const sel = h("select", { id: "category-select" });
  sel.addEventListener("change", () => ctx.setCategory(sel.value));
  $("topbar").append(
    h("a", { class: "brand", href: "#/home", "aria-label": "yt-coach 홈" }, logo(), h("span", {}, "yt-coach")),
    h("label", { class: "cat-select" }, h("span", { class: "cat-label" }, "카테고리"), sel),
    h("div", { class: "topbar-gap" }),
    // Claude job status chip: filled by a later phase; kept empty so the header layout already has its place.
    h("div", { id: "status-chip", class: "status-chip" }),
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
  const pop = h("div", { class: "help-pop", id: "help-pop", hidden: true });
  btn.addEventListener("click", () => setHelpMenu(pop.hidden));
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); setHelpMenu(false); btn.focus(); }
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
  const off = h("button", { type: "button", class: "pop-item pop-danger" }, "서버 끄기");
  off.addEventListener("click", onShutdownClick);
  // a shutdown confirm (only while a collect job runs) appears here
  const confirm = h("div", { class: "pop-confirm", id: "help-confirm", hidden: true });
  pop.replaceChildren(toggle, guide, h("div", { class: "pop-sep", role: "separator" }), off, confirm);
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
  showBanner("서버를 껐습니다. 다시 쓰려면 Claude에게 “화면 열어줘”라고 말하세요.");
  $("main").replaceChildren(h("p", { class: "muted" }, "서버가 꺼졌습니다."));
  setHelpMenu(false);
  $("help-button").disabled = true;
}

// ---- router ----------------------------------------------------------------

function parseHash() {
  const raw = (location.hash || "#/home").replace(/^#\/?/, "");
  const [head, ...rest] = raw.split("/");
  const tail = rest.join("/");
  switch (head) {
    case "add": return { page: "refs", params: { add: true } };
    case "library": return { page: "library", params: {} };
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
      if (!tail) return { page: "refs", params: {} };
      try { return { page: "refs", params: { id: decodeURIComponent(tail) } }; } catch { return null; }
    }
    default: return { page: "home", params: {} };
  }
}

async function route() {
  const token = ++state.navToken;
  if (state.abort) state.abort.abort();
  state.abort = new AbortController();
  const signal = state.abort.signal;
  const main = $("main");
  if (state.current && typeof state.current.unmount === "function") {
    try { state.current.unmount(); } catch { /* ignore page cleanup errors */ }
  }
  state.current = null;
  state.currentPage = null;
  setHelpMenu(false);
  markActive();
  document.title = `${screenName(location.hash || "#/home")} · yt-coach`;
  refreshSidebar();             // progress and counts change as the user works; cheap local reads
  if (helpPanel) helpPanel.setPage(helpPage(location.hash));
  // A malformed #/refs/<id> (undecodable) parses to null: show the list instead.
  const { page, params } = parseHash() || { page: "refs", params: {} };
  main.replaceChildren(h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const mod = await import(`/static/pages/${page}.js`);
    if (token !== state.navToken) return;
    state.current = mod;
    state.currentPage = page;
    main.replaceChildren();
    await mod.mount(main, params, pageCtx(token, signal));
  } catch (e) {
    if (token !== state.navToken) return;
    main.replaceChildren(h("p", { class: "error", role: "alert" }, `화면을 열지 못했습니다: ${e && e.message ? e.message : e}`));
  }
}

// ---- start ----------------------------------------------------------------

async function start() {
  buildTopbar();
  renderTabbar();
  renderSidebar();
  helpPanel = createHelpPanel($("help-panel"), () => ctx.category);
  window.addEventListener("hashchange", () => {
    // Ignore non-route hashes (e.g. the skip link's "#main"); only "#/..." drives the router.
    if (location.hash === "" || location.hash.startsWith("#/")) route();
  });
  document.querySelector(".skip").addEventListener("click", (e) => {
    e.preventDefault();
    $("main").focus();
  });
  await refreshCategories();
  await route();
}

start();
