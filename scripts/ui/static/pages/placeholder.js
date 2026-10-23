// "준비 중" page for 2차 menu items. params: {title, command, note}
import { h } from "/static/dom.js";

export function mount(el, params, ctx) {
  el.replaceChildren();
  const card = h("section", { class: "card" });
  card.append(h("h1", {}, params.title || "준비 중"));
  card.append(h("p", { class: "lead" }, "준비 중입니다. 지금은 터미널의 Claude Code에서 아래 커맨드로 쓸 수 있습니다."));
  if (params.note) card.append(h("p", { class: "muted" }, params.note));
  if (params.command) {
    const row = h("div", { class: "cmd-row" });
    row.append(h("code", { class: "cmd" }, params.command));
    const btn = h("button", { type: "button", class: "btn" }, "복사");
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(params.command);
        ctx.toast("커맨드를 복사했습니다", "ok");
      } catch {
        ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
      }
    });
    row.append(btn);
    card.append(row);
  }
  el.append(card);
}
