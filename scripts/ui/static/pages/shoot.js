// 폰 촬영 모드 #/video/<id>/shoot (spec 4.4, mockup docs/ui-mockups/PhoneShoot.dc.html)
// Dark full screen (app.js hides the header, sidebar and bottom tabs on this route), one scene at a time: the scene
// photo (or the shot on a dark tile), section tag, timecode, 찍음 ✓ badge, action, line, two tips, and ‹ / main / ›.
// Per-scene checks are browser storage (shoot-utils shootStore, spec 7); the last 전부 찍었어요! saves the draft-level
// 다 찍음 (/api/drafts/filmed). The screen stays on (Wake Lock) while open; ←/→ move between scenes.
import { h, fill, filesUrl } from "/static/dom.js";
import { hasRepeats, refCaption } from "/static/conti-utils.js";
import { sectionLabelName } from "/static/labels.js";
import {
  barSegments, doneIndexes, firstTodo, mainLabel, nextTodo, sceneTime, shootCount, shootStore, tipsFrom,
} from "/static/shoot-utils.js";
import { draftPath, shortTitle, videoHref } from "/static/video-utils.js";

let S = null;

const enc = encodeURIComponent;
const alive = (mine) => S === mine && mine.ctx.isCurrent();
const ignorable = (e) => !e || e.error === "aborted";

// ---- small parts ----------------------------------------------------------------------------------

function closeIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ width: "18", height: "18", viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "2.2", "aria-hidden": "true", focusable: "false" })) svg.setAttribute(k, v);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M6 6l12 12M18 6L6 18");
  svg.append(path);
  return svg;
}

function topBar(mine, title, counter) {
  const close = h("a", { class: "shoot-close", href: videoHref(mine.id, "check"), "aria-label": "촬영 모드 닫기" }, closeIcon());
  const count = counter ? h("span", { class: "shoot-count" }) : null;
  if (count) mine.nodes.count = count;
  return h("header", { class: "shoot-top" }, close,
    h("div", { class: "shoot-name" }, h("span", { class: "shoot-k" }, "촬영 모드"), h("h1", {}, title || "")),
    count);
}

function shell(mine, ...kids) {
  return h("section", { class: "shoot shoot-dark", "aria-label": "촬영 모드" }, ...kids);
}

// ---- data -----------------------------------------------------------------------------------------

async function fetchDraft(mine) {
  return mine.ctx.api("GET", `/api/draft?path=${enc(draftPath(mine.id))}`);
}

async function load(mine) {
  fill(mine.el, shell(mine, topBar(mine, "", false), h("p", { class: "shoot-msg", role: "status" }, "불러오는 중…")));
  let draft;
  let conti = null;
  try {
    draft = await fetchDraft(mine);
    if (!alive(mine)) return;
    if (draft.conti) conti = await mine.ctx.api("GET", `/api/conti?path=${enc(draft.conti)}`);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    renderError(mine, e);
    return;
  }
  if (!alive(mine)) return;
  mine.draft = draft;
  mine.cuts = conti && Array.isArray(conti.cuts) ? conti.cuts : [];
  document.title = `촬영 모드 · ${shortTitle(draft.title)} · yt-coach`;
  if (!mine.cuts.length) { renderEmpty(mine); return; }
  mine.i = firstTodo(new Set(doneIndexes(mine.store.get(), mine.cuts)), mine.cuts.length);
  renderShoot(mine);
  lockScreen(mine);
}

function renderError(mine, e) {
  const top = topBar(mine, "", false);
  if (e.error === "draft_not_found" || e.error === "bad_path") {
    fill(mine.el, shell(mine, top, h("div", { class: "shoot-msg" },
      h("h2", {}, "영상을 찾을 수 없어요"),
      h("p", {}, "기획안이 지워졌거나 이름이 바뀌었어요. "), h("a", { href: "#/videos" }, "내 영상으로 가기"))));
    return;
  }
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (alive(mine)) load(mine); });
  fill(mine.el, shell(mine, top, h("div", { class: "shoot-msg" },
    h("p", { role: "alert" }, e.message || "불러오지 못했습니다"), retry)));
}

// No conti (or an empty one): nothing to film yet.
function renderEmpty(mine) {
  fill(mine.el, shell(mine, topBar(mine, shortTitle(mine.draft.title), false), h("div", { class: "shoot-msg" },
    h("h2", {}, "콘티가 아직 없어요"),
    h("p", {}, "콘티를 만들면 찍을 장면이 여기에 순서대로 나와요."),
    h("a", { href: videoHref(mine.id, "conti") }, "콘티 탭으로 가기"))));
}

// ---- the shoot screen -----------------------------------------------------------------------------

const doneSet = (mine) => new Set(doneIndexes(mine.store.get(), mine.cuts));
const REPEATS_NOTE = "같은 장면이 여러 컷으로 나뉘어 있어요. 콘티 탭에서 장면으로 합치면 찍기 편해요.";

