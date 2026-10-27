// 레퍼런스 화면 (추가 + 보기 + 라벨 고치기)
//   #/refs        머리(제목 · 개수 · "+ 링크 추가") + 펼치는 추가 칸(pages/add.js) + 카테고리의 영상 카드 목록
//                 (필터 · 검색 · 정렬은 받은 목록에 클라이언트에서 적용). #/add는 같은 화면을 추가 칸을 펼친 채 연다.
//   #/refs/<id>   영상 하나의 상세 (섹션 흐름 · 장면 프레임 · 본문 · md 경로) + 섹션 라벨·유형 고치기
// 고치기는 POST /api/refs/<id>/labels 한 곳만 쓴다(프런트매터 라벨만 저장). 고치는 중 다른 화면으로 가면 고친 값은 경고 없이 버린다.
// api 호출은 ctx.api를 쓴다: 페이지별 ctx.api가 이미 ctx.signal(이동하면 abort)을 실어 보낸다. 중단된 요청(error "aborted")은 조용히 무시한다.
// 모든 비동기 뒤에는 ctx.isCurrent()로 화면이 아직 이 페이지인지 확인한다.
import { h, fill, filesUrl } from "/static/dom.js";
import {
  bandCaption, bodySubName, fmtCut, formatName, hookName, narrativeName, sectionLabelName, sectionName, topCamera,
} from "/static/labels.js";
import { renderMarkdown } from "/static/md.js";
import { mountAddArea } from "/static/pages/add.js";

const COUNT_DEBOUNCE_MS = 300;
const ROLE_TEXT = { ref: "레퍼런스", mine: "내 영상" };
const FILTERS = [
  { key: "ref", label: "레퍼런스" },
  { key: "mine", label: "내 영상" },
  { key: "all", label: "전체" },
];
const SORTS = [
  { key: "added", label: "최근 추가순" },
  { key: "length", label: "길이순" },
  { key: "title", label: "제목순" },
];

// ---- helpers ----------------------------------------------------------------

const refsHref = (id) => `#/refs/${encodeURIComponent(id)}`;
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const displayTitle = (v) => String(v.title || v.id);
const sortByStart = (list) => [...list].sort((a, b) => num(a.start) - num(b.start));

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(num(sec)));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtRange(s) {
  return `${fmtTime(s.start)}–${fmtTime(s.end)}`;
}

// Unknown roles (a hand-edited md) are shown as is.
function roleText(role) {
  return ROLE_TEXT[role] || (role ? String(role) : "");
}

function reviewedBadge() {
  return h("span", { class: "badge reviewed", title: "사용자가 라벨을 직접 고친 영상입니다" }, "직접 고침");
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function ignorable(e) {
  return !e || e.error === "aborted";
}

// ---- section band (shared by list cards and detail) -------------------------------

// Segment widths are percentages of the video length (or of the last section end when the length is unknown).
function sectionBand(sections, duration) {
  const segs = sortByStart((sections || []).filter((s) => s && typeof s.label === "string" && s.label));
  if (segs.length === 0) return null;
  const total = num(duration) > 0 ? num(duration) : Math.max(...segs.map((s) => num(s.end)));
  if (!(total > 0)) return null;
  const band = h("div", {
    class: "sec-band", role: "img",
    "aria-label": `섹션 흐름: ${segs.map((s) => sectionLabelName(s.label)).join(" → ")}`,
  });
  // Position by start: a gap before a section becomes a left margin; overlaps are clamped to the previous end.
  let cursor = 0;
  for (const s of segs) {
    const from = Math.max(num(s.start), cursor);
    const gap = Math.min(100, (Math.max(0, from - cursor) / total) * 100);
    const width = Math.max(0, Math.min(100, ((num(s.end) - from) / total) * 100));
    const text = `${sectionName(s.label, s.sub)} ${fmtRange(s)}`;
    const seg = h("span", { class: `lb-${s.label}`, title: text, "aria-label": text });
    seg.style.width = `${width}%`;
    if (gap > 0) seg.style.marginLeft = `${gap}%`;
    band.append(seg);
    cursor = Math.max(cursor, num(s.end));
  }
  return band;
}

// ---- page state ---------------------------------------------------------------

let S = null;

// Filter / search / sort survive unmount (list -> detail -> list) and reset only when the category changes.
const VIEW = { category: null, role: "ref", query: "", sort: "added" };

function syncView(category) {
  if (VIEW.category === category) return;
  VIEW.category = category;
  VIEW.role = "ref";
  VIEW.query = "";
  VIEW.sort = "added";
}

function freshState(el, params, ctx) {
  syncView(ctx.category);
  return {
    el, params, ctx, seq: 0, videos: [], skipped: 0, nodes: {}, countTimer: null, trashing: false,
    shell: null, area: null, areaOpen: !!(params && params.add), areaActive: false, autoOpenFor: null, autoOpened: false, shotsPending: 0,
  };
}

function loading(title) {
  S.el.replaceChildren(h("h1", {}, title), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
}

function noCategory(title) {
  S.el.replaceChildren(h("h1", {}, title),
    h("section", { class: "card" },
      h("p", {}, "카테고리가 아직 없습니다. 홈에서 작업 폴더와 카테고리를 먼저 만드세요."),
      h("a", { class: "btn", href: "#/home" }, "홈으로")));
}

function loadError(title, e, retryFn, into = null) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) retryFn(); });
  fill(into || S.el,
    into ? null : h("h1", {}, title),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry,
      S.params && S.params.id ? h("a", { class: "btn", href: "#/refs" }, "← 목록") : null),
  );
}

