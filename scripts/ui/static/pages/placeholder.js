// "준비 중" page for menu items that have no screen yet (unused now; kept for later). params: {title, note}
import { h } from "/static/dom.js";

export function mount(el, params) {
  el.replaceChildren();
  const card = h("section", { class: "card" });
  card.append(h("h1", {}, params.title || "준비 중"));
  card.append(h("p", { class: "lead" }, "준비 중입니다."));
  if (params.note) card.append(h("p", { class: "muted" }, params.note));
  el.append(card);
}
