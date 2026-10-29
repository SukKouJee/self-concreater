// Pure helpers for the motif screen (no imports: node tests load this file).

// Filter chips in the mockup order: 전체 / ★ / 흐름 / 기법 / 각도 / 조합.
export const FILTERS = [["all", "전체"], ["star", "★"], ["flow", "흐름"], ["technique", "기법"], ["angle", "각도"], ["combo", "조합"]];
const KINDS = ["flow", "technique", "angle", "combo"];
const TAG_KINDS = { flow: "flow", technique: "tech", angle: "angle", combo: "combo" };
const UNTITLED = "제목 없는 영상";

// A sentence ends at . ! ? 。 or … followed by a space or the end of the text (so "1.5배" stays whole).
const SENTENCE_RE = /^.*?[.!?。…](?=\s|$)/u;

// The card's one-line summary: the first sentence of the idea, cut to `max` characters with "…".
// There is no summary field in the motif data (spec 7, 보고 목록).
export function oneLine(text, max = 60) {
  if (typeof text !== "string") return "";
  const flat = text.split(/\s+/).filter(Boolean).join(" ");
  if (!flat) return "";
  const m = flat.match(SENTENCE_RE);
  const first = m ? m[0] : flat;
  const chars = Array.from(first);
  return chars.length > max ? `${chars.slice(0, Math.max(1, max - 1)).join("").trimEnd()}…` : first;
}

const list = (v) => (Array.isArray(v) ? v : []);

export function kindCounts(cards) {
  const out = { all: 0, star: 0, flow: 0, technique: 0, angle: 0, combo: 0 };
  for (const c of list(cards)) {
    if (!c) continue;
    out.all += 1;
    if (c.starred) out.star += 1;
    if (KINDS.includes(c.type)) out[c.type] += 1;
  }
  return out;
}

// `pinned` cards stay visible whatever the filter (a star toggle in flight must not make the card vanish).
export function filterCards(cards, f, pinned = new Set()) {
  const all = list(cards).filter(Boolean);
  if (f === "star") return all.filter((c) => c.starred || pinned.has(c.id));
  if (KINDS.includes(f)) return all.filter((c) => c.type === f || pinned.has(c.id));
  return all;
}

// Motif type -> ui.js tag kind ("" = plain tag).
export const tagKind = (type) => TAG_KINDS[type] || "";

// Evidence videos of a card with their reference frame (from /api/refs; the motif API has no thumbnails).
export function evidenceItems(card, refs) {
  const byId = new Map(list(refs).filter((r) => r && r.id).map((r) => [r.id, r]));
  return list(card && card.evidence).filter((ev) => ev && ev.id).map((ev) => {
    const ref = byId.get(ev.id);
    return { id: ev.id, title: ev.title || (ref && ref.title) || null, thumb: (ref && ref.thumb) || null };
  });
}

// Saved drafts (videos) whose source is this card.
export function madeFrom(drafts, cardId) {
  return list(drafts).filter((d) => d && d.source === cardId && typeof d.path === "string")
    .map((d) => ({ path: d.path, title: (typeof d.title === "string" && d.title.trim()) || UNTITLED }));
}

const KIND_KO = { flow: "흐름", technique: "기법", angle: "각도", combo: "조합" };

// Why a kind cannot be drawn yet in this category ("" = it can, or not a kind). `data` is /api/motif
// (stage, has_typify, allowed_types; same rule as motif.allowed_types on the server).
export function lockedReason(kind, data) {
  if (!data || !KINDS.includes(kind)) return "";
  const allowed = Array.isArray(data.allowed_types) ? data.allowed_types : [];
  if (allowed.includes(kind)) return "";
  if (data.stage === 3 && !data.has_typify && (kind === "flow" || kind === "combo")) {
    return "흐름·조합 카드는 유형화를 마친 뒤에 나와요";
  }
  return `${KIND_KO[kind]} 카드는 레퍼런스가 더 모이면 뽑을 수 있어요`;
}
