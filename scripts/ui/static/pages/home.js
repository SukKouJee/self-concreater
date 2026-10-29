// Home (spec 4.1). Desktop: 다음 할 일 (one line + small main button) -> 만들고 있는 영상 (3, 더 보기) -> 오늘 미션 ->
// 최근 결과물 ("영상 제목 · 콘티 · 10월 29일"). Phone (<= 640px): dark 다음 할 일 card with a big red button (촬영 모드
// 시작 when filming is next) -> 오늘 미션 -> three videos (photo, six stage dots, current stage).
// Data: /api/status (+ /api/mission for today's title; a failure leaves the status card's own text). The phone
// layout also reads the conti of the next video (scene count) and of the listed videos (first scene photo).
// The layout follows the width (matchMedia): switching repaints from the loaded data without new requests.
import { h, filesUrl, fill } from "/static/dom.js";
import {
  HOME_ORDER, STEP_NAMES, VISIBLE_VIDEOS, contiOf, isFolded, missionView, nextButton, nextStep, phoneVideos, recentLine,
  shootProgress, stageDots, stageWord, stepSr, stepState, videoMeta,
} from "/static/home-utils.js";
import { shootStore } from "/static/shoot-utils.js";
import { videoHref, videoId } from "/static/video-utils.js";

const PHONE = "(max-width: 640px)";
// Where focus goes after the home reloads (filmed toggle, reload): { next: true } or { path }.
// mount() takes it out first thing, so a key never survives a navigation away.
let focusAfter = null;
let H = null;          // the open home: { el, params, ctx, st, today, media, onMedia, contis: Map(draft path -> conti) }

const alive = (view) => H === view && view.ctx.isCurrent();

// The only way this page makes a desktop main button (the init screen and the desktop next card; one on screen).
function primary(tag, attrs, text) {
  return h(tag, { ...attrs, class: "btn btn-primary" }, text);
}

// ---- filmed toggle (desktop video rows) --------------------------------------

// Every control that toggles this video's filmed state (step button, row-end button).
function filmPeers(view, path) {
  return [...view.el.querySelectorAll("[data-film-path]")].filter((b) => b.dataset.filmPath === path);
}

// Lock -> POST -> reload the home. draft_changed shows a warning (with a reload button) in `warnHost`.
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

// ---- desktop ----------------------------------------------------------------

// 1. 다음 할 일: one line (label, headline, reason) and the small main button.
function nextCard(cat) {
  const step = nextStep(cat);
  const action = primary("a", { href: step.action.href, "data-home-next": "1" }, step.action.label);
  return h("section", { class: "card home-next", "aria-labelledby": "home-next-k" },
    h("div", { class: "home-next-body" },
      h("h2", { id: "home-next-k", class: "home-next-k" }, "다음 할 일"),
      h("p", { class: "home-next-t" }, step.title),
      step.why ? h("p", { class: "home-next-why" }, step.why) : null),
    action);
}

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
    h("a", { class: "vid-title", href: videoHref(videoId(video.path)), title: video.title }, video.title),
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

