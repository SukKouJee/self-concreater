// Pure helpers for the conti cut list (no DOM, no imports) so node tests can load them.
// Cuts here are the objects from GET /api/conti: { id, section (number), ... } in play order.

export const MIN_SEC = 0.5;
export const MAX_SEC = 600;

function clock(sec) {
  const total = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function fmtClock(sec) {
  return clock(sec);
}

// Header line: planned target vs the sum of the cut lengths.
export function fmtDelta(totalSec, targetSec) {
  const total = Math.round(Number(totalSec) || 0);
  if (targetSec === null || targetSec === undefined || Number.isNaN(Number(targetSec))) return `지금 ${clock(total)}`;
  const target = Math.round(Number(targetSec));
  const diff = total - target;
  if (diff === 0) return `목표 ${clock(target)} · 지금 ${clock(total)}`;
  return `목표 ${clock(target)} → 지금 ${clock(total)} (${diff > 0 ? "+" : "-"}${clock(Math.abs(diff))})`;
}

// A section must keep at least one cut, so a cut can only be removed when its section has another one.
export function canRemove(cuts, id) {
  const cut = cuts.find((c) => c.id === id);
  return !!cut && cuts.filter((c) => c.section === cut.section).length >= 2;
}

// Length in seconds: 0.5 steps, 0.5..600 (a merged scene can run minutes).
export function snapSec(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return MIN_SEC;
  return Math.min(MAX_SEC, Math.max(MIN_SEC, Math.round(n * 2) / 2));
}

// Same section rule as the server (conti._fit_section): an explicit section wins when it keeps the section
// numbers in order next to its new neighbours; otherwise the cut's own section if it fits, else the previous
// cut's section (or the next cut's at the very front).
function fitSection(rest, at, explicit, current) {
  const prev = at > 0 ? rest[at - 1].section : null;
  const next = at < rest.length ? rest[at].section : null;
  const fits = (s) => (prev === null || prev <= s) && (next === null || s <= next);
  if (explicit !== undefined && explicit !== null && fits(explicit)) return explicit;
  if (fits(current)) return current;
  if (prev !== null) return prev;
  return next !== null ? next : 0;
}

// Drag and drop. `gap` is the slot the cut was dropped on in the list as shown (0 = before the first cut,
// cuts.length = after the last). Returns what POST /api/conti/cuts move needs: `to` is the final 0-based index
// in the list after the cut has been taken out (so dropping one row lower gives old index + 1) and `section`
// is the cut's section there. `section` (optional) is the section of the row or header it was dropped on.
export function dropTarget(cuts, fromId, gap, section) {
  const from = cuts.findIndex((c) => c.id === fromId);
  if (from < 0) return null;
  const rest = cuts.filter((c) => c.id !== fromId);
  const to = Math.min(Math.max(gap > from ? gap - 1 : gap, 0), rest.length);
  return { to, section: fitSection(rest, to, section, cuts[from].section) };
}

// 위로 / 아래로: swap with the neighbour. Crossing a section edge hands the cut over to the neighbour's section.
export function stepMove(cuts, id, dir) {
  const from = cuts.findIndex((c) => c.id === id);
  const other = from + dir;
  if (from < 0 || other < 0 || other >= cuts.length) return null;
  const section = cuts[other].section;
  return { to: other, section, crosses: section !== cuts[from].section };
}

// Same list as scripts/features.py SHOT_SIZES (tests/test_conti_utils_js.py keeps the two in step).
export const SHOT_SIZES = ["익스트림 클로즈업", "클로즈업", "바스트", "미디엄", "와이드", "탑뷰", "POV", "인서트"];

// ---- history panel ----------------------------------------------------------------------------

// Version kinds as shown in the list. `named` has no fixed text: the name the user gave is the label.
export const KIND_TEXT = {
  created: "처음 만듦", auto: "자동", named: null, before_recreate: "다시 만들기 전", before_merge: "합치기 전",
  external: "파일 직접 수정", restored: "되돌림",
};

export function versionLabel(v) {
  if (v.kind === "named") return v.name || "이름 붙인 버전";
  if (v.kind === "restored") return v.name || KIND_TEXT.restored;
  return KIND_TEXT[v.kind] || v.kind;
}

// "2026-10-27T09:03:05" -> "10/27 09:03" (the server writes local time without a zone).
export function fmtWhen(iso) {
  const m = /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
  return m ? `${m[1]}/${m[2]} ${m[3]}:${m[4]}` : String(iso || "");
}

const FIELD_TEXT = {
  line: "대사", caption: "자막", action: "장면 설명", guide: "가이드", sfx: "효과음", size: "샷 크기", angle: "앵글",
  sec: "길이", ref_frame: "참고 프레임", ref_auto: "자동 참고 사진", art: "장면 그림", section: "섹션",
};
export const CUT_FIELD_KEYS = ["section", "size", "angle", "sec", "action", "line", "caption", "sfx", "guide", "ref_frame"];
export const fieldText = (f) => FIELD_TEXT[f] || f;

function plain(v) {
  if (v === null || v === undefined || v === "") return "(비움)";
  return typeof v === "number" ? String(Math.round(v * 100) / 100) : String(v);
}

function quoted(v) {
  const s = plain(v);
  if (s === "(비움)") return s;
  const chars = [...s];
  return `「${chars.length > 24 ? `${chars.slice(0, 24).join("")}…` : s}」`;
}

// 으로/로 after a number: 3 삼, 6 육, 0 영/십/백 end in a consonant (not ㄹ).
const euro = (n) => (/[360]$/.test(String(n)) ? "으로" : "로");

// One line of the change list. `sectionName` (optional) is the section a new cut sits in.
export function changeText(c, sectionName) {
  if (c.kind === "shifted") {
    const d = c.delta === null || c.delta === undefined || c.delta === 0 ? "" : ` (${c.delta > 0 ? "+" : "-"}${clock(Math.abs(c.delta))})`;
    return `뒤 섹션 ${c.count}개 시간이 밀렸어요${d}`;
  }
  if (c.kind === "total") return `총 길이 ${clock(c.from)} → ${clock(c.to)}`;
  if (c.kind === "removed") return `컷 ${c.n_from} 삭제`;
  if (c.kind === "added") return `컷 ${c.n_to} 새로 생김${sectionName ? `(${sectionName})` : ""}`;
  if (c.kind === "moved") return `컷 ${c.n_from} → ${c.n_to}${euro(c.n_to)} 옮김`;
  if (c.field === "script") return `섹션 ${Number(c.section) + 1} ${c.from === c.to ? "시간" : "대사"} 바뀜`;
  const n = c.n_to ?? c.n_from;
  if (c.field === "sec") return `컷 ${n} 길이 ${plain(c.from)}초 → ${plain(c.to)}초`;
  if (c.field === "size" || c.field === "angle" || c.field === "section") return `컷 ${n} ${fieldText(c.field)}: ${plain(c.from)} → ${plain(c.to)}`;
  return `컷 ${n} ${fieldText(c.field)}: ${quoted(c.from)} → ${quoted(c.to)}`;
}

// Pair the two cut lists of a diff row by row for the side-by-side view. Walks the right (newer) side in order;
// a left cut with no partner is a "removed" row where it stood, a right cut with no partner is "added".
// Cuts are matched by id when all have one, else by position (same rule as the server).
export function alignCuts(left, right, changes) {
  const byId = [...left, ...right].every((c) => c.id);
  const partner = (r, i) => (byId ? left.find((l) => l.id === r.id) : left[i]);
  const hasPartner = (l, i) => (byId ? right.some((r) => r.id === l.id) : i < right.length);
  const fieldsOf = (r) => changes.filter((c) => c.kind === "changed" && c.field !== "script" && c.n_to === r.n).map((c) => c.field);
  const movedTo = new Set(changes.filter((c) => c.kind === "moved").map((c) => c.n_to));
  const used = new Set();
  const rows = [];
  let li = 0;
  let ri = 0;
  while (li < left.length || ri < right.length) {
    while (li < left.length && used.has(left[li])) li += 1;
    if (li < left.length && !hasPartner(left[li], li)) {
      rows.push({ status: "removed", l: left[li], r: null, fields: [] });
      li += 1;
    } else if (ri < right.length) {
      const l = partner(right[ri], ri);
      if (!l) {
        rows.push({ status: "added", l: null, r: right[ri], fields: [] });
      } else {
        used.add(l);
        const fields = fieldsOf(right[ri]);
        const status = movedTo.has(right[ri].n) ? "moved" : fields.length ? "changed" : "same";
        rows.push({ status, l, r: right[ri], fields });
      }
      ri += 1;
    } else {
      break;
    }
  }
  return rows;
}

// A single insert shifts every later section's time; show that run of time-only section lines as one line.
// Section text changes and everything else stay as they are. The shift is read from the "total" entry.
export function collapseChanges(changes) {
  const total = changes.find((c) => c.kind === "total");
  const delta = total ? Math.round(Number(total.to) - Number(total.from)) : null;
  const timeOnly = (c) => c.kind === "changed" && c.field === "script" && c.from === c.to;
  const out = [];
  for (const c of changes) {
    const last = out[out.length - 1];
    if (!timeOnly(c)) out.push(c);
    else if (last && last.kind === "shifted") last.count += 1;
    else out.push({ kind: "shifted", count: 1, delta });
  }
  return out;
}

// ---- card board (tool row, cards, scene actions) -------------------------------------------------

// Section filter groups: 도입 = hook + intro, 마무리 = cta + outro, 본론 = everything else (body, demo, transition).
export const SECTION_GROUPS = [{ key: "intro", label: "도입" }, { key: "main", label: "본론" }, { key: "end", label: "마무리" }];
const GROUP_OF = { hook: "intro", intro: "intro", cta: "end", outro: "end" };
export const sectionGroupOf = (label) => GROUP_OF[label] || "main";

// Filter chips present in this conti: [{key, label, count}]. count = cuts in the group when cuts are given, else sections.
export function sectionGroups(script, cuts) {
  const labels = Array.isArray(cuts) ? cuts.map((c) => c.label) : (Array.isArray(script) ? script : []).map((s) => s && s.label);
  const count = {};
  for (const l of labels) { const g = sectionGroupOf(l); count[g] = (count[g] || 0) + 1; }
  return SECTION_GROUPS.filter((g) => count[g.key]).map((g) => ({ ...g, count: count[g.key] }));
}

const SEARCH_KEYS = ["action", "line", "guide", "caption", "sfx", "size", "angle"];

// Search box + section chip. Every word of q must appear in the scene's text (or its photo description);
// a bare number or "#3" also finds scene 3.
export function filterCuts(cuts, { q = "", section = "all" } = {}) {
  if (!Array.isArray(cuts)) return [];
  const words = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
  const num = words.length === 1 && /^#?\d+$/.test(words[0]) ? Number(words[0].replace("#", "")) : null;
  return cuts.filter((c) => {
    if (section && section !== "all" && sectionGroupOf(c.label) !== section) return false;
    if (!words.length) return true;
    if (num !== null) return c.n === num;
    const text = [...SEARCH_KEYS.map((k) => c[k]), c.ref && c.ref.desc].filter(Boolean).join(" ").toLowerCase();
    return words.every((w) => text.includes(w));
  });
}

const MATCH_LABEL = { pinned: "참고(고정)", content: "참고", composition: "구도만 참고" };

// Caption under a scene photo: "참고 · <설명>", "구도만 참고 · <설명>", "참고(고정)". A pinned photo's title is its
// file name, so only its description is shown.
export function refCaption(ref) {
  if (!ref) return "";
  const label = MATCH_LABEL[ref.match] || MATCH_LABEL.composition;
  const what = ref.desc || (ref.match === "pinned" ? "" : ref.title || "");
  return what ? `${label} · ${what}` : label;
}

function secText(sec) {
  const n = Number(sec) || 0;
  return String(n >= 10 ? Math.round(n) : Math.round(n * 10) / 10);
}

// Scene length on a card: "약 25초".
export const aboutSec = (sec) => `약 ${secText(sec)}초`;

// Frame badge: "01 · 0:00–0:10".
export function cardStamp(c) {
  return `${String(c.n).padStart(2, "0")} · ${clock(c.start)}–${clock(c.end)}`;
}

// ui.js tag kind for a section label (transition shows as the 체크 tag color).
const TAG_OF = { hook: "hook", intro: "intro", transition: "check", cta: "outro", outro: "outro" };
export const tagKind = (label) => TAG_OF[label] || "body";

// Same rule as scripts/scene_merge.groups: cuts in one section whose action matches once spaces and punctuation are
// dropped are one scene. True when some scene is still repeated (장면으로 합치기 is offered).
const bare = (t) => String(t || "").replace(/[^\p{L}\p{N}]+/gu, "");
export function hasRepeats(cuts) {
  const seen = new Set();
  for (const c of Array.isArray(cuts) ? cuts : []) {
    const key = `${c.section}\u0000${bare(c.action)}`;
    if (seen.has(key)) return true;
    seen.add(key);
  }
  return false;
}

// 사진 설명 만들기: Claude looks at 8 photos per call, about 45 seconds each (scripts/timings.py "describe").
const DESC_BATCH = 8;
const DESC_BATCH_SEC = 45;

// Hint above the button while some reference photos have no description; null when there is nothing to describe.
export function describeHint(status) {
  if (!status) return null;
  const todo = Math.max(0, (Number(status.frames) || 0) - (Number(status.described) || 0));
  if (!todo) return null;
  const minutes = Math.max(1, Math.ceil((Math.ceil(todo / DESC_BATCH) * DESC_BATCH_SEC) / 60));
  return { todo, minutes, text: `레퍼런스 사진 설명을 만들면 장면에 맞는 사진이 붙어요 (사진 ${todo}장, 약 ${minutes}분)` };
}

const MERGE_SCENE_KEYS = ["n", "id", "action", "sec", "line", "size"];

// Merge preview by section: how many cuts it had, whether it changes, and its scenes (new order, new lines).
export function mergeView(preview) {
  if (!preview || !Array.isArray(preview.sections)) return [];
  const cuts = Array.isArray(preview.cuts) ? preview.cuts : [];
  return preview.sections.map((s) => ({
    idx: s.idx, label: s.label, changed: !!s.split,
    before: (s.scenes || []).reduce((n, sc) => n + ((sc.merged_ids || []).length || 1), 0),
    scenes: cuts.filter((c) => c.section === s.idx).map((c) => Object.fromEntries(MERGE_SCENE_KEYS.map((k) => [k, c[k]]))),
  }));
}

// What POST /api/conti/merge/apply takes: the scenes in their new order with section and line only.
export function mergeApplyCuts(preview) {
  return (preview && Array.isArray(preview.cuts) ? preview.cuts : []).map((c) => ({ id: c.id, section: c.section, line: c.line }));
}

// Card grid arrow keys. `tops` are the cards' layout tops (getBoundingClientRect: a positioned card's offsetTop is 0),
// in order; the cards on the first row's top (within 1px) give the column count. ←→ one card, ↑↓ one row,
// Home / End the ends. Returns the new index, or null for another key.
export function gridMove(tops, i, key) {
  const n = Array.isArray(tops) ? tops.length : 0;
  if (!n) return null;
  const cols = Math.max(1, tops.filter((t) => Math.abs(t - tops[0]) < 1).length);
  // ↑ on the first row and ↓ on the last row stay; ↓ into a short last row lands on its last card
  const lastRow = Math.floor((n - 1) / cols);
  const down = Math.floor(i / cols) < lastRow ? Math.min(n - 1, i + cols) : i;
  const to = { ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: down, ArrowUp: i - cols >= 0 ? i - cols : i, Home: 0, End: n - 1 }[key];
  return to === undefined ? null : Math.max(0, Math.min(n - 1, to));
}

// Card / list view switch as stored in the browser ("cards" unless the stored value is "list").
export const parseView = (raw) => (raw === "list" ? "list" : "cards");

// Cut id at number n in a conti answer, or null.
export function idOfN(data, n) {
  const c = data.cuts.find((x) => x.n === n);
  return c ? c.id : null;
}

// Maps keyed by cut number (drafts, alternatives) follow their cut by id after cuts come, go or move;
// entries whose cut is gone are dropped.
// Remaps from a snapshot: in place, a cut moving to a number that is still waiting to be moved would overwrite it.
export function remapByCutId(map, before, after) {
  const entries = [...map.entries()];
  map.clear();
  for (const [n, value] of entries) {
    const id = idOfN(before, n);
    const moved = id && after.cuts.find((c) => c.id === id);
    if (moved) map.set(moved.n, value);
  }
}

// ---- scene illustrations (장면 그림) ------------------------------------------------------------

// A scene whose description was edited waits this long for more edits before it is drawn again.
export const ART_DEBOUNCE_MS = 4000;

// Auto redraw as a pure state machine. state = { pending: [ids], running, disabled } (never mutated).
// Events: stale {ids} (an edit made these drawings old: wait ART_DEBOUNCE_MS again) · timer (the wait is over: start
// the pending scenes unless a drawing job is running) · done (the job ended: pending scenes go at once) ·
// requeue {ids} (the start found another job on this conti: wait for it) · missed (that job was not found: wait
// ART_DEBOUNCE_MS again) · fail_ready (Claude is not usable: stop
// auto start on this screen) · leave (the screen is left: drop what was scheduled; a started job goes on).
// Returns { state, start: ids to draw now | null, delayMs: (re)start the wait timer | null }.
export function artRedrawPlan(state, event) {
  const st = { pending: [...((state && state.pending) || [])], running: !!(state && state.running), disabled: !!(state && state.disabled) };
  const add = (ids) => { for (const id of ids || []) if (!st.pending.includes(id)) st.pending.push(id); };
  const type = event && event.type;
  if (type === "stale") {
    if (st.disabled) return { state: st, start: null, delayMs: null };
    add(event.ids);
    return { state: st, start: null, delayMs: st.pending.length ? ART_DEBOUNCE_MS : null };
  }
  if (type === "timer") {
    if (st.running || st.disabled || !st.pending.length) return { state: st, start: null, delayMs: null };
    const start = st.pending;
    return { state: { ...st, pending: [], running: true }, start, delayMs: null };
  }
  if (type === "done") {
    st.running = false;
    return { state: st, start: null, delayMs: st.pending.length && !st.disabled ? 0 : null };
  }
  if (type === "requeue") {
    if (!st.disabled) add(event.ids);
    return { state: st, start: null, delayMs: null };
  }
  // the job that held this conti could not be found (another conti's job, a failed request): try again after a
  // full wait, never at once (that would POST, meet conti_busy and look again in a tight loop)
  if (type === "missed") {
    st.running = false;
    return { state: st, start: null, delayMs: st.pending.length && !st.disabled ? ART_DEBOUNCE_MS : null };
  }
  if (type === "fail_ready") return { state: { pending: [], running: false, disabled: true }, start: null, delayMs: null };
  if (type === "leave") return { state: { ...st, pending: [] }, start: null, delayMs: null };
  return { state: st, start: null, delayMs: null };
}

// What a scene shows: the photo the user pinned > the drawing > a matched photo > the shot sketch.
export function sceneVisual(cut) {
  const ref = cut && cut.ref;
  if (ref && ref.match === "pinned") return { kind: "pinned", ref };
  if (cut && cut.art && cut.art.url) return { kind: "art", art: cut.art };
  if (ref) return { kind: "photo", ref };
  return { kind: "sketch" };
}

// Caption under a drawing. busy: a drawing job runs on this conti or the scene waits to be drawn again.
export function artCaption(art, busy) {
  if (!art || art.state !== "stale") return "장면 그림";
  return busy ? "다시 그리는 중…" : "설명이 바뀌었어요";
}

// Scene tool button: 장면 그림 그리기 while a scene that can have a drawing has none, else 전체 다시 그리기;
// null when every scene has a pinned photo (nothing to draw).
export function artToolLabel(cuts) {
  const drawable = (Array.isArray(cuts) ? cuts : []).filter((c) => !c.ref_frame);
  if (!drawable.length) return null;
  return drawable.some((c) => !c.art) ? "장면 그림 그리기" : "전체 다시 그리기";
}
