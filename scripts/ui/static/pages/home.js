import { h, filesUrl, fill } from "/static/dom.js";
import {
  NEXT_REASON, STEP_NAMES, VISIBLE_VIDEOS, isFolded, nextButton, nextHeadline, recentName, stageLine, stepSr, stepState, videoMeta,
} from "/static/home-utils.js";

const MENU_ROUTES = { add: "#/add", conti: "#/conti", typify: "#/typify", draft: "#/draft", diagnose: "#/diagnose" };
const MENU_NAMES = { add: "레퍼런스", conti: "콘티", typify: "유형화", draft: "기획안", diagnose: "진단" };
const KIND_LABELS = { conti: "콘티", report: "리포트", draft: "기획안" };
const MISSION_STATUS = { done: "제출함", skipped: "건너뜀", replaced: "교체됨", pending: "진행 전", active: "진행 중" };
// Category stage tags. The diagnose count is every diagnosis record of the category, not per video: "진단 기록".
const CAT_TAG_NAMES = { collect: "모으기", typify: "유형화", draft: "기획안", conti: "콘티", diagnose: "진단 기록" };
const TAG_SR = { done: " (완료)", now: " (지금 단계)", todo: " (아직)" };
// Where focus goes after the home reloads (filmed toggle, reload, category switch): { next: true } or { path }.
// mount() takes it out first thing, so a key never survives a navigation away.
let focusAfter = null;

// The only way this page makes a primary button. Exactly one is on screen at a time; call sites are
// the init screen (no work folder, shown instead of the home) and nextCard's four branches
// (video link, video filmed toggle, category, new draft), which are mutually exclusive.
function primary(tag, attrs, text) {
  return h(tag, { ...attrs, class: "btn btn-primary" }, text);
}

// ---- filmed toggle ------------------------------------------------------

// Lock -> POST -> reload the home. draft_changed shows a warning (with a reload button) in `warnHost`.
// Every control that toggles this video's filmed state (next-action button, step button, row-end button).
function filmPeers(view, path) {
  return [...view.el.querySelectorAll("[data-film-path]")].filter((b) => b.dataset.filmPath === path);
}

async function toggleFilmed(btn, video, filmed, warnHost, view) {
  const peers = filmPeers(view, video.path);
  if (!peers.includes(btn)) peers.push(btn);
  for (const b of peers) b.disabled = true;
  try {
    await view.ctx.api("POST", "/api/drafts/filmed",
      { category: view.category, path: video.path, filmed, hash: video.hash });
    if (!view.ctx.isCurrent()) return;
    focusAfter = view.focusKey;
    mount(view.el, view.params, view.ctx);
  } catch (e) {
    if (!view.ctx.isCurrent()) return;
    // draft_changed: the loaded hash is stale, so every toggle of this video stays locked until a reload
    const changed = e && e.error === "draft_changed";
    if (!changed) for (const b of peers) b.disabled = false;
    if (changed) {
      const reload = h("button", { type: "button", class: "btn btn-sm" }, "다시 불러오기");
      reload.addEventListener("click", () => {
        if (!view.ctx.isCurrent()) return;
        focusAfter = view.focusKey;
        mount(view.el, view.params, view.ctx);
      });
      fill(warnHost, h("p", { class: "vid-warn-text", role: "alert" }, e.message), reload);
      warnHost.hidden = false;
    } else {
      view.ctx.toast(e && e.message ? e.message : String(e), "error");
    }
  }
}

// ---- 1. next action -----------------------------------------------------

function nextCard(cat, view) {
  const card = h("section", { class: "card home-next", "aria-labelledby": "home-next-k" });
  const body = h("div", { class: "home-next-body" }, h("h2", { id: "home-next-k", class: "home-next-k" }, "다음 할 일"));
  const warn = h("div", { class: "vid-warn", hidden: true });
  const na = cat.next_action || { kind: "new_draft" };
  let action = null;
  if (na.kind === "video") {
    const video = (cat.videos || []).find((v) => v.path === na.path);
    body.append(h("p", { class: "home-next-t" }, nextHeadline(na.title || (video && video.title) || na.path, na.step)),
      h("p", { class: "home-next-why" }, NEXT_REASON[na.step] || ""));
    const nb = nextButton(na.step, na.path);
    if (nb && nb.href) action = primary("a", { href: nb.href }, nb.text);
    else if (nb && nb.action === "film" && video) {
      action = primary("button", { type: "button", "data-film-path": video.path }, nb.text);
      action.addEventListener("click", () => toggleFilmed(action, video, true, warn, { ...view, focusKey: { next: true } }));
    }
  } else if (na.kind === "category") {
    const allDone = (cat.videos || []).length > 0;
    body.append(h("p", { class: "home-next-t" }, na.text),
      h("p", { class: "home-next-why" }, allDone ? "만들고 있는 영상은 모두 마쳤어요. 카테고리 단계를 이어가요"
        : "카테고리 단계를 이어가요"));
    const route = MENU_ROUTES[na.menu];
    if (route) action = primary("a", { href: route }, `${MENU_NAMES[na.menu]} 열기`);
  } else {
    body.append(h("p", { class: "home-next-t" }, "다음 영상을 기획할 차례예요"),
      h("p", { class: "home-next-why" }, "카테고리 단계와 만들고 있는 영상을 모두 마쳤어요"));
    action = primary("a", { href: "#/draft/new" }, "새 기획안 만들기");
  }
  if (action) action.dataset.homeNext = "1";
  fill(card, body, action, warn);
  return card;
}