// ---- shell: head (title, count, add toggle) + add area + list ------------------

function teardownShell() {
  if (S.area) S.area.unmount();
  S.area = null;
  S.shell = null;
}

function ensureShell() {
  if (S.shell) return;
  const mine = S;
  S.el.replaceChildren();
  const count = h("p", { class: "lead" });
  const toggle = h("button", {
    type: "button", class: "btn btn-primary add-toggle", "aria-expanded": String(mine.areaOpen), "aria-controls": "add-area",
  }, "+ 링크 추가");
  const areaEl = h("section", { class: "card add-area", id: "add-area", "aria-label": "링크 추가" });
  const listEl = h("div", { class: "refs-list" });
  S.el.append(
    h("div", { class: "page-head" }, h("div", { class: "page-head-text" }, h("h1", {}, "레퍼런스"), count), toggle),
    areaEl, listEl);
  S.shell = { count, toggle, areaEl, listEl };
  S.area = mountAddArea(areaEl, mine.ctx, {
    open: mine.areaOpen,
    onSaved: () => { if (alive(mine)) loadList({ quiet: true }); },
    onChange: ({ open, active }) => {
      if (!alive(mine)) return;
      const finished = mine.areaActive && !active;
      mine.areaOpen = open;
      mine.areaActive = active;
      toggle.setAttribute("aria-expanded", String(open));
      if (finished) loadList({ quiet: true });
    },
  });
  toggle.addEventListener("click", () => {
    if (!alive(mine) || !mine.area) return;
    mine.autoOpened = false;
    if (mine.areaOpen) mine.area.close(); else mine.area.open();
  });
}

// ---- list ---------------------------------------------------------------------

// quiet: keep the cards while the new list loads (after saving a video in the add area).
async function loadList(opts = {}) {
  const mine = S;
  const seq = ++mine.seq;
  if (!mine.ctx.category) { teardownShell(); noCategory("레퍼런스"); return; }
  ensureShell();
  if (!opts.quiet) fill(mine.shell.listEl, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const data = await mine.ctx.api("GET", `/api/refs?category=${encodeURIComponent(mine.ctx.category)}`);
    if (!alive(mine) || seq !== mine.seq) return;
    mine.videos = Array.isArray(data.videos) ? data.videos : [];
    mine.skipped = num(data.skipped);
    renderList();
    openWhenEmpty();
    loadShotsPending();
  } catch (e) {
    if (!alive(mine) || seq !== mine.seq || ignorable(e)) return;
    loadError("레퍼런스", e, () => loadList(), mine.shell.listEl);
  }
}

