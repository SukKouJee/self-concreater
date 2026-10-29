// Pure helpers of the video workspace (#/videos, #/video/<id>[/<tab>], #/video/<id>/shoot). No imports and no DOM,
// so node tests load this file directly.
// A video is one draft: its id is the draft path under drafts/ without ".md" (sub folders keep their "/"). The id is
// one hash segment, so the router encodes it (encodeURIComponent turns "/" into %2F).

// Workspace tabs. 촬영 체크 is "check": "#/video/<id>/shoot" is the phone shoot mode, not a tab.
export const TABS = [
  { key: "plan", label: "기획안" },
  { key: "conti", label: "콘티" },
  { key: "check", label: "촬영 체크" },
  { key: "diagnose", label: "진단" },
];
const TAB_KEYS = TABS.map((t) => t.key);

export function videoId(path) {
  return String(path || "").replace(/^drafts\//, "").replace(/\.md$/i, "");
}

export function draftPath(id) {
  return `drafts/${id}.md`;
}

export function videoHref(id, tab) {
  return `#/video/${encodeURIComponent(id)}${tab ? `/${tab}` : ""}`;
}

function decode(text) {
  try { return decodeURIComponent(text); } catch { return null; }
}

// "#/videos" -> list; "#/video/<id>[/<tab>]" -> one video (unknown tab: null = the default tab);
// "#/video/<id>/shoot" -> shoot mode; a missing or undecodable id -> list; any other route -> null.
export function parseVideoHash(hash) {
  const [route] = String(hash || "").split("?");
  const parts = route.replace(/^#\/?/, "").split("/");
  if (parts[0] === "videos") return { mode: "list" };
  if (parts[0] !== "video") return null;
  const id = parts[1] ? decode(parts[1]) : null;
  if (!id) return { mode: "list" };
  if (parts[2] === "shoot") return { mode: "shoot", id };
  return { mode: "video", id, tab: TAB_KEYS.includes(parts[2]) ? parts[2] : null };
}

const isDraft = (p) => /^drafts\/.+\.md$/i.test(p) && !/-conti\.md$/i.test(p);

// Old routes (bookmarks, links inside the hosted pages) -> the new route, or null when the hash is not an old route.
//   #/draft, #/conti, #/diagnose (no target)            -> #/videos
//   #/draft/view/<draft> | #/draft?path=<draft>         -> #/video/<id>/plan
//   #/conti/<draft or conti> | #/conti?path=…           -> #/video/<id>/conti
//   #/diagnose/<draft> | #/diagnose?path=<draft>        -> #/video/<id>/diagnose
//   #/library                                           -> #/refs?tab=library
// #/draft/new… stays (new draft screen); #/diagnose/<own uploaded video> stays (that video has no workspace).
export function legacyRoute(hash) {
  const [route, query] = String(hash || "").split("?");
  const parts = route.replace(/^#\/?/, "").split("/");
  const head = parts[0];
  const fromQuery = () => new URLSearchParams(query || "").get("path");
  if (head === "library") return "#/refs?tab=library";
  if (head === "draft") {
    if (parts[1] === "new") return null;
    const path = parts[1] === "view" ? decode(parts.slice(2).join("/")) : fromQuery();
    return path && isDraft(path) ? videoHref(videoId(path), "plan") : "#/videos";
  }
  if (head === "conti") {
    const raw = parts.length > 1 && parts[1] ? decode(parts.slice(1).join("/")) : fromQuery();
    const path = raw ? raw.replace(/-conti\.md$/i, ".md") : null;
    return path && isDraft(path) ? videoHref(videoId(path), "conti") : "#/videos";
  }
  if (head === "diagnose") {
    const raw = parts.length > 1 && parts[1] ? parts.slice(1).join("/") : null;
    const path = raw !== null ? decode(raw) : fromQuery();
    if (!path) return "#/videos";
    return isDraft(path) ? videoHref(videoId(path), "diagnose") : null;
  }
  return null;
}

// Six stages (spec 4.2 / 7). 다듬기 and 편집 are optional (status.py VIDEO_STEPS).
const STAGES = [
  { key: "draft", label: "기획안" },
  { key: "polish", label: "다듬기", optional: true },
  { key: "conti", label: "콘티" },
  { key: "filmed", label: "촬영" },
  { key: "captions", label: "편집", optional: true },
  { key: "diagnose", label: "진단" },
];

// draft: { conti, filmed_at } (missing key = unknown). extras: { polish, captions, diagnose } booleans and/or
// `steps` from /api/status videos[] (explicit booleans win). Unknown stages show "–" (skip).
// state: done | now (first required stage not done) | skip (unknown, or an optional stage passed by) | todo.
export function stages(draft, extras = {}) {
  const d = draft || {};
  const x = extras || {};
  const fromSteps = {};
  for (const s of Array.isArray(x.steps) ? x.steps : []) if (s && s.key) fromSteps[s.key] = !!s.done;
  const pick = (key, own) => (own !== undefined ? !!own : key in fromSteps ? fromSteps[key] : undefined);
  const done = {
    draft: true,
    polish: pick("polish", x.polish),
    conti: pick("conti", "conti" in d ? d.conti : undefined),
    filmed: pick("filmed", "filmed_at" in d ? d.filmed_at : undefined),
    captions: pick("captions", x.captions),
    diagnose: pick("diagnose", x.diagnose),
  };
  const nowAt = STAGES.findIndex((s) => !s.optional && done[s.key] === false);
  return STAGES.map((s, i) => {
    let state = "todo";
    if (done[s.key] === true) state = "done";
    else if (i === nowAt) state = "now";
    else if (done[s.key] === undefined || (s.optional && (nowAt === -1 || i < nowAt))) state = "skip";
    return { key: s.key, label: s.label, state };
  });
}

const MARK = { done: " ✓", skip: " –", now: " ●", todo: "" };
export function stageText(stage) {
  return stage.label + (MARK[stage.state] || "");
}

const ACTIONS = {
  conti: { label: "콘티 만들기", kind: "conti" },
  filmed: { label: "폰 촬영 모드", kind: "shoot" },
  diagnose: { label: "진단하기", kind: "diagnose" },
};

// The progress card's right-side action for the current stage.
export function nextAction(list) {
  const now = (list || []).find((s) => s.state === "now");
  return (now && ACTIONS[now.key]) || { label: "모든 단계를 마쳤어요", kind: "done" };
}

// Tab opened by "#/video/<id>" without a tab: 진단 once the video is filmed, else 콘티.
export function defaultTab(list) {
  const now = (list || []).find((s) => s.state === "now");
  return now && now.key === "diagnose" ? "diagnose" : "conti";
}

// Target length: "목표 45초" under a minute, whole minutes from 60 up ("목표 15분").
function targetText(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) return "";
  const s = Math.max(1, Math.round(sec));
  return s < 60 ? `목표 ${s}초` : `목표 ${Math.round(s / 60)}분`;
}

// Header meta from the draft front matter: "10월 25일 시작 · 목표 15분", the source motif card chip (when the
// source is a motif card) and the archetype chip. cards: /api/motif cards; archetypes: /api/typify applied list.
export function videoMeta(draft, cards, archetypes) {
  const d = draft || {};
  const parts = [];
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d.date || ""));
  if (m) parts.push(`${Number(m[2])}월 ${Number(m[3])}일 시작`);
  const target = targetText(d.target_sec);
  if (target) parts.push(target);
  const card = d.source ? (cards || []).find((c) => c && c.id === d.source) : null;
  const arch = d.archetype ? (archetypes || []).find((a) => a && String(a.id) === String(d.archetype)) : null;
  return {
    when: parts.join(" · "),
    motif: card ? { label: [card.type_ko, card.title].filter(Boolean).join(" · "), kind: card.type || "" } : null,
    type: d.archetype ? { label: `유형 ${d.archetype}` + (arch && arch.name ? ` · ${arch.name}` : "") } : null,
  };
}

// Breadcrumb title: up to `max` characters, then "…".
export function shortTitle(title, max = 24) {
  const t = String(title || "").trim();
  return t.length > max ? `${t.slice(0, max).trimEnd()}…` : t;
}

function clock(sec) {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

// Tab bar summary from GET /api/conti: "6컷 · 15:00 · 평균 컷 3.0초" ("" without cuts).
export function contiSummary(conti) {
  const cuts = conti && Array.isArray(conti.cuts) ? conti.cuts : [];
  if (!cuts.length) return "";
  const total = typeof conti.total_sec === "number" ? conti.total_sec : cuts.reduce((n, c) => n + Number(c.sec || 0), 0);
  return `${cuts.length}컷 · ${clock(total)} · 평균 컷 ${(total / cuts.length).toFixed(1)}초`;
}

// "촬영 0 / 6컷". Per-cut checks come with the shoot mode; until then the draft-level 다 찍음 counts every cut.
export function shootLine(count, filmed) {
  return `촬영 ${filmed ? count : 0} / ${count}컷`;
}
