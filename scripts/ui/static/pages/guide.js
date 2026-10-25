// 가이드 화면: 숏폼/롱폼 만들기 순서. docs/help/guide-shorts.md, guide-long.md를 그대로 렌더하고,
// 단계 링크가 가리키는 화면 중 홈 진행 단계에 있는 다섯 개(모으기·유형화·기획안·콘티·진단)에만 완료 배지를 붙인다.
// 읽기 전용 화면이라 파일은 바뀌지 않는다.
import { h, fill } from "/static/dom.js";
import { renderMarkdown } from "/static/md.js";

const TABS = [
  { key: "shorts", label: "숏폼 만들기", page: "guide-shorts" },
  { key: "long", label: "롱폼 만들기", page: "guide-long" },
];
// step-line link route -> /api/status step key. Other steps (모티브, 미션, ...) get no badge.
const ROUTE_STEP = { "#/add": "collect", "#/typify": "typify", "#/draft": "draft", "#/conti": "conti", "#/diagnose": "diagnose" };

let token = 0;
let current = null;   // { el, params, ctx } of the mounted screen

function tabLinks(active) {
  const nav = h("nav", { class: "guide-tabs", "aria-label": "가이드 종류" });
  for (const t of TABS) {
    nav.append(h("a", {
      class: "guide-tab", href: `#/guide/${t.key}`, "aria-current": t.key === active ? "page" : false,
    }, t.label));
  }
  return nav;
}

// Which step keys are done for the selected category; null when there is no category or the status is unusable.
function doneSteps(status, category) {
  if (!category || !status || !Array.isArray(status.categories)) return null;
  const cat = status.categories.find((c) => c.category === category);
  if (!cat || !Array.isArray(cat.steps)) return null;
  const map = new Map();
  for (const s of cat.steps) map.set(s.key, !!s.done);
  return map;
}

// A step line is a list item that starts with its screen link ("3. [모티브](#/motif)"); "다듬기 ([기획안](#/draft) 화면 안)"
// starts with text, so it never gets a badge.
function addBadges(root, done) {
  if (!done) return;
  for (const li of root.querySelectorAll("li")) {
    const first = li.firstElementChild;
    if (!first || first.tagName !== "A" || li.firstChild !== first) continue;
    const key = ROUTE_STEP[first.getAttribute("href")];
    if (!key || !done.has(key)) continue;
    const ok = done.get(key);
    const badge = h("span", { class: "guide-badge" + (ok ? " guide-badge-done" : "") }, ok ? "✓ 완료" : "아직");
    const nested = li.querySelector(":scope > ul, :scope > ol");
    if (nested) li.insertBefore(badge, nested);
    else li.append(badge);
    li.insertBefore(document.createTextNode(" "), badge);
  }
}

export async function mount(el, params, ctx) {
  const mine = ++token;
  current = { el, params, ctx };
  const tab = TABS.find((t) => t.key === params.tab) || TABS[0];
  const body = h("div", { class: "md guide-md" }, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  fill(el, h("h1", {}, "가이드"), tabLinks(tab.key), body);
  const live = () => mine === token && ctx.isCurrent();
  try {
    const [doc, status] = await Promise.all([
      ctx.api("GET", `/api/help/${tab.page}`),
      ctx.api("GET", "/api/status").catch(() => null),      // badges are optional: the guide still reads without them
    ]);
    if (!live()) return;
    const content = h("div", { class: "md guide-md" },
      renderMarkdown(String(doc.markdown || ""), { routeLinks: true, dropTitle: String(doc.title || "") }));
    addBadges(content, doneSteps(status, ctx.category));
    body.replaceWith(content);
  } catch (e) {
    if (!live() || (e && e.error === "aborted")) return;
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", () => { if (ctx.isCurrent()) mount(el, params, ctx); });
    body.replaceWith(h("div", {},
      h("p", { class: "error", role: "alert" }, (e && e.message) || "가이드를 불러오지 못했습니다"),
      h("div", { class: "btn-row" }, retry)));
  }
}

// Badges follow the selected category: re-render in place (the router does not remount non-home pages).
export function onCategory() {
  if (current) mount(current.el, current.params, current.ctx);
}

export function unmount() {
  token += 1;
  current = null;
}