// A category without any reference starts with the add area open (once per category, without taking focus).
function openWhenEmpty() {
  const cat = S.ctx.category;
  if (S.autoOpenFor === cat) return;
  S.autoOpenFor = cat;
  if (!S.videos.some((v) => v.role === "ref")) {
    if (!S.areaOpen && S.area) { S.area.open({ focus: false }); S.autoOpened = true; }
  } else if (S.autoOpened) {
    // Opened by itself for an empty category: fold it away again unless a job is running in it.
    S.autoOpened = false;
    if (S.areaOpen && !S.areaActive && S.area) S.area.close();
  }
}

async function loadShotsPending() {
  const mine = S;
  const cat = mine.ctx.category;
  try {
    const data = await mine.ctx.api("GET", `/api/add/shots-pending?category=${encodeURIComponent(cat)}`);
    if (!alive(mine) || mine.ctx.category !== cat) return;
    mine.shotsPending = num(data.count);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.shotsPending = 0;
  }
  paintShotsButton();
}

function paintShotsButton() {
  const btn = S.nodes.shots;
  if (!btn || !btn.isConnected) return;
  btn.hidden = !(S.shotsPending > 0);
  btn.textContent = `샷 분류 보강 (${S.shotsPending}개)`;
}

function countText() {
  const refs = S.videos.filter((v) => v.role === "ref").length;
  const mineN = S.videos.filter((v) => v.role === "mine").length;
  return `레퍼런스 ${refs} · 내 영상 ${mineN}`;
}

function visibleVideos() {
  const q = VIEW.query.trim().toLowerCase();
  const list = S.videos.filter((v) => {
    if (VIEW.role !== "all" && v.role !== VIEW.role) return false;
    if (!q) return true;
    return `${v.title || ""} ${v.channel || ""}`.toLowerCase().includes(q);
  });
  const byTitle = (a, b) => displayTitle(a).localeCompare(displayTitle(b), "ko");
  if (VIEW.sort === "length") list.sort((a, b) => num(b.duration_sec) - num(a.duration_sec) || byTitle(a, b));
  else if (VIEW.sort === "title") list.sort(byTitle);
  else list.sort((a, b) => String(b.added || "").localeCompare(String(a.added || "")) || byTitle(a, b));
  return list;
}

function renderList() {
  const box = S.shell.listEl;
  S.shell.count.textContent = countText();
  box.replaceChildren();
  if (S.skipped > 0) {
    box.append(h("p", { class: "warn-text", role: "status" }, `읽지 못한 md ${S.skipped}개(프런트매터 확인)`));
  }
  if (S.videos.length === 0) {
    box.append(h("section", { class: "card empty" },
      h("h2", {}, "아직 영상이 없습니다"),
      h("p", { class: "muted" }, "링크를 분석하면 이 카테고리의 영상이 여기에 모입니다.")));
    return;
  }
  box.append(toolbar());
  S.nodes.result = h("p", { class: "muted small", role: "status", "aria-live": "polite" });
  S.nodes.grid = h("ul", { class: "ref-grid", "aria-label": "영상 목록" });
  S.nodes.empty = h("p", { class: "muted", hidden: true }, "조건에 맞는 영상이 없습니다");
  box.append(S.nodes.result, S.nodes.grid, S.nodes.empty);
  renderCards();
}

function toolbar() {
  const bar = h("div", { class: "ref-tools" });

  const group = h("div", { class: "ref-filters", role: "group", "aria-label": "역할 필터" });
  for (const f of FILTERS) {
    const btn = h("button", {
      type: "button", class: "btn", "aria-pressed": String(VIEW.role === f.key), "data-role": f.key,
    }, f.label);
    btn.addEventListener("click", () => {
      VIEW.role = f.key;
      for (const b of group.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.role === VIEW.role));
      renderCards();
    });
    group.append(btn);
  }

  const search = h("input", { type: "search", id: "ref-search", autocomplete: "off" });
  search.value = VIEW.query;
  search.addEventListener("input", () => { VIEW.query = search.value; renderCards(); });

  const sort = h("select", { id: "ref-sort" }, ...SORTS.map((o) => h("option", { value: o.key }, o.label)));
  sort.value = VIEW.sort;
  sort.addEventListener("change", () => { VIEW.sort = sort.value; renderCards(); });

  const shots = h("button", { type: "button", class: "btn ref-shots", hidden: true }, "샷 분류 보강");
  shots.addEventListener("click", async () => {
    const mine = S;
    if (!alive(mine) || !mine.area || shots.disabled) return;
    shots.disabled = true;
    const ok = await mine.area.startShotsOnly();
    if (!alive(mine)) return;
    shots.disabled = false;
    if (ok) { mine.shotsPending = 0; paintShotsButton(); }
  });
  S.nodes.shots = shots;

  bar.append(group,
    h("div", { class: "field ref-search" }, h("label", { for: "ref-search" }, "제목·채널 검색"), search),
    h("div", { class: "field ref-sort" }, h("label", { for: "ref-sort" }, "정렬"), sort),
    shots);
  return bar;
}

