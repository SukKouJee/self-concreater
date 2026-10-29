// 영상 작업 공간 (spec 4.2)
//   #/videos                     내 영상 목록 + "+ 새 영상 시작"(모티브로)
//   #/video/<id>[/<tab>]         한 영상: 브레드크럼, 제목, 메타, 진행 카드, 탭(기획안/콘티/촬영 체크/진단)
//   #/video/<id>/shoot           폰 촬영 모드 (Phase 5가 채운다: 지금은 첫 컷만 보여 주는 작은 화면)
// 탭은 기존 화면 모듈(draft.js 상세, conti.js, diagnose.js)을 그대로 mount/unmount 한다(params.embedded: 머리말은
// 이 페이지가 보여 준다). 같은 영상의 탭 이동은 app.js가 update()로 넘겨 페이지를 다시 만들지 않는다. 떠나기 전 확인은
// unsavedNote()로 탭 모듈에 묻는다(app.js가 묻는 창을 띄운다).
import { api } from "/static/api.js";
import { h, fill, filesUrl } from "/static/dom.js";
import { videoMeta as listMeta } from "/static/home-utils.js";
import { sectionLabelName } from "/static/labels.js";
import { videoProgress } from "/static/menu-utils.js";
import { tabs, tag } from "/static/ui.js";
import {
  TABS, contiSummary, defaultTab, draftPath, nextAction, shootLine, shortTitle, stageText, stages, videoHref, videoId,
  videoMeta,
} from "/static/video-utils.js";

const HOSTED = { plan: "/static/pages/draft.js", conti: "/static/pages/conti.js", diagnose: "/static/pages/diagnose.js" };
const STATE_SR = { done: "완료", now: "지금 단계", skip: "건너뜀 또는 알 수 없음", todo: "아직" };
// Section labels -> ui.js tag colors (hook/intro/body/outro have their own; the rest share the check color).
const SECTION_TAG = { hook: "hook", intro: "intro", body: "body", demo: "body", outro: "outro", cta: "outro" };

let V = null;

function fresh(el, params, ctx) {
  return {
    el, params, ctx, category: ctx.category, tab: null, data: null, stages: [],
    loadSeq: 0, loading: false, host: null, hostSeq: 0, filming: false, nodes: {},
  };
}

const alive = (mine) => V === mine && mine.ctx.isCurrent();
const ignorable = (e) => !e || e.error === "aborted";
const enc = encodeURIComponent;

// ---- data ---------------------------------------------------------------------------------------

// Everything the header needs, from existing APIs only: the draft (/api/draft), its steps and filmed date
// (/api/status), motif card and archetype names for the chips, and the conti (cut count, summary, shoot list).
async function fetchData(mine) {
  const draft = await mine.ctx.api("GET", `/api/draft?path=${enc(draftPath(mine.params.id))}`);
  const category = draft.category || mine.ctx.category;
  const q = `?category=${enc(category || "")}`;
  const none = Promise.resolve(null);
  const [st, mo, ty, co] = await Promise.allSettled([
    category ? mine.ctx.api("GET", `/api/status${q}`) : none,
    category ? mine.ctx.api("GET", `/api/motif${q}`) : none,
    category ? mine.ctx.api("GET", `/api/typify${q}`) : none,
    draft.conti ? mine.ctx.api("GET", `/api/conti?path=${enc(draft.conti)}`) : none,
  ]);
  const ok = (r) => (r.status === "fulfilled" ? r.value : null);
  const cat = ok(st) ? (ok(st).categories || []).find((c) => c.category === category) : null;
  const applied = ok(ty) ? ok(ty).applied : null;
  return {
    category, draft,
    video: cat ? (cat.videos || []).find((v) => v.path === draft.path) || null : null,
    conti: ok(co),
    cards: ok(mo) ? ok(mo).cards || [] : [],
    archetypes: applied ? applied.archetypes || [] : [],
  };
}