function renderShoot(mine) {
  const n = mine.nodes;
  const top = topBar(mine, shortTitle(mine.draft.title), true);
  n.bar = h("div", { class: "shoot-bar", role: "img" });
  n.scene = h("div", { class: "shoot-scene" });
  n.prev = h("button", { type: "button", class: "shoot-side", "aria-label": "이전 장면" }, "‹");
  n.main = h("button", { type: "button", class: "shoot-main" });
  n.next = h("button", { type: "button", class: "shoot-side", "aria-label": "다음 장면" }, "›");
  n.prev.addEventListener("click", () => go(mine, mine.i - 1));
  n.next.addEventListener("click", () => go(mine, mine.i + 1));
  n.main.addEventListener("click", () => onMain(mine));
  n.error = h("p", { class: "shoot-error", role: "alert" });
  n.live = h("p", { class: "sr-only", role: "status", "aria-live": "polite" });
  const note = mine.store.persistent() ? null
    : h("p", { class: "shoot-note" }, "이 브라우저는 체크를 저장할 수 없어요. 창을 닫으면 체크가 사라져요.");
  // an unmerged conti (one cut per line): filming goes better scene by scene
  const repeats = hasRepeats(mine.cuts) ? h("p", { class: "shoot-hint" }, REPEATS_NOTE) : null;
  fill(mine.el, shell(mine, top, n.bar, repeats, n.scene,
    h("div", { class: "shoot-nav" }, n.error, h("div", { class: "shoot-nav-row" }, n.prev, n.main, n.next), note),
    n.live));
  renderScene(mine);
}

function sceneFrame(c) {
  const frame = h("div", { class: "shoot-frame" });
  const shot = [c.size, c.angle].filter(Boolean).join(" · ");
  if (c.ref && c.ref.frame) {
    frame.classList.add("has-photo");
    const img = h("img", { src: filesUrl(c.ref.frame), alt: refCaption(c.ref) || "참고 사진" });
    img.addEventListener("error", () => { img.remove(); frame.classList.remove("has-photo"); });   // tile fallback
    frame.append(img);
  }
  const label = c.label ? sectionLabelName(c.label) : "";
  const time = sceneTime(c);
  const parts = [
    label ? h("span", { class: "shoot-pill shoot-sec" }, label) : null,
    time ? h("span", { class: "shoot-pill shoot-time" }, h("span", { class: "sr-only" }, "구간 "), time) : null,
    shot ? h("span", { class: "shoot-shot" }, shot) : null,
  ];
  frame.append(...parts.filter(Boolean));
  return frame;
}

// A new scene: frame (photo), action, line, tips. Checking and unchecking only repaint the marks (paintMarks), so the
// photo is not reloaded.
function renderScene(mine) {
  const n = mine.nodes;
  const c = mine.cuts[mine.i];
  n.frame = sceneFrame(c);
  n.undoSlot = h("div", { class: "shoot-undo-slot" });
  const kids = [n.frame, n.undoSlot, h("h2", { class: "shoot-action" }, c.action || "")];
  kids.push(c.line
    ? h("p", { class: "shoot-line" }, h("span", { class: "sr-only" }, "대사 "), `“${c.line}”`)
    : h("p", { class: "shoot-line is-empty" }, "(대사 없음)"));
  const tips = tipsFrom(c.guide);
  if (tips.length) kids.push(h("ul", { class: "shoot-tips", "aria-label": "촬영 팁" }, ...tips.map((t) => h("li", {}, t))));
  fill(n.scene, ...kids);
  paintMarks(mine);
}

// Counter, bar, 찍음 ✓ badge, 찍음 지우기, main button and the side buttons for the current scene.
function paintMarks(mine) {
  const n = mine.nodes;
  const total = mine.cuts.length;
  const done = doneSet(mine);
  const c = mine.cuts[mine.i];
  const filmed = done.has(mine.i);
  fill(n.count, h("span", { class: "sr-only" }, "장면 "), `${mine.i + 1} / ${total}`);
  n.bar.setAttribute("aria-label", shootCount(done.size, total));
  fill(n.bar, ...barSegments(done, mine.i, total).map((s) => h("span", { class: `shoot-seg is-${s}` })));
  const badge = n.frame.querySelector(".shoot-badge");
  if (filmed && !badge) n.frame.append(h("span", { class: "shoot-badge" }, "찍음 ✓"));
  if (!filmed && badge) badge.remove();
  const undoHadFocus = n.undoSlot.contains(document.activeElement);
  if (filmed && !n.undoSlot.firstChild) {
    const undo = h("button", { type: "button", class: "btn btn-link btn-s shoot-undo" }, "찍음 지우기");
    undo.addEventListener("click", () => {
      mine.store.set(c.id, false);
      paintMarks(mine);
      n.live.textContent = `${mine.i + 1}번 장면 찍음을 지웠어요`;
    });
    n.undoSlot.append(undo);
  }
  if (!filmed) n.undoSlot.replaceChildren();
  n.main.textContent = mainLabel(done, mine.i, total);
  n.main.classList.toggle("is-done", filmed);
  n.main.setAttribute("aria-disabled", String(mine.busy));   // not disabled: it keeps focus while saving
  n.prev.setAttribute("aria-disabled", String(mine.i === 0));
  n.next.setAttribute("aria-disabled", String(mine.i === total - 1));
  if (undoHadFocus && !filmed) n.main.focus();      // the undo button went away
}