function renderCards() {
  const list = visibleVideos();
  S.nodes.grid.replaceChildren(...list.map((v, i) => videoCard(v, i)));
  S.nodes.empty.hidden = list.length > 0;
  // The live region is updated after typing pauses so screen readers don't announce every keystroke.
  const mine = S;
  clearTimeout(mine.countTimer);
  mine.countTimer = setTimeout(() => {
    if (alive(mine)) mine.nodes.result.textContent = `${list.length}개 표시`;
  }, COUNT_DEBOUNCE_MS);
}

// Thumbnail with chips on top: format and (when the list mixes roles) role at top-left, "직접 고침" at top-right,
// length at bottom-right. Chips are plain text so screen readers read them with the card link.
function thumb(v) {
  const pic = v.thumb
    ? h("img", { src: filesUrl(v.thumb), alt: "", loading: "lazy" })
    : h("span", { class: "ref-thumb-empty" }, "프레임 없음");
  const tl = [formatName(v.format), VIEW.role === "all" ? roleText(v.role) : ""].filter(Boolean);
  // A space before each chip keeps the card's description readable ("롱폼 레퍼런스 20:28", not "롱폼레퍼런스20:28");
  // whitespace between flex items takes no room.
  const chip = (text, cls = "ref-chip") => [" ", h("span", { class: cls }, text)];
  return h("div", { class: "ref-thumb" }, pic,
    tl.length ? h("span", { class: "ref-ov ref-ov-tl" }, ...tl.flatMap((t) => chip(t))) : null,
    v.reviewed ? h("span", { class: "ref-ov ref-ov-tr" }, ...chip("직접 고침", "ref-chip ref-chip-ok")) : null,
    num(v.duration_sec) > 0 ? h("span", { class: "ref-ov ref-ov-br" }, ...chip(fmtTime(v.duration_sec))) : null);
}

function typeTags(v) {
  const tags = [["훅", hookName(v.hook_type)], ["서사", narrativeName(v.narrative_type)]].filter(([, name]) => name);
  if (!tags.length) return null;
  return h("p", { class: "ref-tags" }, ...tags.map(([k, name]) => h("span", { class: "ref-tag" }, h("span", { class: "ref-tag-k" }, k), name)));
}

function stats(v) {
  const cut = fmtCut(v.avg_cut_sec);
  const cam = topCamera(v.camera);
  const cells = [
    cut ? ["평균 컷", cut] : null,
    cam ? ["카메라", `${cam.label} ${cam.pct}%`] : null,
  ].filter(Boolean);
  if (!cells.length) return null;
  return h("dl", { class: "ref-stats" }, ...cells.map(([k, val]) => h("div", { class: "ref-stat" }, h("dt", {}, k), h("dd", {}, val))));
}

function bandWithCaption(v) {
  const band = sectionBand(v.sections, v.duration_sec);
  if (!band) return null;
  const caption = bandCaption(v.sections);
  if (caption) band.setAttribute("aria-hidden", "true");   // the caption below reads the same flow in words
  return h("div", { class: "ref-flow" }, band, caption ? h("p", { class: "ref-flow-cap" }, caption) : null);
}

