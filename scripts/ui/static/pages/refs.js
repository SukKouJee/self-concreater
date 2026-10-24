// 레퍼런스 목록 화면 (보기 전용)
//   #/refs        카테고리의 영상 카드 목록 (필터 · 검색 · 정렬은 받은 목록에 클라이언트에서 적용)
//   #/refs/<id>   영상 하나의 상세 (섹션 흐름 · 장면 프레임 · 본문 · md 경로)
// api 호출은 ctx.api를 쓴다: 페이지별 ctx.api가 이미 ctx.signal(이동하면 abort)을 실어 보낸다. 중단된 요청(error "aborted")은 조용히 무시한다.
// 파일을 쓰지 않는다. 모든 비동기 뒤에는 ctx.isCurrent()로 화면이 아직 이 페이지인지 확인한다.
import { h, filesUrl } from "/static/dom.js";

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

function roleBadge(role) {
  const text = ROLE_TEXT[role] || (role ? String(role) : "");
  return text ? h("span", { class: `badge role-${String(role).replace(/[^\w-]/g, "_")}` }, text) : null;
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
    "aria-label": `섹션 흐름: ${segs.map((s) => s.label).join(" → ")}`,
  });
  // Position by start: a gap before a section becomes a left margin; overlaps are clamped to the previous end.
  let cursor = 0;
  for (const s of segs) {
    const from = Math.max(num(s.start), cursor);
    const gap = Math.min(100, (Math.max(0, from - cursor) / total) * 100);
    const width = Math.max(0, Math.min(100, ((num(s.end) - from) / total) * 100));
    const text = `${s.label} ${fmtRange(s)}`;
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
  return { el, params, ctx, seq: 0, videos: [], skipped: 0, nodes: {}, countTimer: null };
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

function loadError(title, e, retryFn) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) retryFn(); });
  S.el.replaceChildren(
    h("h1", {}, title),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry,
      S.params && S.params.id ? h("a", { class: "btn", href: "#/refs" }, "← 목록") : null),
  );
}

// ---- list ---------------------------------------------------------------------

async function loadList() {
  const mine = S;
  const seq = ++mine.seq;
  if (!mine.ctx.category) { noCategory("레퍼런스 목록"); return; }
  loading("레퍼런스 목록");
  try {
    const data = await mine.ctx.api("GET", `/api/refs?category=${encodeURIComponent(mine.ctx.category)}`);
    if (!alive(mine) || seq !== mine.seq) return;
    mine.videos = Array.isArray(data.videos) ? data.videos : [];
    mine.skipped = num(data.skipped);
    renderList();
  } catch (e) {
    if (!alive(mine) || seq !== mine.seq || ignorable(e)) return;
    loadError("레퍼런스 목록", e, loadList);
  }
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
  S.el.replaceChildren();
  S.el.append(h("h1", {}, "레퍼런스 목록"), h("p", { class: "lead" }, countText()));
  if (S.skipped > 0) {
    S.el.append(h("p", { class: "warn-text", role: "status" }, `읽지 못한 md ${S.skipped}개(프런트매터 확인)`));
  }
  if (S.videos.length === 0) {
    S.el.append(h("section", { class: "card empty" },
      h("h2", {}, "아직 영상이 없습니다"),
      h("p", { class: "muted" }, "링크를 분석하면 이 카테고리의 영상이 여기에 모입니다."),
      h("a", { class: "btn btn-primary", href: "#/add" }, "레퍼런스 추가로 가기")));
    return;
  }
  S.el.append(toolbar());
  S.nodes.result = h("p", { class: "muted small", role: "status", "aria-live": "polite" });
  S.nodes.grid = h("ul", { class: "ref-grid", "aria-label": "영상 목록" });
  S.nodes.empty = h("p", { class: "muted", hidden: true }, "조건에 맞는 영상이 없습니다");
  S.el.append(S.nodes.result, S.nodes.grid, S.nodes.empty);
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

  bar.append(group,
    h("div", { class: "field ref-search" }, h("label", { for: "ref-search" }, "제목·채널 검색"), search),
    h("div", { class: "field ref-sort" }, h("label", { for: "ref-sort" }, "정렬"), sort));
  return bar;
}