function go(mine, i) {
  if (mine.busy || i < 0 || i >= mine.cuts.length || i === mine.i) return;
  mine.i = i;
  mine.nodes.error.textContent = "";
  renderScene(mine);
  mine.nodes.live.textContent = `장면 ${i + 1} / ${mine.cuts.length}`;
}

function onMain(mine) {
  if (mine.busy) return;
  const total = mine.cuts.length;
  const done = doneSet(mine);
  if (!done.has(mine.i)) {
    mine.store.set(mine.cuts[mine.i].id, true);
    paintMarks(mine);
    mine.nodes.live.textContent = `${mine.i + 1}번 장면 찍음`;
    return;
  }
  const next = nextTodo(done, mine.i, total);
  if (next !== null) { go(mine, next); return; }
  finish(mine);
}

// 전부 찍었어요!: the draft-level 다 찍음 (filmed_at). A draft changed since opening (draft_changed): read its new hash
// and try once more.
async function finish(mine) {
  mine.busy = true;
  mine.nodes.error.textContent = "";
  paintMarks(mine);
  const post = (d) => mine.ctx.api("POST", "/api/drafts/filmed",
    { category: d.category, path: d.path, filmed: true, hash: d.hash });
  try {
    try {
      await post(mine.draft);
    } catch (e) {
      if (!e || e.error !== "draft_changed" || !alive(mine)) throw e;
      mine.draft = await fetchDraft(mine);
      if (!alive(mine)) return;
      await post(mine.draft);
    }
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.busy = false;
    paintMarks(mine);
    mine.nodes.error.textContent = `${e.message || "저장하지 못했어요"} 다시 눌러 주세요.`;
    return;
  }
  if (!alive(mine)) return;
  renderDone(mine);
}

function renderDone(mine) {
  mine.finished = true;
  unlock(mine);
  const head = h("h2", { tabindex: "-1" }, "전부 찍었어요!");
  fill(mine.el, shell(mine, topBar(mine, shortTitle(mine.draft.title), false), h("div", { class: "shoot-msg shoot-done" },
    head,
    h("p", {}, `장면 ${mine.cuts.length}개를 다 찍었어요. 이 영상은 촬영 완료로 표시했어요.`),
    h("a", { class: "btn btn-primary", href: videoHref(mine.id) }, "영상으로 돌아가기"))));
  head.focus();
}

// ---- keys and screen lock ---------------------------------------------------------------------------

function onKey(e) {
  const mine = S;
  if (!mine || !mine.nodes.main || mine.finished || !mine.nodes.main.isConnected) return;
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;   // typing
  if (e.key === "ArrowLeft") { e.preventDefault(); go(mine, mine.i - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); go(mine, mine.i + 1); }
}

async function lockScreen(mine) {
  if (!navigator.wakeLock || document.visibilityState !== "visible" || mine.finished) return;
  if (mine.locking || (mine.lock && !mine.lock.released)) return;     // one request at a time
  mine.locking = true;
  try {
    const lock = await navigator.wakeLock.request("screen");
    if (!alive(mine) || mine.finished) { lock.release().catch(() => {}); return; }
    mine.lock = lock;
  } catch { /* not allowed (battery saver, no user gesture): the screen may dim */ } finally {
    mine.locking = false;
  }
}

function unlock(mine) {
  const lock = mine.lock;
  mine.lock = null;
  if (!lock) return;
  try { lock.release().catch(() => {}); } catch { /* already released */ }
}

// The browser drops the lock when the tab hides: ask again when it comes back.
function onVisible() {
  if (S && S.cuts.length && document.visibilityState === "visible") lockScreen(S);
}

// ---- page contract --------------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  if (S) unmount();
  const mine = S = {
    el, ctx, id: params.id, draft: null, cuts: [], i: 0, store: shootStore(params.id),
    busy: false, finished: false, lock: null, locking: false, nodes: {},
  };
  document.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", onVisible);
  await load(mine);
}

export function unmount() {
  document.removeEventListener("keydown", onKey);
  document.removeEventListener("visibilitychange", onVisible);
  if (S) unlock(S);
  S = null;
}
