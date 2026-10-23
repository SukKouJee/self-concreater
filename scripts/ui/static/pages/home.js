import { h, filesUrl } from "/static/dom.js";

const STEP_LABELS = { collect: "모으기", typify: "유형화", draft: "기획안", conti: "콘티", diagnose: "진단" };
const MENU_ROUTES = { add: "#/add", conti: "#/conti", typify: "#/soon/typify", draft: "#/soon/draft", diagnose: "#/soon/diagnose" };
const MENU_NAMES = { add: "레퍼런스 추가", conti: "콘티", typify: "유형화", draft: "기획안", diagnose: "진단" };
const KIND_LABELS = { conti: "콘티", report: "리포트", draft: "기획안" };
const MISSION_STATUS = { done: "제출함", skipped: "건너뜀", replaced: "교체됨", pending: "진행 전", active: "진행 중" };

function stepBar(cat) {
  const ol = h("ol", { class: "steps", "aria-label": `${cat.category} 진행 단계` });
  for (const step of cat.steps) {
    const current = step.key === cat.current;
    const state = step.done ? "done" : current ? "current" : "todo";
    const li = h("li", { class: `step step-${state}`, "aria-current": current ? "step" : false });
    li.append(
      h("span", { class: "step-dot", "aria-hidden": "true" }, step.done ? "✓" : ""),
      h("span", { class: "step-label" }, STEP_LABELS[step.key] || step.label),
      h("span", { class: "step-detail" }, step.detail),
      h("span", { class: "sr-only" }, step.done ? " (완료)" : current ? " (지금 단계)" : " (아직)"),
    );
    ol.append(li);
  }
  return ol;
}

function categoryCard(cat, selected) {
  const card = h("article", { class: "card" + (selected ? " card-selected" : "") });
  const head = h("div", { class: "card-head" });
  head.append(h("h2", {}, cat.category));
  card.append(head);
  const lines = (cat.stage_text || "").split("\n").map((l) => l.replace(/^\s*-\s*/, "").trim()).filter(Boolean);
  if (lines.length) {
    card.append(h("ul", { class: "stage-notes" }, ...lines.map((l) => h("li", {}, l))));
  }
  card.append(stepBar(cat));
  if (cat.next) {
    const row = h("div", { class: "next-row" });
    row.append(h("p", { class: "next-text" }, cat.next.text));
    const route = MENU_ROUTES[cat.next.menu];
    if (route) {
      row.append(h("a", { class: "btn btn-primary", href: route }, `${MENU_NAMES[cat.next.menu]} 열기`));
    }
    card.append(row);
  } else {
    card.append(h("p", { class: "muted" }, "모든 단계를 한 번씩 거쳤습니다."));
  }
  return card;
}

function missionCard(m) {
  const card = h("article", { class: "card" });
  card.append(h("h2", {}, "오늘 미션"));
  if (!m || !m.exists) {
    card.append(h("p", { class: "muted" }, "오늘 미션이 없습니다 — ", h("code", {}, "/yt-coach:yt-mission")));
  } else {
    const parts = [m.date, m.kind, MISSION_STATUS[m.status] || m.status].filter(Boolean);
    card.append(h("p", {}, parts.join(" · ")), h("p", { class: "muted" }, m.path || ""));
  }
  return card;
}

function recentCard(items) {
  const card = h("article", { class: "card" });
  card.append(h("h2", {}, "최근 결과물"));
  if (!items || items.length === 0) {
    card.append(h("p", { class: "muted" }, "아직 결과물이 없습니다."));
    return card;
  }
  const ul = h("ul", { class: "recent" });
  for (const it of items) {
    const name = it.path.split("/").pop();
    const label = KIND_LABELS[it.kind] || it.kind;
    const link = it.kind === "draft"
      ? h("a", { href: `#/conti/${encodeURIComponent(it.path)}` }, name)
      : h("a", { href: filesUrl(it.path), target: "_blank", rel: "noopener" }, name);
    const when = new Date(it.mtime * 1000).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
    ul.append(h("li", {}, h("span", { class: "tag" }, label), link, h("time", { class: "muted" }, when)));
  }
  card.append(ul);
  return card;
}

export async function mount(el, params, ctx) {
  el.replaceChildren(h("h1", {}, "홈"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  let st;
  try {
    st = await ctx.api("GET", "/api/status");
  } catch (e) {
    if (!ctx.isCurrent()) return;
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", () => { if (ctx.isCurrent()) mount(el, params, ctx); });
    el.replaceChildren(h("h1", {}, "홈"), h("p", { class: "error", role: "alert" }, e.message), retry);
    return;
  }
  if (!ctx.isCurrent()) return;
  el.replaceChildren(h("h1", {}, "홈"));
  if (!st.initialized) {
    const card = h("section", { class: "card" });
    card.append(h("h2", {}, "아직 작업 폴더가 아닙니다"),
      h("p", { class: "muted" }, st.work_root));
    const btn = h("button", { type: "button", class: "btn btn-primary" }, "이 폴더를 yt-coach 작업 폴더로 만들기");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await ctx.api("POST", "/api/init", {});
        if (!ctx.isCurrent()) return;
        ctx.toast("작업 폴더를 만들었습니다", "ok");
        await ctx.refreshCategories();
        if (!ctx.isCurrent()) return;
        mount(el, params, ctx);
      } catch (e) {
        if (!ctx.isCurrent()) return;
        btn.disabled = false;
        ctx.toast(e.message, "error");
      }
    });
    card.append(btn);
    el.append(card);
    return;
  }
  if (st.categories.length === 0) {
    el.append(h("section", { class: "card" },
      h("h2", {}, "카테고리가 없습니다"),
      h("p", { class: "muted" }, "터미널에서 ", h("code", {}, "/yt-coach:yt-init"), " 으로 첫 카테고리를 만드세요.")));
  }
  const sorted = [...st.categories].sort((a, b) => (b.category === ctx.category) - (a.category === ctx.category));
  for (const cat of sorted) el.append(categoryCard(cat, cat.category === ctx.category));
  const grid = h("div", { class: "grid-2" }, missionCard(st.mission), recentCard(st.recent));
  el.append(grid);
}