// The link is named by the title alone (screen-reader link lists stay scannable); the chips and the rest of the
// card (channel, tags, numbers, flow caption) are its description. Both wrappers are display: contents, so the
// layout is the same as without them. Ids come from the card index: video ids are file stems and need no escaping.
function videoCard(v, i) {
  const tid = `ref-card-${i}`;
  return h("li", { class: "ref-card" },
    h("a", { class: "ref-link", href: refsHref(v.id), "aria-labelledby": tid, "aria-describedby": `${tid}-d ${tid}-b` },
      h("div", { id: `${tid}-d`, class: "ref-desc" }, thumb(v)),
      h("div", { class: "ref-body" },
        h("h2", { class: "ref-title", id: tid }, displayTitle(v)),
        h("div", { id: `${tid}-b`, class: "ref-desc" },
          v.channel ? h("p", { class: "ref-meta" }, v.channel) : null,
          typeTags(v),
          stats(v),
          bandWithCaption(v)))));
}

// ---- detail --------------------------------------------------------------------

// quiet: keep the current DOM until the new detail arrives and restore the scroll position (used after saving).
// focus: move focus to the section heading once rendered (the element that had focus was replaced).
async function loadDetail(opts = {}) {
  const mine = S;
  const seq = ++mine.seq;
  if (!mine.ctx.category) { noCategory("영상"); return; }
  const scrollY = window.scrollY;
  if (!opts.quiet) loading("영상");
  try {
    const data = await mine.ctx.api("GET",
      `/api/refs/${encodeURIComponent(mine.params.id)}?category=${encodeURIComponent(mine.ctx.category)}`);
    if (!alive(mine) || seq !== mine.seq) return;
    renderDetail(data);
    if (opts.quiet) window.scrollTo(0, scrollY);
    if (opts.focus) {
      const title = document.getElementById("ref-sec-title");
      if (title) title.focus({ preventScroll: true });
    }
  } catch (e) {
    if (!alive(mine) || seq !== mine.seq || ignorable(e)) return;
    loadError("영상", e, () => loadDetail());
  }
}

function renderDetail(data) {
  const v = data.video;
  const meta = [
    v.channel,
    num(v.duration_sec) > 0 ? fmtTime(v.duration_sec) : null,
    ROLE_TEXT[v.role] || v.role,
    v.hook_type ? `훅 ${hookName(v.hook_type)}` : null,
    v.narrative_type ? `서사 ${narrativeName(v.narrative_type)}` : null,
    v.format ? `형식 ${formatName(v.format)}` : null,
  ].filter(Boolean).join(" · ");

  fill(S.el,
    h("p", { class: "crumb" }, h("a", { href: "#/refs" }, "← 목록")),
    h("h1", {}, displayTitle(v)),
    meta ? h("p", { class: "meta-line muted" }, meta) : null,
    v.reviewed ? h("p", { class: "ref-badges" }, reviewedBadge()) : null,
    v.url ? h("p", {}, h("a", { href: v.url, target: "_blank", rel: "noopener noreferrer" }, "유튜브에서 보기")) : null,
  );

  const sections = sortByStart((v.sections || []).filter((s) => s && s.label));
  if (sections.length) S.el.append(sectionBlock(data, sections));

  S.el.append(framesBlock(data.frames || []));

  for (const part of data.body || []) {
    const block = h("section", { class: "ref-block" });
    if (part.heading === "섹션") {
      const note = "라벨을 고치면 위 섹션 목록이 기준이 됩니다. 이 표는 바뀌지 않습니다";
      block.append(h("div", { class: "ref-block-head" }, h("h2", {}, part.heading),
        h("span", { class: "badge", title: note }, "분석 당시 기록")));
      block.append(h("p", { class: "muted small" }, note));
    } else if (part.heading) block.append(h("h2", {}, part.heading));
    block.append(h("div", { class: "md" }, renderMarkdown(part.text || "")));
    S.el.append(block);
  }

  const trash = trashControls(v);
  S.el.append(h("div", { class: "btn-row" }, trash.button), trash.panel, pathRow(data.md));
}

// ---- delete (moves to workspace/trash/) ------------------------------------------