// ---- 2. videos in progress ------------------------------------------------

function stepItem(step, video, warn, view) {
  const state = stepState(step, video.next);
  const cls = `vid-step vid-step-${state}`;
  const dot = h("span", { class: "vid-dot", "aria-hidden": "true" }, state === "done" ? "✓" : "");
  const name = h("span", { class: "vid-step-name" }, STEP_NAMES[step.key] || step.key);
  if (step.key === "filmed") {
    const btn = h("button", {
      type: "button", class: "vid-film", "aria-pressed": step.done ? "true" : "false",
      "aria-label": `${video.title} 촬영함` + (state === "next" ? ", 다음 단계" : ""), "data-film-path": video.path,
    }, dot, name);
    btn.addEventListener("click", () =>
      toggleFilmed(btn, video, !step.done, warn, { ...view, focusKey: { path: video.path } }));
    return h("li", { class: cls }, btn);
  }
  return h("li", { class: cls }, dot, name, h("span", { class: "sr-only" }, `, ${stepSr(state)}`));
}

function videoRow(video, view) {
  const warn = h("div", { class: "vid-warn", hidden: true });
  const head = h("div", { class: "vid-head" },
    h("a", { class: "vid-title", href: `#/draft/view/${encodeURIComponent(video.path)}`, title: video.title }, video.title),
    videoMeta(video) ? h("p", { class: "vid-meta" }, videoMeta(video)) : null);
  const steps = h("ol", { class: "vid-steps", "aria-label": `${video.title} 단계` },
    ...video.steps.map((s) => stepItem(s, video, warn, view)));
  let end;
  const nb = nextButton(video.next, video.path);
  if (!nb) end = h("span", { class: "vid-complete" }, "완료");
  else if (nb.href) end = h("a", { class: "btn btn-sm", href: nb.href, "aria-label": `${video.title} ${nb.text}` }, nb.text);
  else {
    end = h("button", { type: "button", class: "btn btn-sm", "aria-label": `${video.title} ${nb.text}`,
      "data-film-path": video.path }, nb.text);
    end.addEventListener("click", () => toggleFilmed(end, video, true, warn, { ...view, focusKey: { path: video.path } }));
  }
  return h("li", { class: "vid-row" + (video.next ? "" : " vid-row-complete") }, head, steps, h("div", { class: "vid-end" }, end), warn);
}

function videosCard(cat, view) {
  const videos = cat.videos || [];
  const done = videos.filter((v) => !v.next).length;
  const count = videos.length ? `${videos.length}개` + (done ? ` · 완료 ${done}개` : "") : null;
  const card = h("section", { class: "card", "aria-labelledby": "home-videos-h" },
    h("h2", { id: "home-videos-h", class: "card-title" }, "만들고 있는 영상", count && h("span", { class: "muted" }, count)));
  if (videos.length === 0) {
    card.append(h("p", { class: "muted" }, "아직 기획안이 없어요. 기획안을 만들면 영상마다 어느 단계까지 왔는지 여기에 나와요."));
    return card;
  }
  const list = h("ul", { class: "vid-list" }, ...videos.slice(0, VISIBLE_VIDEOS).map((v) => videoRow(v, view)));
  card.append(list);
  const rest = videos.slice(VISIBLE_VIDEOS);
  if (rest.length) {
    const more = h("button", { type: "button", class: "btn btn-sm vid-more" }, `${rest.length}개 더 보기`);
    more.addEventListener("click", () => {
      const rows = rest.map((v) => videoRow(v, view));
      list.append(...rows);
      more.remove();
      rows[0].querySelector(".vid-title").focus();
    });
    card.append(more);
  }
  return card;
}

// ---- 3. category stage line ----------------------------------------------

function catTagText(step) {
  const name = CAT_TAG_NAMES[step.key] || step.label || step.key;
  return Number.isInteger(step.count) ? `${name} ${step.count}` : name;
}

// Tags + stage text. `inButton` uses spans only (a button may hold phrasing content, not lists).
function catLineParts(cat, inButton) {
  const tags = cat.steps.map((s) => {
    const state = s.done ? "done" : s.key === cat.current ? "now" : "todo";
    return h(inButton ? "span" : "li", { class: `cat-tag cat-tag-${state}` }, catTagText(s),
      h("span", { class: "sr-only" }, TAG_SR[state]));
  });
  const box = inButton ? h("span", { class: "cat-tags" }, ...tags)
    : h("ol", { class: "cat-tags", "aria-label": `${cat.category} 단계` }, ...tags);
  return [box, h("span", { class: "cat-stage" }, stageLine(cat))];
}

