// 콘티 탭의 카드 보드: 도구 줄(장면 검색 · 구간 필터 칩 · 카드/리스트 보기)과 장면 카드.
// 만드는 함수만 둔다. 상태(검색어·구간·보기·고른 장면)와 다시 그리기는 pages/conti.js가 맡는다.
// 카드를 누르면 지금의 편집 패널(pages/conti.js renderPanel)이 열린다.
import { h, filesUrl } from "/static/dom.js";
import { chip, tag } from "/static/ui.js";
import { sectionLabelName } from "/static/labels.js";
import { aboutSec, artCaption, cardStamp, parseView, refCaption, sceneVisual, tagKind } from "/static/conti-utils.js";

const VIEW_KEY = "ytc.conti.view";

// Card or list view as last chosen in this browser (storage may be off: then cards).
export function readView() {
  try { return parseView(localStorage.getItem(VIEW_KEY)); } catch { return "cards"; }
}

export function writeView(view) {
  try { localStorage.setItem(VIEW_KEY, parseView(view)); } catch { /* storage unavailable */ }
}

// o: { q, section, view, total, shown, groups: [{key,label,count}], onQuery(q), onSection(key), onView(view) }
// The search box is not rebuilt while typing (the caller only redraws the cards), so it keeps its caret.
export function toolRow(o) {
  const search = h("input", {
    type: "search", class: "cb-search", placeholder: "장면 검색", "aria-label": "장면 검색", autocomplete: "off",
    "data-fk": "board-search",
  });
  search.value = o.q || "";
  search.addEventListener("input", () => o.onQuery(search.value));
  const pick = (key, label, count) => {
    const c = chip(label, { pressed: o.section === key, count, onClick: () => o.onSection(key) });
    c.setAttribute("data-fk", `board-sec:${key}`);
    return c;
  };
  const chips = h("div", { class: "cb-chips", role: "group", "aria-label": "구간 필터" },
    pick("all", "전체", o.total), ...o.groups.map((g) => pick(g.key, g.label, g.count)));
  const viewBtn = (key, label) => {
    const b = h("button", { type: "button", class: "cb-view", "aria-pressed": String(o.view === key), "data-fk": `board-view:${key}` }, label);
    b.addEventListener("click", () => { if (o.view !== key) o.onView(key); });
    return b;
  };
  const views = h("div", { class: "cb-views", role: "group", "aria-label": "보기 방식" }, viewBtn("cards", "카드"), viewBtn("list", "리스트"));
  const filtered = o.shown !== o.total;
  const count = h("span", { class: "cb-count", role: "status" }, filtered ? `${o.shown}/${o.total}개` : "");
  return h("div", { class: "cb-tools" }, search, chips, count, views);
}

// No scene matches the search / section: say so and offer to clear.
export function noMatch(onClear) {
  const clear = h("button", { type: "button", class: "btn btn-s", "data-fk": "board-clear" }, "필터 지우기");
  clear.addEventListener("click", onClear);
  return h("div", { class: "cb-empty" }, h("p", { class: "muted" }, "찾는 장면이 없어요"), clear);
}

// Caption under a scene drawing. data-art-cap lets the page change it in place when a drawing job starts or ends.
function artCap(c, busy) {
  const stale = c.art.state === "stale";
  return h("span", { class: `cb-ref cb-art-cap${stale ? " is-stale" : ""}`, "data-art-cap": c.id }, artCaption(c.art, busy));
}

// One scene card. o: { selected, isTab, warn, dot (element or null), artBusy (a drawing job runs or the scene waits
// to be drawn again), onOpen(), more (the ⋯ button) }
// Frame: the pinned photo, else Claude's drawing, else a matched photo, else the shot description (sceneVisual);
// top left "01 · 0:00–0:10", top right length. The drawing is only ever an <img src> (never markup in the page).
export function sceneCard(c, o) {
  const vis = sceneVisual(c);
  const frame = h("span", { class: `cb-frame${vis.kind === "art" ? " has-art" : vis.ref ? " has-photo" : ""}` });
  if (vis.kind === "art") frame.append(h("img", { src: c.art.url, alt: "장면 그림", loading: "lazy" }));
  else if (vis.ref) frame.append(h("img", { src: filesUrl(c.ref.frame), alt: "", loading: "lazy" }));
  else frame.append(h("span", { class: "cb-shot" }, [c.size, c.angle].filter(Boolean).join(" · ")));
  frame.append(h("span", { class: "cb-stamp num" }, cardStamp(c)), h("span", { class: "cb-len" }, aboutSec(c.sec)));
  const top = h("span", { class: "cb-top" }, tag(sectionLabelName(c.label), tagKind(c.label)));
  if (o.dot) top.append(o.dot);
  if (o.warn) top.append(h("span", { class: "warn-icon", title: "대사가 길어요" }, "⚠", h("span", { class: "sr-only" }, "대사가 깁니다")));
  const body = h("span", { class: "cb-body" }, top,
    h("span", { class: "cb-action" }, c.action || ""),
    h("span", { class: "cb-line" + (c.line ? "" : " muted") }, c.line ? `“${c.line}”` : "(대사 없음)"),
    vis.kind === "art" ? artCap(c, o.artBusy) : c.ref ? h("span", { class: "cb-ref" }, refCaption(c.ref)) : null);
  const open = h("button", {
    type: "button", class: "cb-open" + (o.selected ? " is-selected" : ""), "data-n": String(c.n), "data-id": c.id,
    "data-nav": `card:${c.id}`, "aria-current": o.selected ? "true" : false, tabindex: o.isTab ? "0" : "-1",
  }, frame, body);
  open.addEventListener("click", o.onOpen);
  return h("li", { class: "cb-card" + (o.selected ? " is-selected" : ""), "data-id": c.id }, open, o.more);
}