// "레퍼런스 삭제" button + inline confirm, shown and hidden in place so focus stays put.
function trashControls(v) {
  const mine = S;
  const what = v.role === "mine" ? "내 영상" : "레퍼런스";
  const whatObj = v.role === "mine" ? "내 영상을" : "레퍼런스를";
  const button = h("button", { type: "button", class: "btn", "aria-expanded": "false", "aria-controls": "ref-trash" },
    `${what} 삭제`);
  const yes = h("button", { type: "button", class: "btn btn-danger" }, "삭제");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  const panel = h("div", { class: "inline-confirm", id: "ref-trash", hidden: true },
    h("p", { class: "warn-text" },
      `이 ${what}의 md와 분석 데이터(프레임·컷·샷 분류·자막)를 휴지통 폴더(workspace/trash/)로 옮깁니다`),
    h("p", { class: "muted hint" },
      "기법 모음에서도 이 영상 출처를 뺍니다. 다른 카테고리에 같은 영상이 있으면 분석 데이터는 그대로 둡니다. 잘못 지웠으면 휴지통 폴더 안의 파일을 같은 경로로 다시 옮기면 됩니다."),
    h("div", { class: "btn-row" }, yes, no));
  const open = (on) => {
    panel.hidden = !on;
    button.setAttribute("aria-expanded", String(on));
    (on ? yes : button).focus();
  };
  button.addEventListener("click", () => open(panel.hidden));
  no.addEventListener("click", () => open(false));
  yes.addEventListener("click", async () => {
    if (mine.trashing) return;
    mine.trashing = true;
    for (const b of [button, yes, no]) b.disabled = true;
    try {
      const res = await mine.ctx.api("POST",
        `/api/refs/${encodeURIComponent(v.id)}/trash?category=${encodeURIComponent(mine.ctx.category)}`, {});
      const notes = [`${whatObj} 휴지통 폴더로 옮겼습니다: ${res.trash}`];
      if (res.typified) notes.push("유형화에 들어간 영상이라, 유형화를 다시 하면 결과에서 빠집니다");
      if (res.techniques_stale) notes.push("기법 모음(techniques.md)에서는 출처를 빼지 못했습니다");
      mine.ctx.toast(notes.join(" · "), "info");     // even if the user already left
      if (!alive(mine)) return;
      location.hash = "#/refs";      // hashchange remounts the list page
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      mine.ctx.toast(e.message, "error");
      if (e.error === "video_not_found") { location.hash = "#/refs"; return; }
      mine.trashing = false;
      for (const b of [button, yes, no]) b.disabled = false;
      yes.focus();
    }
  });
  return { button, panel };
}

// ---- section block (view / edit) -----------------------------------------------------

function sectionBlock(data, sections) {
  const v = data.video;
  const block = h("section", { class: "ref-block", "aria-labelledby": "ref-sec-title" });
  const edit = h("button", { type: "button", class: "btn" }, "라벨 고치기");
  edit.addEventListener("click", () => {
    if (!S || !S.ctx.isCurrent()) return;
    const form = editBlock(data, block);
    block.replaceWith(form);
    const first = form.querySelector("select");
    if (first) first.focus();
  });
  block.append(
    h("div", { class: "ref-block-head" }, h("h2", { id: "ref-sec-title", tabindex: "-1" }, "섹션 흐름"), edit),
    sectionBand(sections, v.duration_sec),
    h("ul", { class: "ref-sec-list" }, ...sections.map((s) =>
      h("li", {}, h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" }),
        `${sectionName(s.label, s.sub)} ${fmtRange(s)}`))));
  return block;
}

// Options list that keeps the current value even when the vocabulary does not contain it.
// nameOf: what each option shows (the value stays the English code that is saved).
function optionList(values, current, nameOf = (val) => val) {
  const list = Array.isArray(values) ? values : [];
  const all = current && !list.includes(current) ? [current, ...list] : list;
  return all.map((val) => h("option", { value: val }, nameOf(val)));
}

function selectOf(attrs, options, value) {
  const sel = h("select", attrs, ...options);
  sel.value = value;
  return sel;
}

