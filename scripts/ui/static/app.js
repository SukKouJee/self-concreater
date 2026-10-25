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
import { activeGroup, expandFor, isActive, parseCollapsed, toggleCollapsed } from "/static/menu-utils.js";

const CATEGORY_KEY = "ytc.category";
const MENU_KEY = "ytc.menu.collapsed";

// Menu definition. No item uses `soon` now; the `soon` mechanism and #/soon/<key> placeholder page are kept but unused.
const MENU = [
  { group: null, icon: "home", items: [{ name: "홈", route: "#/home" }] },
  {
    group: "모으기",
    icon: "plus",
    items: [
      { name: "레퍼런스 추가", route: "#/add" },
      { name: "레퍼런스 목록", route: "#/refs" },
      { name: "공용 라이브러리", route: "#/library" },
    ],
  },
  {
    group: "분석",
    icon: "chart",
    items: [
      { name: "유형화", route: "#/typify" },
      { name: "리포트", route: "#/report" },
    ],
  },
  {
    group: "만들기",
    icon: "pen",
    items: [
      { name: "모티브", route: "#/motif" },
      { name: "기획안", route: "#/draft" },
      { name: "콘티", route: "#/conti" },
    ],
  },
  {
    group: "성장",
    icon: "trend",
    items: [
      { name: "진단", route: "#/diagnose" },
      { name: "미션", route: "#/mission" },
    ],
  },
  { group: null, icon: "help", items: [{ name: "가이드", route: "#/guide" }] },
];

const SOON = {};
for (const g of MENU) for (const it of g.items) if (it.soon) SOON[it.soon] = it;

const $ = (id) => document.getElementById(id);
let helpPanel = null;
const state = { status: null, categories: [], current: null, currentPage: null, navToken: 0, abort: null };

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
    } else if (state.current && typeof state.current.onCategory === "function") {
      state.current.onCategory(ctx.category);
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
  try { state.status = await api("GET", "/api/status"); } catch { /* keep previous */ }
  const names = state.categories.map((c) => c.category);
  const stored = readStoredCategory();
  ctx.category = names.includes(ctx.category) ? ctx.category
    : names.includes(stored) ? stored : (names[0] || null);
  updateTopbar();
}

// ---- menu ----------------------------------------------------------------

function itemHref(it) {
  return it.soon ? `#/soon/${it.soon}` : it.route;
}

// Line icons for the menu (24x24, stroke). Built with createElementNS: h() makes HTML elements only.
const ICONS = {
  home: ["M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"],
  plus: ["M12 5v14M5 12h14"],
  chart: ["M4 20V10M10 20V4M16 20v-7M22 20H2"],
  pen: ["M4 20h4L19 9l-4-4L4 16z"],
  trend: ["M3 17l6-6 4 4 8-8M15 7h6v6"],
  help: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6M12 17h.01"],
  chevron: ["M6 9l6 6 6-6"],
};
const SVG_NS = "http://www.w3.org/2000/svg";

function svgIcon(name, cls) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", cls);
  for (const d of ICONS[name]) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function readCollapsed() {
  try { return parseCollapsed(localStorage.getItem(MENU_KEY)); } catch { return []; }
}
function storeCollapsed(list) {
  try { localStorage.setItem(MENU_KEY, JSON.stringify(list)); } catch { /* storage unavailable */ }
}

let lastGroup; // group of the previous route (undefined before the first route)

const menuGroupId = (name) => `mg-${MENU.findIndex((g) => g.group === name)}`;

// Top-level items (홈, 가이드) are links with an icon; groups are a box with a toggle button and the sub links.
function renderMenu() {
  const nav = $("menu");
  nav.replaceChildren();
  for (const g of MENU) {
    if (!g.group) {
      for (const it of g.items) {
        nav.append(h("a", { class: "menu-link menu-top", href: itemHref(it), "data-href": itemHref(it) },
          h("span", { class: "menu-icon" }, svgIcon(g.icon, "icon")), h("span", {}, it.name)));
      }
      continue;
    }
    const id = menuGroupId(g.group);
    const toggle = h("button", {
      type: "button", class: "menu-group-toggle", id: `${id}-t`, "aria-expanded": "true", "aria-controls": `${id}-list`,
    }, h("span", { class: "menu-icon" }, svgIcon(g.icon, "icon")), h("span", { class: "menu-title" }, g.group),
    svgIcon("chevron", "menu-chevron"));
    toggle.addEventListener("click", () => {
      storeCollapsed(toggleCollapsed(readCollapsed(), g.group));
      applyCollapsed();
    });
    const ul = h("ul", { class: "menu-list", id: `${id}-list`, "aria-labelledby": `${id}-t` });
    for (const it of g.items) {
      const a = h("a", { class: "menu-link" + (it.soon ? " soon" : ""), href: itemHref(it), "data-href": itemHref(it) }, it.name);
      if (it.soon) a.append(h("span", { class: "badge" }, "2차"));
      ul.append(h("li", {}, a));
    }
    nav.append(h("div", { class: "menu-group", "data-group": g.group }, toggle, ul));
  }
}

