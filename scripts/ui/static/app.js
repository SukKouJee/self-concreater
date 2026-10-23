import { api, showBanner } from "/static/api.js";
import { h } from "/static/dom.js";

const CATEGORY_KEY = "ytc.category";

// Menu definition. `soon` items are 2차: a placeholder page that shows the terminal command.
const MENU = [
  { group: null, items: [{ name: "홈", route: "#/home" }] },
  {
    group: "모으기",
    items: [
      { name: "레퍼런스 추가", route: "#/add" },
      {
        name: "레퍼런스 목록", soon: "list", command: "/yt-coach:yt-add",
        note: "/yt-coach:yt-add로 추가한 영상은 refs/ref_<카테고리>/videos/에 있습니다.",
      },
    ],
  },
  {
    group: "분석",
    items: [
      { name: "유형화", soon: "typify", command: "/yt-coach:yt-typify" },
      { name: "리포트", soon: "report", command: "/yt-coach:yt-review" },
    ],
  },
  {
    group: "만들기",
    items: [
      { name: "모티브", soon: "motive", command: "/yt-coach:yt-inspire" },
      { name: "기획안", soon: "draft", command: "/yt-coach:yt-draft" },
      { name: "콘티", route: "#/conti" },
    ],
  },
  {
    group: "성장",
    items: [
      { name: "진단", soon: "diagnose", command: "/yt-coach:yt-diagnose" },
      { name: "미션", soon: "mission", command: "/yt-coach:yt-mission" },
    ],
  },
];

const SOON = {};
for (const g of MENU) for (const it of g.items) if (it.soon) SOON[it.soon] = it;

const $ = (id) => document.getElementById(id);
const state = { status: null, categories: [], current: null, navToken: 0 };

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

const ctx = {
  category: null,
  api,
  toast,
  setCategory(name) {
    ctx.category = name || null;
    if (name) storeCategory(name);
    renderTopbar();
    route();
  },
  refreshCategories,
};

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
  renderTopbar();
}

// ---- menu ----------------------------------------------------------------

function itemHref(it) {
  return it.soon ? `#/soon/${it.soon}` : it.route;
}

function renderMenu() {
  const nav = $("menu");
  nav.replaceChildren();
  for (const g of MENU) {
    const sec = h("div", { class: "menu-group" });
    if (g.group) sec.append(h("div", { class: "menu-title", id: `mg-${g.group}` }, g.group));
    const ul = h("ul", { class: "menu-list", "aria-labelledby": g.group ? `mg-${g.group}` : false });
    for (const it of g.items) {
      const a = h("a", { class: "menu-link" + (it.soon ? " soon" : ""), href: itemHref(it), "data-href": itemHref(it) }, it.name);
      if (it.soon) a.append(h("span", { class: "badge" }, "2차"));
      ul.append(h("li", {}, a));
    }
    sec.append(ul);
    nav.append(sec);
  }
}

function markActive() {
  const hash = location.hash || "#/home";
  for (const a of $("menu").querySelectorAll("a.menu-link")) {
    const href = a.dataset.href;
    const active = hash === href || (href === "#/conti" && hash.startsWith("#/conti/"));
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  $("scrim").hidden = !open;
  const btn = document.getElementById("menu-toggle");
  if (btn) btn.setAttribute("aria-expanded", String(open));
}

// ---- top bar ----------------------------------------------------------------

function renderTopbar() {
  const bar = $("topbar");
  bar.replaceChildren();
  const toggle = h("button", {
    type: "button", class: "btn btn-icon menu-toggle", id: "menu-toggle", "aria-label": "메뉴 열기",
    "aria-controls": "menu", "aria-expanded": String(document.body.classList.contains("drawer-open")),
  }, "☰");
  toggle.addEventListener("click", () => setDrawer(!document.body.classList.contains("drawer-open")));
  bar.append(toggle, h("a", { class: "brand", href: "#/home" }, "yt-coach"));

  const sel = h("select", { id: "category-select", "aria-label": "카테고리" });
  if (state.categories.length === 0) {
    sel.append(h("option", { value: "" }, "카테고리 없음"));
    sel.disabled = true;
  }
  for (const c of state.categories) {
    sel.append(h("option", { value: c.category, selected: c.category === ctx.category }, c.category));
  }
  sel.addEventListener("change", () => ctx.setCategory(sel.value));
  bar.append(h("div", { class: "cat-select" }, sel));

  const root = state.status ? state.status.work_root : "";
  bar.append(h("span", { class: "work-root", title: root }, root));
  bar.append(shutdownArea());
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
    case "conti": {
      let path = null;
      if (tail) { try { path = decodeURIComponent(tail); } catch { path = null; } }
      return { page: "conti", params: { path } };
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
  const main = $("main");
  if (state.current && typeof state.current.unmount === "function") {
    try { state.current.unmount(); } catch { /* ignore page cleanup errors */ }
  }
  state.current = null;
  setDrawer(false);
  markActive();
  const { page, params } = parseHash();
  main.replaceChildren(h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const mod = await import(`/static/pages/${page}.js`);
    if (token !== state.navToken) return;
    state.current = mod;
    main.replaceChildren();
    await mod.mount(main, params, ctx);
  } catch (e) {
    if (token !== state.navToken) return;
    main.replaceChildren(h("p", { class: "error", role: "alert" }, `화면을 열지 못했습니다: ${e && e.message ? e.message : e}`));
  }
}

// ---- start ----------------------------------------------------------------

async function start() {
  renderMenu();
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setDrawer(false); });
  $("scrim").addEventListener("click", () => setDrawer(false));
  window.addEventListener("hashchange", route);
  await refreshCategories();
  await route();
}

start();