function editBlock(data, viewBlock) {
  const mine = S;
  const v = data.video;
  const vocab = data.vocab || {};
  const orig = Array.isArray(v.sections) ? v.sections : [];   // md order: the API needs the full array in this order
  // keep: last body sub of the row, restored when the label returns to body
  const draft = orig.map((s) => ({ label: String(s.label || ""), sub: s.sub ? String(s.sub) : "", keep: s.sub ? String(s.sub) : "" }));
  const controls = [];
  const origOf = (i) => ({ label: String(orig[i].label || ""), sub: orig[i].sub ? String(orig[i].sub) : "" });
  const hook = { orig: v.hook_type || "", cur: v.hook_type || "" };
  const narr = { orig: v.narrative_type || "", cur: v.narrative_type || "" };
  let busy = false;

  const block = h("section", { class: "ref-block ref-edit", "aria-labelledby": "ref-sec-title" });
  const band = h("div", { class: "ref-edit-band" });
  const rows = h("ul", { class: "ref-edit-list" });
  const notice = h("div", { class: "ref-edit-notice", tabindex: "-1" });
  const save = h("button", { type: "submit", class: "btn btn-primary" }, "저장");
  const cancel = h("button", { type: "button", class: "btn" }, "취소");

  const effSub = (d) => (d.label === "body" ? d.sub : "");
  const sectionsChanged = () => draft.some((d, i) => {
    const o = origOf(i);
    return d.label !== o.label || effSub(d) !== effSub(o);
  });
  const dirty = () => sectionsChanged() || hook.cur !== hook.orig || narr.cur !== narr.orig;

  function refresh() {
    band.replaceChildren();
    const shown = sectionBand(draft.map((d, i) => ({ ...orig[i], label: d.label })), v.duration_sec);
    if (shown) band.append(shown);
    save.disabled = busy || !dirty();
    cancel.disabled = busy;
    for (const c of controls) c.disabled = busy;
    save.textContent = busy ? "저장 중…" : "저장";
  }

  const items = orig.map((s, i) => ({ ...s, i }));
  for (const item of sortByStart(items)) {
    const d = draft[item.i];
    const dot = h("span", { class: `lb-dot lb-${d.label}`, "aria-hidden": "true" });
    const labelSel = selectOf(
      { class: "label-select", "aria-label": `${fmtRange(item)} 섹션 라벨` },
      optionList(vocab.label, d.label, sectionLabelName), d.label);
    const subSel = selectOf(
      { class: "label-select", "aria-label": `${fmtRange(item)} 본문 세부 유형` },
      [h("option", { value: "" }, "(없음)"), ...optionList(vocab.body_sub, d.sub, bodySubName)], d.sub);
    subSel.hidden = d.label !== "body";
    labelSel.addEventListener("change", () => {
      d.label = labelSel.value;
      if (d.label !== "body") {
        if (d.sub) d.keep = d.sub;
        d.sub = "";
        subSel.value = "";
      } else {
        d.sub = d.keep;
        subSel.value = d.sub;
      }
      subSel.hidden = d.label !== "body";
      dot.className = `lb-dot lb-${d.label}`;
      refresh();
    });
    subSel.addEventListener("change", () => { d.sub = subSel.value; d.keep = d.sub; refresh(); });
    controls.push(labelSel, subSel);
    rows.append(h("li", { class: "ref-edit-row" },
      h("span", { class: "sec-time" }, fmtRange(item)),
      h("span", { class: "sec-pick" }, dot, labelSel, subSel)));
  }

  function typeField(id, text, state, values, nameOf) {
    const opts = optionList(values, state.cur, nameOf);
    if (!state.cur) opts.unshift(h("option", { value: "", disabled: true, selected: true }, "(없음)"));
    const sel = selectOf({ id, class: "label-select" }, opts, state.cur);
    sel.addEventListener("change", () => { state.cur = sel.value; refresh(); });
    controls.push(sel);
    return h("div", { class: "field" }, h("label", { for: id }, text), sel);
  }

  function showError(e) {
    notice.replaceChildren();
    if (e.error === "ref_changed") {
      const again = h("button", { type: "button", class: "btn" }, "다시 불러오기");
      again.addEventListener("click", () => { if (alive(mine)) loadDetail({ quiet: true, focus: true }); });
      notice.append(
        h("p", { class: "warn-text", role: "alert" }, e.message || "화면을 연 뒤 md가 바뀌었습니다"),
        h("div", { class: "btn-row" }, again,
          h("span", { class: "muted small" }, "다시 불러오면 지금 고친 값은 버려집니다")));
    } else {
      notice.append(h("p", { class: "error", role: "alert" }, e.message || "저장하지 못했습니다"));
    }
  }

  const form = h("form", { class: "ref-edit-form", novalidate: "true" });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (busy || !dirty()) return;
    const payload = { hash: data.hash };
    if (sectionsChanged()) {
      payload.sections = draft.map((d) => (d.label === "body" && d.sub ? { label: d.label, sub: d.sub } : { label: d.label }));
    }
    if (hook.cur !== hook.orig) payload.hook_type = hook.cur;
    if (narr.cur !== narr.orig) payload.narrative_type = narr.cur;
    busy = true;
    notice.replaceChildren();
    refresh();
    let saved;
    try {
      saved = await mine.ctx.api("POST",
        `/api/refs/${encodeURIComponent(mine.params.id)}/labels?category=${encodeURIComponent(mine.ctx.category)}`, payload);
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      busy = false;
      refresh();
      showError(e);
      notice.focus();   // the disabled save button lost focus; announce and focus the message
      return;
    }
    if (!alive(mine)) return;
    const note = v.role === "ref" ? " — 다음 분석 때 교정 예시로 쓰입니다" : "";
    const stale = saved && saved.pacing_stale ? " (컷 데이터가 없어 컷 속도는 다시 계산하지 못했습니다)" : "";
    mine.ctx.toast(`저장했습니다${note}${stale}`);
    loadDetail({ quiet: true, focus: true });
  });
  cancel.addEventListener("click", () => {
    if (!alive(mine) || busy) return;
    block.replaceWith(viewBlock);
    const btn = viewBlock.querySelector("button");
    if (btn) btn.focus();
  });

  form.append(rows,
    h("div", { class: "ref-edit-types" },
      typeField("ref-edit-hook", "훅 유형", hook, vocab.hook_type, hookName),
      typeField("ref-edit-narr", "서사 유형", narr, vocab.narrative_type, narrativeName)),
    notice,
    h("div", { class: "btn-row" }, save, cancel));
  block.append(h("div", { class: "ref-block-head" }, h("h2", { id: "ref-sec-title", tabindex: "-1" }, "섹션 흐름 — 라벨 고치기")),
    band, form);
  refresh();
  return block;
}