function renderCards() {
  const list = visibleVideos();
  S.nodes.grid.replaceChildren(...list.map(videoCard));
  S.nodes.empty.hidden = list.length > 0;
  // The live region is updated after typing pauses so screen readers don't announce every keystroke.
  const mine = S;
  clearTimeout(mine.countTimer);
  mine.countTimer = setTimeout(() => {
    if (alive(mine)) mine.nodes.result.textContent = `${list.length}개 표시`;
  }, COUNT_DEBOUNCE_MS);
}

function thumb(v) {
  if (!v.thumb) return h("div", { class: "ref-thumb ref-thumb-none" }, "프레임 없음");
  return h("div", { class: "ref-thumb" }, h("img", { src: filesUrl(v.thumb), alt: "", loading: "lazy" }));
}

function codes(v) {
  const parts = [v.hook_type, v.narrative_type].filter(Boolean);
  return parts.length ? h("p", { class: "ref-codes small muted" }, parts.join(" · ")) : null;
}

function videoCard(v) {
  const meta = [v.channel, num(v.duration_sec) > 0 ? fmtTime(v.duration_sec) : null].filter(Boolean).join(" · ");
  return h("li", { class: "ref-card" },
    h("a", { class: "ref-link", href: refsHref(v.id) },
      thumb(v),
      h("div", { class: "ref-body" },
        h("h2", { class: "ref-title" }, displayTitle(v)),
        meta ? h("p", { class: "ref-meta small muted" }, meta) : null,
        h("div", { class: "ref-badges" }, roleBadge(v.role)),
        codes(v),
        sectionBand(v.sections, v.duration_sec))));
}

// ---- detail --------------------------------------------------------------------

async function loadDetail() {
  const mine = S;
  const seq = ++mine.seq;
  if (!mine.ctx.category) { noCategory("영상"); return; }
  loading("영상");
  try {
    const data = await mine.ctx.api("GET",
      `/api/refs/${encodeURIComponent(mine.params.id)}?category=${encodeURIComponent(mine.ctx.category)}`);
    if (!alive(mine) || seq !== mine.seq) return;
    renderDetail(data);
  } catch (e) {
    if (!alive(mine) || seq !== mine.seq || ignorable(e)) return;
    loadError("영상", e, loadDetail);
  }
}

function renderDetail(data) {
  const v = data.video;
  const meta = [
    v.channel,
    num(v.duration_sec) > 0 ? fmtTime(v.duration_sec) : null,
    ROLE_TEXT[v.role] || v.role,
    v.hook_type ? `훅 ${v.hook_type}` : null,
    v.narrative_type ? `서사 ${v.narrative_type}` : null,
    v.format ? `형식 ${v.format}` : null,
  ].filter(Boolean).join(" · ");

  S.el.replaceChildren(
    h("p", { class: "crumb" }, h("a", { href: "#/refs" }, "← 목록")),
    h("h1", {}, displayTitle(v)),
    meta ? h("p", { class: "meta-line muted" }, meta) : null,
    v.url ? h("p", {}, h("a", { href: v.url, target: "_blank", rel: "noopener noreferrer" }, "유튜브에서 보기")) : null,
  );

  const sections = sortByStart((v.sections || []).filter((s) => s && s.label));
  if (sections.length) {
    S.el.append(h("section", { class: "ref-block", "aria-labelledby": "ref-sec-title" },
      h("h2", { id: "ref-sec-title" }, "섹션 흐름"),
      sectionBand(sections, v.duration_sec),
      h("ul", { class: "ref-sec-list" }, ...sections.map((s) =>
        h("li", {}, h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" }),
          `${s.label}${s.sub ? `(${s.sub})` : ""} ${fmtRange(s)}`)))));
  }

  S.el.append(framesBlock(data.frames || []));

  for (const part of data.body || []) {
    const block = h("section", { class: "ref-block" });
    if (part.heading) block.append(h("h2", {}, part.heading));
    const text = h("div", { class: "md-text" });
    text.textContent = part.text || "";
    block.append(text);
    S.el.append(block);
  }

  S.el.append(pathRow(data.md));
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
  if (S) clearTimeout(S.countTimer);
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
  loadList();
}
