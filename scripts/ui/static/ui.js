// Small shared UI kit on the spec tokens (app.css): buttons, toggle chips, kind/section tags, cards, underline tabs.
// Builders return plain elements; callers own state and re-render. Only dom.js is imported (node tests load this file).
import { h } from "./dom.js";

const SIZES = { s: "btn-s" };
const VARIANTS = { rec: "btn-rec", primary: "btn-primary", ghost: "btn-ghost", link: "btn-link" };
// Card kinds and conti section tags; intro/body/outro reuse the flow/tech/combo colors (mockup values).
export const TAG_KINDS = ["flow", "tech", "angle", "combo", "hook", "intro", "body", "check", "outro"];

// "outline" (the default .btn look) and size "m" (32px) add no class; unknown values fall back to them.
export function buttonClass(size, variant) {
  return ["btn", SIZES[size], VARIANTS[variant]].filter(Boolean).join(" ");
}

// rec = the one main (recording) action per screen; primary = dark fill; ghost/link = quiet secondary actions.
export function button(label, { size = "m", variant = "outline", onClick, ...attrs } = {}) {
  const el = h("button", { type: "button", ...attrs, class: buttonClass(size, variant) }, label);
  if (onClick) el.addEventListener("click", onClick);
  return el;
}

// Filter / choice chip: a toggle button (aria-pressed). The caller flips `pressed` and re-renders.
export function chip(label, { pressed = false, count, onClick } = {}) {
  const el = h("button", { type: "button", class: "ui-chip", "aria-pressed": String(!!pressed) }, label,
    count === undefined || count === null ? null : h("span", { class: "ui-chip-count" }, String(count)));
  if (onClick) el.addEventListener("click", onClick);
  return el;
}

export function tag(label, kind) {
  return h("span", { class: TAG_KINDS.includes(kind) ? `ui-tag ui-tag-${kind}` : "ui-tag" }, label);
}

export function card(...kids) {
  return h("div", { class: "ui-card" }, ...kids);
}

// Roving focus for a horizontal tab list: next index for the key, or null when the key does not move.
export function nextTabIndex(i, key, n) {
  if (key === "ArrowRight") return (i + 1) % n;
  if (key === "ArrowLeft") return (i - 1 + n) % n;
  if (key === "Home") return 0;
  if (key === "End") return n - 1;
  return null;
}

// Underline tabs (role=tablist). Arrow keys/Home/End move and select (automatic activation); onSelect(key) runs
// only when the selection changes. Panels are the caller's (give each tab's panel aria-labelledby if needed).
export function tabs(items, current, onSelect, { label } = {}) {
  const list = h("div", { class: "ui-tabs", role: "tablist", "aria-label": label });
  let selected = current;
  const btns = items.map((it) => h("button", {
    type: "button", class: "ui-tab", role: "tab", "data-key": it.key,
  }, it.label));
  const paint = () => {
    // no matching tab: the first one still takes Tab focus so the list stays reachable
    const focusKey = items.some((it) => it.key === selected) ? selected : (items[0] && items[0].key);
    for (const b of btns) {
      const key = b.getAttribute("data-key");
      b.setAttribute("aria-selected", String(key === selected));
      b.setAttribute("tabindex", key === focusKey ? "0" : "-1");
    }
  };
  const choose = (key) => {
    if (key === selected) return;
    selected = key;
    paint();
    if (onSelect) onSelect(key);
  };
  btns.forEach((b, i) => {
    b.addEventListener("click", () => choose(items[i].key));
    b.addEventListener("keydown", (e) => {
      const next = nextTabIndex(i, e.key, btns.length);
      if (next === null) return;
      e.preventDefault();
      choose(items[next].key);
      btns[next].focus();
    });
  });
  paint();
  list.append(...btns);
  return list;
}