function applyCollapsed() {
  const collapsed = readCollapsed();
  for (const sec of $("menu").querySelectorAll(".menu-group")) {
    const open = !collapsed.includes(sec.dataset.group);
    sec.classList.toggle("closed", !open);
    sec.querySelector(".menu-group-toggle").setAttribute("aria-expanded", String(open));
  }
}

function markActive() {
  const hash = location.hash || "#/home";
  for (const a of $("menu").querySelectorAll("a.menu-link")) {
    if (isActive(a.dataset.href, hash)) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  // Expand only when the user moves into a group from elsewhere: a group collapsed while its screen is open
  // stays collapsed across reloads and within-group navigation.
  const group = activeGroup(MENU, hash, itemHref);
  if (lastGroup !== undefined && group !== lastGroup) {
    const collapsed = readCollapsed();
    const expanded = expandFor(collapsed, group);
    if (expanded !== collapsed) storeCollapsed(expanded);
  }
  lastGroup = group;
  applyCollapsed();
}

function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  $("scrim").hidden = !open;
  const btn = document.getElementById("menu-toggle");
  if (btn) btn.setAttribute("aria-expanded", String(open));
}

// ---- top bar ----------------------------------------------------------------

function buildTopbar() {
  const bar = $("topbar");
  const toggle = h("button", {
    type: "button", class: "btn btn-icon menu-toggle", id: "menu-toggle", "aria-label": "메뉴 열기",
    "aria-controls": "menu", "aria-expanded": "false",
  }, "☰");
  toggle.addEventListener("click", () => setDrawer(!document.body.classList.contains("drawer-open")));
  const sel = h("select", { id: "category-select", "aria-label": "카테고리" });
  sel.addEventListener("change", () => ctx.setCategory(sel.value));
  bar.append(
    toggle,
    h("a", { class: "brand", href: "#/home" }, "yt-coach"),
    h("div", { class: "cat-select" }, sel),
    h("span", { class: "work-root", id: "work-root" }),
    helpToggle(),
    shutdownArea(),
  );
  updateTopbar();
}

function helpToggle() {
  const btn = h("button", {
    type: "button", class: "btn help-toggle", id: "help-toggle", "aria-controls": "help-panel", "aria-expanded": "false",
  }, "도움말");
  btn.addEventListener("click", () => { if (helpPanel) helpPanel.toggle(); });
  return btn;
}

// Only refresh dynamic parts so focus and a pending shutdown confirm survive.
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
  const root = state.status ? state.status.work_root : "";
  const rootEl = $("work-root");
  rootEl.textContent = root;
  rootEl.title = root;
}

function shutdownArea() {
  const box = h("div", { class: "shutdown", id: "shutdown-area" });
  const btn = h("button", { type: "button", class: "btn" }, "서버 끄기");
  btn.addEventListener("click", onShutdownClick);
  box.append(btn);
  return box;
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
  const box = $("shutdown-area");
  const yes = h("button", { type: "button", class: "btn btn-danger" }, "끄기");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  box.replaceChildren(h("span", { class: "confirm-text", role: "alert" }, "진행 중인 작업이 멈춥니다. 끌까요?"), yes, no);
  yes.addEventListener("click", doShutdown);
  no.addEventListener("click", () => {
    const fresh = shutdownArea();
    box.replaceWith(fresh);
    fresh.querySelector("button").focus();
  });
  no.focus();
}

async function doShutdown() {
  try {
    await api("POST", "/api/shutdown", {});
  } catch (e) {
    if (e.error !== "network") { toast(e.message, "error"); return; }
  }
  showBanner("서버를 껐습니다. 다시 쓰려면 터미널에서 ytc ui를 실행하세요.");
  $("main").replaceChildren(h("p", { class: "muted" }, "서버가 꺼졌습니다."));
  $("shutdown-area").replaceChildren();
}

// ---- router ----------------------------------------------------------------

function parseHash() {
  const raw = (location.hash || "#/home").replace(/^#\/?/, "");
  const [head, ...rest] = raw.split("/");
  const tail = rest.join("/");
  switch (head) {
    case "add": return { page: "add", params: {} };
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
    case "soon": {
      const it = SOON[tail];
      if (it) return { page: "placeholder", params: { title: it.name, command: it.command, note: it.note } };
      return { page: "home", params: {} };
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
  setDrawer(false);
  markActive();
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
  renderMenu();
  buildTopbar();
  helpPanel = createHelpPanel($("help-panel"), () => ctx.category);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setDrawer(false); });
  $("scrim").addEventListener("click", () => setDrawer(false));
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