// 2. 만들고 있는 영상: three rows, the rest behind "N개 더 보기".
function videosCard(cat, view) {
  const videos = cat.videos || [];
  const done = videos.filter((v) => !v.next).length;
  const count = videos.length ? `${videos.length}개` + (done ? ` · 완료 ${done}개` : "") : null;
  const card = h("section", { class: "card", "aria-labelledby": "home-videos-h" },
    h("h2", { id: "home-videos-h", class: "card-title" }, "만들고 있는 영상", count && h("span", { class: "muted" }, count)));
  if (videos.length === 0) {
    card.append(h("p", { class: "muted" }, "아직 영상이 없어요. 모티브 카드를 골라 새 영상을 시작하면 단계가 여기에 나와요."));
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

// 3. 오늘 미션
function missionCard(st, today, phone) {
  const m = missionView(today, st.mission);
  const head = h("div", { class: "home-card-head" }, h("h2", { id: "home-mission-h" }, "오늘 미션"),
    m.meta ? h("span", { class: "home-card-meta" }, m.meta) : null);
  return h("section", { class: `card home-mission${phone ? " home-mission-phone" : ""}`, "aria-labelledby": "home-mission-h" },
    head,
    h("p", { class: "home-mission-t" }, m.title),
    m.text ? h("p", { class: "home-mission-text" }, m.text) : null,
    h("a", { class: "btn home-mission-go", href: "#/mission" }, m.action));
}

// 4. 최근 결과물: "영상 제목 · 콘티 · 10월 29일" (no file names).
function recentCard(items, categories) {
  const titles = {};
  for (const c of categories || []) for (const v of c.videos || []) titles[v.path] = v.title;
  const card = h("section", { class: "card", "aria-labelledby": "home-recent-h" }, h("h2", { id: "home-recent-h" }, "최근 결과물"));
  if (!items || items.length === 0) {
    card.append(h("p", { class: "muted" }, "아직 결과물이 없어요."));
    return card;
  }
  card.append(h("ul", { class: "recent" }, ...items.map((it) => {
    const line = recentLine(it, titles);
    return h("li", {}, h("a", { href: line.href }, line.text));
  })));
  return card;
}

// ---- phone --------------------------------------------------------------------

function phoneNext(cat, view) {
  const step = nextStep(cat);
  const go = h("a", { class: "btn btn-rec home-go", href: step.action.href, "data-home-next": "1" },
    step.film ? h("span", { class: "home-go-rec", "aria-hidden": "true" }) : null, step.action.label);
  let count = null;
  if (step.film && step.path) {
    const bar = h("span", { class: "home-shoot-fill" });
    const text = h("span", { class: "home-shoot-text" });
    count = h("div", { class: "home-shoot", hidden: true },
      h("span", { class: "home-shoot-bar", "aria-hidden": "true" }, bar), text);
    view.shoot = { path: step.path, el: count, fill: bar, text };
  }
  return h("section", { class: "home-next-phone", "aria-labelledby": "home-next-k" },
    h("h2", { id: "home-next-k", class: "home-next-k" }, "다음 할 일"),
    h("p", { class: "home-next-t" }, step.title), count, go);
}

function dots(video) {
  return h("span", { class: "home-dots", "aria-hidden": "true" },
    ...stageDots(video).map((s) => h("span", { class: `home-dot is-${s}` })));
}

function phoneVideoRow(video, view) {
  const thumb = h("span", { class: "home-thumb", "aria-hidden": "true" });
  view.thumbs.set(video.path, thumb);
  const word = stageWord(video);
  return h("li", {}, h("a", { class: "home-pv", href: videoHref(videoId(video.path)), "aria-label": `${video.title}, ${word}` },
    thumb,
    h("span", { class: "home-pv-body" },
      h("span", { class: "home-pv-title" }, video.title), dots(video),
      h("span", { class: "home-pv-stage" + (video.next ? " is-next" : "") }, word))));
}

function phoneVideoList(cat, view) {
  const { shown, total } = phoneVideos(cat.videos);
  const head = h("div", { class: "home-card-head" }, h("h2", { id: "home-videos-h" }, "만들고 있는 영상"),
    total ? h("a", { href: "#/videos" }, `전체 ${total}편`) : null);
  const body = shown.length ? h("ul", { class: "home-pv-list" }, ...shown.map((v) => phoneVideoRow(v, view)))
    : h("p", { class: "muted" }, total ? "만드는 중인 영상이 없어요" : "아직 영상이 없어요");
  return h("section", { class: "home-videos-phone", "aria-labelledby": "home-videos-h" }, head, body);
}

// Conti reads for the phone layout: the next video's scene count, the listed videos' first scene photo.
async function loadContis(view, cat) {
  const want = new Set();
  if (view.shoot) want.add(view.shoot.path);
  for (const v of phoneVideos(cat.videos).shown) {
    if ((v.steps || []).some((s) => s.key === "conti" && s.done)) want.add(v.path);
  }
  await Promise.all([...want].filter((p) => !view.contis.has(p)).map(async (p) => {
    try {
      view.contis.set(p, await view.ctx.api("GET", `/api/conti?path=${encodeURIComponent(contiOf(p))}`));
    } catch {
      view.contis.set(p, null);              // no conti or unreadable: no photo, no count
    }
  }));
  if (!alive(view)) return;
  paintContis(view, cat);
}

function paintContis(view, cat) {
  if (view.shoot) {
    const conti = view.contis.get(view.shoot.path);
    const video = (cat.videos || []).find((v) => v.path === view.shoot.path);
    if (conti && Array.isArray(conti.cuts) && conti.cuts.length) {
      const ids = shootStore(videoId(view.shoot.path)).get();
      const { done, total } = shootProgress(conti.cuts, ids, !!(video && video.filmed_at));
      view.shoot.fill.style.width = `${Math.round((done / total) * 100)}%`;
      view.shoot.text.textContent = `${done} / ${total}장면`;
      view.shoot.el.hidden = false;
    }
  }
  for (const [path, box] of view.thumbs) {
    const conti = view.contis.get(path);
    const cut = conti && Array.isArray(conti.cuts) ? conti.cuts.find((c) => c && c.ref && c.ref.frame) : null;
    if (!cut || box.firstChild) continue;
    const img = h("img", { src: filesUrl(cut.ref.frame), alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.remove());
    box.append(img);
  }
}

// ---- render -----------------------------------------------------------------

function restoreFocus(el, target, videos) {
  if (!target) return;
  let node = null;
  if (target.path) {
    const find = () => [...el.querySelectorAll(".vid-film")].find((b) => b.dataset.filmPath === target.path) || null;
    node = find();
    const more = el.querySelector(".vid-more");
    if (!node && more && isFolded(videos, target.path)) {
      more.click();
      node = find();
    }
  }
  node = node || el.querySelector("[data-home-next]");
  if (!node) {
    node = el.querySelector("h1");
    if (node) node.setAttribute("tabindex", "-1");
  }
  if (node) node.focus();
}

function render(view, pendingFocus) {
  const { el, st, ctx } = view;
  const phone = view.media.matches;
  const cat = st.categories.find((c) => c.category === ctx.category) || st.categories[0];
  const rv = { el, params: view.params, ctx, category: cat ? cat.category : null };
  view.shoot = null;
  view.thumbs = new Map();
  const build = {
    next: () => (phone ? phoneNext(cat, view) : nextCard(cat)),
    videos: () => (phone ? phoneVideoList(cat, view) : videosCard(cat, rv)),
    mission: () => missionCard(st, view.today, phone),
    recent: () => recentCard(st.recent, st.categories),
  };
  const order = HOME_ORDER[phone ? "phone" : "desktop"];
  // `.home` is the size container: desktop rows switch layout by the column width (menu, help panel)
  fill(el, h("h1", { class: phone ? "sr-only" : null }, "홈"),
    h("div", { class: `home${phone ? " home-phone" : ""}` }, ...order.map((key) => (cat || key === "mission" || key === "recent" ? build[key]() : null))));
  if (!cat) el.querySelector(".home").prepend(h("section", { class: "card" }, h("h2", {}, "카테고리가 없습니다"),
    h("p", { class: "muted" }, "Claude에게 “처음 설정해줘”라고 말하면 첫 카테고리를 함께 만들어요.")));
  restoreFocus(el, pendingFocus, cat ? cat.videos : []);
  if (phone && cat) {
    paintContis(view, cat);
    loadContis(view, cat);
  }
}

function initCard(el, params, ctx) {
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
  return card;
}

export async function mount(el, params, ctx) {
  unmount();
  const pendingFocus = focusAfter;
  focusAfter = null;
  el.replaceChildren(h("h1", {}, "홈"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  let st;
  let today;
  try {
    const [status, mission] = await Promise.all([
      ctx.api("GET", "/api/status"),
      ctx.api("GET", "/api/mission").then((m) => (m ? m.today : undefined), () => undefined),
    ]);
    st = status;
    today = mission;
  } catch (e) {
    if (!ctx.isCurrent()) return;
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", () => { if (ctx.isCurrent()) mount(el, params, ctx); });
    el.replaceChildren(h("h1", {}, "홈"), h("p", { class: "error", role: "alert" }, e.message), retry);
    return;
  }
  if (!ctx.isCurrent()) return;
  if (!st.initialized) {
    el.replaceChildren(h("h1", {}, "홈"), initCard(el, params, ctx));
    return;
  }
  const media = window.matchMedia(PHONE);
  const view = { el, params, ctx, st, today, media, contis: new Map(), shoot: null, thumbs: new Map() };
  view.onMedia = () => { if (alive(view)) render(view, null); };
  media.addEventListener("change", view.onMedia);
  H = view;
  render(view, pendingFocus);
}

export function unmount() {
  if (H) H.media.removeEventListener("change", H.onMedia);
  H = null;
}