// Tab switches and the 다 찍음 toggle re-read only /api/status (steps, filmed date, draft hash), plus the conti when
// asked (its cut list and summary). A changed draft (hash) or a new/removed conti falls back to the full read.
async function fetchLight(mine, withConti) {
  const data = mine.data;
  let st;
  try {
    st = await mine.ctx.api("GET", `/api/status?category=${enc(data.category || "")}`);
  } catch (e) {
    if (ignorable(e)) throw e;
    return fetchData(mine);
  }
  const cat = (st.categories || []).find((c) => c.category === data.category);
  const video = cat ? (cat.videos || []).find((v) => v.path === data.draft.path) || null : null;
  const contiDone = !!video && (video.steps || []).some((s) => s.key === "conti" && s.done);
  if (!video || video.hash !== data.draft.hash || contiDone !== !!data.draft.conti) return fetchData(mine);
  let conti = data.conti;
  if (withConti && data.draft.conti) {
    try { conti = await mine.ctx.api("GET", `/api/conti?path=${enc(data.draft.conti)}`); } catch (e) { if (ignorable(e)) throw e; }
  }
  return { ...data, video, conti };
}

function stagesOf(data) {
  const v = data.video;
  return stages({ conti: data.draft.conti, ...(v ? { filmed_at: v.filmed_at } : {}) }, v ? { steps: v.steps } : {});
}

// The video's category becomes the selected one (its diagnose tab and the sidebar read the selected category).
function adoptCategory(mine, category) {
  mine.category = category;
  if (category && category !== mine.ctx.category) mine.ctx.setCategory(category);
}

const isFilmed = (data) => !!(data.video && data.video.filmed_at);
const cutCount = (data) => (data.conti && Array.isArray(data.conti.cuts) ? data.conti.cuts.length : 0);

// ---- list: #/videos ---------------------------------------------------------------------------------