function framesBlock(frames) {
  const block = h("section", { class: "ref-block", "aria-labelledby": "ref-frames-title" },
    h("h2", { id: "ref-frames-title" }, "장면 프레임"));
  if (frames.length === 0) {
    block.append(h("p", { class: "muted" }, "저장된 프레임이 없습니다"));
    return block;
  }
  const strip = h("ul", { class: "frame-strip", role: "list", tabindex: "0", "aria-label": "장면 프레임 (가로 스크롤)" });
  for (const f of frames) {
    const cap = f.section ? `${fmtTime(f.start)} · ${f.section}` : fmtTime(f.start);
    strip.append(h("li", {}, h("figure", {},
      h("img", { src: filesUrl(f.frame), alt: "", loading: "lazy" }),
      h("figcaption", {}, cap))));
  }
  block.append(strip);
  return block;
}

function pathRow(md) {
  const copy = h("button", { type: "button", class: "btn" }, "경로 복사");
  copy.addEventListener("click", async () => {
    const mine = S;
    try {
      await navigator.clipboard.writeText(md);
      if (alive(mine)) mine.ctx.toast("경로를 복사했습니다");
    } catch {
      if (alive(mine)) mine.ctx.toast("복사하지 못했습니다 — 직접 선택해 복사하세요", "error");
    }
  });
  return h("div", { class: "ref-path" }, h("span", { class: "muted small" }, "md 파일"), h("code", {}, md), copy);
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  S = freshState(el, params, ctx);
  if (params && params.id) await loadDetail();
  else await loadList();
}

export function unmount() {
  if (S) {
    clearTimeout(S.countTimer);
    if (S.area) S.area.unmount();
  }
  S = null;
}

// 상세의 영상은 다른 카테고리에 없을 수 있으므로 어느 쪽이든 목록으로 돌아가 새 카테고리를 그린다.
export function onCategory() {
  if (!S || !S.ctx.isCurrent()) return;
  if (S.params && S.params.id) {
    location.hash = "#/refs";   // hashchange remounts the list page
    return;
  }
  syncView(S.ctx.category);
  S.videos = [];
  S.shotsPending = 0;
  if (S.area) S.area.onCategory(S.ctx.category);
  loadList();
}