function categoryLine(cat) {
  return h("section", { class: "card cat-line", "aria-label": `${cat.category} 카테고리 단계` },
    h("h2", { class: "cat-name" }, cat.category), ...catLineParts(cat, false));
}

// Other categories: one line each; pressing it switches the selected category (the home reloads).
function otherCategories(cats, ctx) {
  if (cats.length === 0) return null;
  const list = h("ul", { class: "cat-others" }, ...cats.map((cat) => {
    const btn = h("button", { type: "button", class: "cat-line cat-line-btn" },
      h("span", { class: "cat-name" }, cat.category), ...catLineParts(cat, true),
      h("span", { class: "sr-only" }, " — 이 카테고리 보기"));
    btn.addEventListener("click", () => { focusAfter = { next: true }; ctx.setCategory(cat.category); });
    return h("li", {}, btn);
  }));
  return h("section", { class: "home-others", "aria-labelledby": "home-others-h" },
    h("h2", { id: "home-others-h", class: "home-others-h" }, "다른 카테고리"), list);
}

// ---- 4. mission / recent ------------------------------------------------

function missionCard(m) {
  const card = h("article", { class: "card" });
  card.append(h("h2", {}, "오늘 미션"));
  if (!m || !m.exists) {
    card.append(h("p", { class: "muted" }, "오늘 미션이 없습니다 — 미션 화면에서 받을 수 있어요"));
  } else {
    const parts = [m.date, m.kind, MISSION_STATUS[m.status] || m.status].filter(Boolean);
    card.append(h("p", {}, parts.join(" · ")));
  }
  card.append(h("div", { class: "btn-row" }, h("a", { class: "btn btn-sm", href: "#/mission" }, "미션 화면 열기")));
  return card;
}

function recentCard(items, categories) {
  const titles = {};
  for (const c of categories || []) for (const v of c.videos || []) titles[v.path] = v.title;
  const card = h("article", { class: "card" });
  card.append(h("h2", {}, "최근 결과물"));
  if (!items || items.length === 0) {
    card.append(h("p", { class: "muted" }, "아직 결과물이 없습니다."));
    return card;
  }
  const ul = h("ul", { class: "recent" });
  for (const it of items) {
    const name = recentName(it, titles);
    const label = KIND_LABELS[it.kind] || it.kind;
    const link = it.kind === "draft"
      ? h("a", { href: `#/draft/view/${encodeURIComponent(it.path)}` }, name)
      : it.kind === "report" ? h("a", { href: "#/report" }, name)
      : h("a", { href: filesUrl(it.path), target: "_blank", rel: "noopener" }, name);
    const when = new Date(it.mtime * 1000).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
    ul.append(h("li", {}, h("span", { class: "tag" }, label), link, h("time", { class: "muted" }, when)));
  }
  card.append(ul);
  return card;
}

// ---- mount --------------------------------------------------------------

// The step toggle of `path` (expanding "더 보기" if that row is folded), else the next-action button, else the h1.
function findFilmStep(el, path) {
  return [...el.querySelectorAll(".vid-film")].find((b) => b.dataset.filmPath === path) || null;
}

function restoreFocus(el, target, videos) {
  if (!target) return;
  let node = null;
  if (target.path) {
    node = findFilmStep(el, target.path);
    const more = el.querySelector(".vid-more");
    if (!node && more && isFolded(videos, target.path)) {
      more.click();
      node = findFilmStep(el, target.path);
    }
  }
  node = node || el.querySelector("[data-home-next]");
  if (!node) {
    node = el.querySelector("h1");
    if (node) node.setAttribute("tabindex", "-1");
  }
  if (node) node.focus();
}

export async function mount(el, params, ctx) {
  const pendingFocus = focusAfter;
  focusAfter = null;
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
      h("p", { class: "muted" }, "지금 연 폴더를 작업 폴더로 만들면 레퍼런스와 기획안이 여기에 저장돼요."));
    const btn = primary("button", { type: "button" }, "이 폴더를 yt-coach 작업 폴더로 만들기");
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
  let videos = [];
  if (st.categories.length === 0) {
    el.append(h("section", { class: "card" },
      h("h2", {}, "카테고리가 없습니다"),
      h("p", { class: "muted" }, "Claude에게 “처음 설정해줘”라고 말하면 첫 카테고리를 함께 만들어요.")));
  } else {
    const cat = st.categories.find((c) => c.category === ctx.category) || st.categories[0];
    const view = { el, params, ctx, category: cat.category };
    videos = cat.videos;
    // `.home` is the size container: its cards and rows switch layout by the column width (menu, help panel)
    el.append(h("div", { class: "home" }, nextCard(cat, view), videosCard(cat, view), categoryLine(cat),
      otherCategories(st.categories.filter((c) => c !== cat), ctx)));
  }
  el.append(h("div", { class: "grid-2 home-sub" }, missionCard(st.mission), recentCard(st.recent, st.categories)));
  restoreFocus(el, pendingFocus, videos);
}