async function loadList(mine) {
  const cat = mine.category;
  if (!cat) {
    fill(mine.el, h("h1", {}, "내 영상"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", {}, "내 영상"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  const q = `?category=${enc(cat)}`;
  const [st, tg] = await Promise.allSettled([
    mine.ctx.api("GET", `/api/status${q}`), mine.ctx.api("GET", `/api/diagnose/targets${q}`),
  ]);
  if (!alive(mine)) return;
  if (st.status === "rejected") {
    if (ignorable(st.reason)) return;
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", () => { if (alive(mine)) loadList(mine); });
    fill(mine.el, h("h1", {}, "내 영상"),
      h("p", { class: "error", role: "alert" }, st.reason.message || "불러오지 못했습니다"), h("div", { class: "btn-row" }, retry));
    return;
  }
  const entry = (st.value.categories || []).find((c) => c.category === cat);
  const uploads = tg.status === "fulfilled" ? tg.value.videos || [] : [];
  renderList(mine, entry ? entry.videos || [] : [], uploads);
}

function listRow(v) {
  const { pct, stage } = videoProgress(v);
  const meta = listMeta(v);
  return h("li", { class: "videos-row" },
    h("a", { class: "videos-title", href: videoHref(videoId(v.path)) }, v.title),
    meta ? h("span", { class: "videos-meta" }, meta) : null,
    h("span", { class: "vid-prog videos-prog" },
      h("span", { class: "vid-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": String(pct), "aria-valuetext": `${stage} 단계 · ${pct}%`, "aria-label": `${v.title} 진행` },
      h("span", { style: `width: ${pct}%` })),
      h("span", { class: "vid-stage" }, stage)));
}

function renderList(mine, videos, uploads) {
  const make = h("a", { class: "btn btn-primary", href: "#/motif" }, "+ 새 영상 시작");
  const kids = [
    h("div", { class: "videos-head" }, h("h1", {}, "내 영상"), make),
    h("p", { class: "lead" }, `${mine.category} · 영상 ${videos.length}편`),
  ];
  if (videos.length === 0) {
    kids.push(h("div", { class: "ui-card videos-empty" },
      h("p", {}, "아직 영상이 없어요."),
      h("p", { class: "muted" }, "모티브에서 카드를 골라 첫 영상을 시작해요. 기획안을 만들면 여기에 영상이 생겨요.")));
  } else {
    kids.push(h("ul", { class: "videos-list", "aria-label": "영상" }, ...videos.map(listRow)));
  }
  // Own uploaded videos have no workspace (no draft): their diagnosis stays on the diagnose screen.
  if (uploads.length) {
    kids.push(h("section", { class: "videos-uploads", "aria-labelledby": "videos-up-h" },
      h("h2", { id: "videos-up-h" }, "올린 영상 진단"),
      h("ul", { class: "videos-list" }, ...uploads.map((u) => h("li", { class: "videos-row" },
        h("a", { class: "videos-title", href: `#/diagnose/${enc(u.path)}` }, u.title),
        h("span", { class: "videos-meta" }, u.diagnosed ? "진단 있음" : "진단 전"))))));
  }
  fill(mine.el, ...kids);
}

// ---- one video ----------------------------------------------------------------------------------

async function loadVideo(mine) {
  const seq = ++mine.loadSeq;
  mine.loading = true;
  fill(mine.el, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  let data;
  try {
    data = await fetchData(mine);
  } catch (e) {
    if (!alive(mine) || seq !== mine.loadSeq) return;
    mine.loading = false;
    if (!ignorable(e)) renderLoadError(mine, e);
    return;
  }
  if (!alive(mine) || seq !== mine.loadSeq) return;
  mine.loading = false;
  adoptCategory(mine, data.category);
  mine.data = data;
  mine.stages = stagesOf(data);
  mine.tab = mine.params.tab || defaultTab(mine.stages);
  document.title = `${shortTitle(data.draft.title)} · yt-coach`;
  if (mine.params.mode === "shoot") { renderShoot(mine); return; }
  renderWorkspace(mine);
  mountTab(mine, mine.tab);
}

function renderLoadError(mine, e) {
  const crumb = h("nav", { class: "video-crumb", "aria-label": "경로" }, h("a", { href: "#/videos" }, "내 영상"));
  if (e.error === "draft_not_found" || e.error === "bad_path") {
    fill(mine.el, crumb, h("h1", {}, "영상을 찾을 수 없어요"),
      h("p", { class: "muted" }, "기획안이 지워졌거나 이름이 바뀌었어요. 내 영상 목록에서 다시 골라 주세요."));
    return;
  }
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (alive(mine)) loadVideo(mine); });
  fill(mine.el, crumb, h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

function renderWorkspace(mine) {
  const n = mine.nodes = {};
  n.head = h("div", { class: "video-head" });
  n.progress = h("section", { class: "ui-card video-progress", "aria-label": "진행" });
  n.tabs = h("div");
  n.tools = h("div", { class: "video-tools" });
  n.panel = h("div", { class: "video-panel", id: "video-panel", role: "tabpanel" });
  fill(mine.el, n.head, n.progress, h("div", { class: "video-tabbar" }, n.tabs, n.tools), n.panel);
  renderHead(mine);
  renderProgress(mine);
  paintTabs(mine, false);
  renderTools(mine);
}

function renderHead(mine) {
  const d = mine.data.draft;
  const meta = videoMeta(d, mine.data.cards, mine.data.archetypes);
  const chips = [];
  if (meta.motif) chips.push(h("span", { class: `video-chip ui-tag-${meta.motif.kind}` }, meta.motif.label));
  if (meta.type) chips.push(h("span", { class: "video-chip ui-tag-flow" }, meta.type.label));
  fill(mine.nodes.head,
    h("nav", { class: "video-crumb", "aria-label": "경로" },
      h("a", { href: "#/videos" }, "내 영상"), h("span", { "aria-hidden": "true" }, " › "),
      h("span", { "aria-current": "page" }, shortTitle(d.title))),
    h("h1", { class: "video-title" }, String(d.title)),
    meta.when || chips.length ? h("div", { class: "video-meta" }, meta.when ? h("span", {}, meta.when) : null, ...chips) : null);
}

// Progress card: six stages (✓ done, ● now, – skipped/unknown) + the action of the current stage on the right.
function renderProgress(mine) {
  const box = mine.nodes.progress;
  const focusKey = box.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const steps = h("ol", { class: "vp-steps", "aria-label": "진행 단계" }, ...mine.stages.map((s) =>
    h("li", { class: `vp-step is-${s.state}` },
      h("span", { class: "vp-bar", "aria-hidden": "true" }),
      h("span", { class: "vp-label" }, h("span", { "aria-hidden": "true" }, stageText(s)),
        h("span", { class: "sr-only" }, `${s.label}: ${STATE_SR[s.state]}`)))));
  fill(box, steps, h("div", { class: "vp-actions" }, ...progressActions(mine)));
  if (focusKey) { const again = box.querySelector(`[data-focus="${focusKey}"]`); if (again) again.focus(); }
}

function progressActions(mine) {
  const id = mine.params.id;
  const act = nextAction(mine.stages);
  if (act.kind === "conti" || act.kind === "diagnose") {
    return [h("a", { class: "btn btn-rec btn-s", href: videoHref(id, act.kind), "data-focus": act.kind }, act.label)];
  }
  if (act.kind === "shoot") {
    return [
      h("span", { class: "vp-count" }, shootLine(cutCount(mine.data), isFilmed(mine.data))),
      h("a", { class: "btn btn-rec btn-s", href: videoHref(id) + "/shoot", "data-focus": "shoot" }, act.label),
      filmedButton(mine, "card"),
    ];
  }
  return act.kind === "done" ? [h("span", { class: "vp-done" }, act.label)] : [];   // unknown: no action
}

// Draft-level "다 찍음" toggle (/api/drafts/filmed). aria-pressed shows the saved state.
function filmedButton(mine, where) {
  const filmed = isFilmed(mine.data);
  const btn = h("button", { type: "button", class: "btn btn-s", "aria-pressed": String(filmed),
    "data-focus": `filmed-${where}`, disabled: mine.filming }, "다 찍음");
  btn.addEventListener("click", () => setFilmed(mine, !filmed));
  return btn;
}

async function setFilmed(mine, filmed) {
  if (mine.filming) return;
  mine.filming = true;
  for (const b of mine.el.querySelectorAll('[data-focus^="filmed-"]')) b.disabled = true;
  const d = mine.data.draft;
  try {
    await mine.ctx.api("POST", "/api/drafts/filmed", { category: mine.data.category, path: d.path, filmed, hash: d.hash });
    if (!alive(mine)) return;
    mine.ctx.toast(filmed ? "다 찍음으로 표시했어요" : "다 찍음 표시를 지웠어요", "info");
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.ctx.toast(e.message, "error");     // draft_changed: the refresh below takes the new hash
  }
  mine.filming = false;
  await refreshHead(mine, false);
}

// Re-read (light) and repaint the header parts (not the tab panel). Keeps focus on the same control.
async function refreshHead(mine, withConti) {
  const seq = ++mine.loadSeq;
  try {
    const data = await fetchLight(mine, withConti);
    if (!alive(mine) || seq !== mine.loadSeq) return;
    mine.data = data;
    mine.stages = stagesOf(data);
  } catch (e) {
    if (!alive(mine) || seq !== mine.loadSeq || ignorable(e)) return;
    mine.ctx.toast(e.message || "불러오지 못했습니다", "error");
  }
  if (!alive(mine) || !mine.nodes.head) return;
  const focusKey = mine.el.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  renderHead(mine);
  renderProgress(mine);
  renderTools(mine);
  if (mine.tab === "check" && mine.host === null) renderCheck(mine);
  if (focusKey) { const again = mine.el.querySelector(`[data-focus="${focusKey}"]`); if (again) again.focus(); }
}

// Underline tabs (ui.js). Selecting a tab changes the hash; the router hands it to update(). Rebuilt (not toggled)
// when the selection changes from outside (another link, the user stayed after the leave question).
function paintTabs(mine, focus) {
  const id = mine.params.id;
  const list = tabs(TABS, mine.tab, (key) => { location.hash = videoHref(id, key); }, { label: "작업 탭" });
  for (const b of list.querySelectorAll('[role="tab"]')) {
    b.id = `vtab-${b.getAttribute("data-key")}`;
    b.setAttribute("aria-controls", "video-panel");
  }
  mine.nodes.tabs.replaceWith(list);
  mine.nodes.tabs = list;
  if (mine.nodes.panel) mine.nodes.panel.setAttribute("aria-labelledby", `vtab-${mine.tab}`);
  if (focus) { const cur = list.querySelector('[aria-selected="true"]'); if (cur) cur.focus(); }
}

// Right of the tabs: conti summary, 인쇄 (the storyboard HTML), 다시 만들기 (conti tab, existing recreate flow).
function renderTools(mine) {
  const d = mine.data.draft;
  const kids = [];
  const summary = contiSummary(mine.data.conti);
  if (summary) kids.push(h("span", { class: "video-summary" }, summary));
  if (d.html) {
    kids.push(h("a", { class: "btn btn-link btn-s", href: filesUrl(d.html), target: "_blank", rel: "noopener",
      "data-focus": "print" }, "인쇄"));
  }
  if (mine.tab === "conti" && d.conti) {
    const redo = h("button", { type: "button", class: "btn btn-link btn-s", "data-focus": "redo" }, "다시 만들기");
    redo.addEventListener("click", () => {
      const go = () => mountTab(mine, "conti", { recreate: true });
      const note = unsavedNote();
      if (note) mine.ctx.confirmLeave(note, go); else go();
    });
    kids.push(redo);
  }
  fill(mine.nodes.tools, ...kids);
}

// ---- tab panel: hosted pages ----------------------------------------------------------------------

function unmountHost(mine) {
  const host = mine.host;
  mine.host = null;
  mine.hostSeq += 1;
  if (!host) return;
  if (host.mod && typeof host.mod.unmount === "function") {
    try { host.mod.unmount(); } catch { /* ignore page cleanup errors */ }
  }
  host.abort.abort();
  mine.ctx.signal.removeEventListener("abort", host.onAbort);
}

function hostParams(mine, tab, extra) {
  const d = mine.data.draft;
  if (tab === "plan") return { mode: "view", path: d.path, embedded: true };
  if (tab === "conti") return { path: extra.recreate ? d.path : d.conti || d.path, embedded: true, ...extra };
  return { path: d.path, embedded: true };
}

async function mountTab(mine, tab, extra = {}) {
  unmountHost(mine);
  const panel = mine.nodes.panel;
  panel.setAttribute("aria-labelledby", `vtab-${tab}`);
  if (tab === "check") { renderCheck(mine); return; }
  const seq = mine.hostSeq;
  const abort = new AbortController();
  const onAbort = () => abort.abort();
  mine.ctx.signal.addEventListener("abort", onAbort, { once: true });
  const host = { mod: null, abort, onAbort };
  mine.host = host;
  const live = () => alive(mine) && mine.hostSeq === seq;
  // The hosted page gets its own liveness and abort signal: switching tabs stops its late work like leaving would.
  // The tab's category is the video's (a category switch with unsaved input waits for the leave question).
  const hostCtx = {
    get category() { return mine.category || mine.ctx.category; },
    setCategory: mine.ctx.setCategory,
    refreshCategories: mine.ctx.refreshCategories,
    toast: mine.ctx.toast,
    api: (method, path, body) => api(method, path, body, abort.signal),
    signal: abort.signal,
    isCurrent: live,
    confirmLeave: mine.ctx.confirmLeave,
    navigate: mine.ctx.navigate,
  };
  fill(panel, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const mod = await import(HOSTED[tab]);
    if (!live()) return;
    host.mod = mod;
    panel.replaceChildren();
    await mod.mount(panel, hostParams(mine, tab, extra), hostCtx);
    if (live() && extra.recreate) {
      const yes = panel.querySelector("[data-recreate-yes]");
      if (yes) yes.focus();
    }
  } catch (e) {
    if (!live()) return;
    fill(panel, h("p", { class: "error", role: "alert" }, `화면을 열지 못했습니다: ${e && e.message ? e.message : e}`));
  }
}

// 촬영 체크 (this phase: the conti's cuts + the draft-level 다 찍음; per-cut checks come with the shoot mode).
function renderCheck(mine) {
  const panel = mine.nodes.panel;
  const id = mine.params.id;
  const cuts = mine.data.conti && Array.isArray(mine.data.conti.cuts) ? mine.data.conti.cuts : [];
  const head = h("div", { class: "check-head" },
    h("h2", { class: "sr-only" }, "촬영 체크"),
    h("span", { class: "vp-count" }, shootLine(cuts.length, isFilmed(mine.data))),
    filmedButton(mine, "check"),
    cuts.length ? h("a", { class: "btn btn-s", href: videoHref(id) + "/shoot" }, "폰 촬영 모드") : null);
  if (!cuts.length) {
    fill(panel, head, h("div", { class: "ui-card check-empty" },
      h("p", {}, "콘티가 아직 없어요."),
      h("p", { class: "muted" }, "콘티를 만들면 여기에서 찍을 컷을 순서대로 확인해요. ",
        h("a", { href: videoHref(id, "conti") }, "콘티 탭으로 가기"))));
    return;
  }
  const sections = (mine.data.conti.fm && mine.data.conti.fm.script) || [];
  fill(panel, head, h("ol", { class: "check-list", "aria-label": "찍을 컷" }, ...cuts.map((c) => {
    const label = c.label || (sections[c.section] || {}).label || "";
    return h("li", { class: "check-row" },
      h("span", { class: "check-no" }, String(c.n).padStart(2, "0")),
      h("div", { class: "check-body" },
        label ? tag(sectionLabelName(label), SECTION_TAG[label] || "check") : null,
        h("p", { class: "check-action" }, c.action || ""),
        c.line ? h("p", { class: "check-line" }, `“${c.line}”`) : null,
        h("p", { class: "check-shot" }, [c.size, c.sec ? `${c.sec}초` : null].filter(Boolean).join(" · "))));
  })));
}

// ---- shoot mode (minimal; Phase 5 replaces it) ------------------------------------------------------

function renderShoot(mine) {
  const id = mine.params.id;
  const d = mine.data.draft;
  const cuts = mine.data.conti && Array.isArray(mine.data.conti.cuts) ? mine.data.conti.cuts : [];
  const close = h("a", { class: "btn shoot-close", href: videoHref(id, "check"), "aria-label": "촬영 모드 닫기" }, "닫기");
  const top = h("div", { class: "shoot-top" }, close,
    h("div", { class: "shoot-name" }, h("span", { class: "shoot-k" }, "촬영 모드"), h("h1", {}, shortTitle(d.title))),
    cuts.length ? h("span", { class: "shoot-count" }, `1 / ${cuts.length}`) : null);
  const first = cuts[0];
  const body = first
    ? h("div", { class: "shoot-cut" },
      first.label ? tag(sectionLabelName(first.label), SECTION_TAG[first.label] || "check") : null,
      h("h2", { class: "shoot-action" }, first.action || ""),
      first.line ? h("p", { class: "shoot-line" }, `“${first.line}”`) : null,
      first.guide ? h("p", { class: "shoot-guide" }, first.guide) : null)
    : h("p", { class: "shoot-empty" }, "콘티를 만들면 컷이 여기에 나와요. ", h("a", { href: videoHref(id, "conti") }, "콘티 탭으로 가기"));
  fill(mine.el, h("section", { class: "shoot-dark shoot-stub", "aria-label": "촬영 모드" }, top, body));
}

// ---- page contract ------------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  if (V) unmountHost(V);
  V = fresh(el, params || { mode: "list" }, ctx);
  if (V.params.mode === "list") await loadList(V);
  else await loadVideo(V);
}

// Another tab of the same video: switch in place (fresh header data, then the tab's page). Anything else remounts.
export function update(params) {
  const mine = V;
  if (!mine || !params || params.mode !== "video" || mine.params.mode !== "video" || params.id !== mine.params.id) return false;
  // first load still running: it opens params.tab; a failed load: remount (a fresh try)
  if (!mine.data) { if (mine.loading) mine.params = params; return mine.loading; }
  mine.params = params;
  const hadFocus = mine.nodes.tabs.contains(document.activeElement);
  const before = mine.tab;
  mine.tab = params.tab || defaultTab(mine.stages);
  document.title = `${shortTitle(mine.data.draft.title)} · yt-coach`;
  paintTabs(mine, hadFocus);
  renderTools(mine);
  unmountHost(mine);
  fill(mine.nodes.panel, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  const seq = mine.hostSeq;
  // the conti is re-read for the shoot list and after conti edits (summary); the rest comes from /api/status
  refreshHead(mine, mine.tab === "check" || before === "conti").then(() => {
    if (alive(mine) && mine.hostSeq === seq) mountTab(mine, mine.tab);
  });
  return true;
}

export function unsavedNote() {
  const host = V && V.host;
  return host && host.mod && typeof host.mod.unsavedNote === "function" ? host.mod.unsavedNote() : null;
}

// The user stayed after the leave question: the tab list goes back to the open tab.
export function onStay() {
  if (!V || !V.data || !V.nodes.tabs) return;
  paintTabs(V, V.nodes.tabs.contains(document.activeElement));
}

// A video belongs to one category: another category leaves for its video list. With unsaved input the user is asked
// first; staying puts the video's category back in the select and the sidebar.
export function onCategory(category) {
  const mine = V;
  if (!mine || category === mine.category) return;
  if (mine.params.mode === "list") { mine.category = category; loadList(mine); return; }
  const note = unsavedNote();
  if (!note) { mine.ctx.navigate("#/videos"); return; }
  mine.ctx.confirmLeave(note, () => mine.ctx.navigate("#/videos", { force: true }),
    () => { if (alive(mine)) mine.ctx.setCategory(mine.category); });
}

export function unmount() {
  if (V) unmountHost(V);
  V = null;
}
