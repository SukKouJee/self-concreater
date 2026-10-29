// Pure helpers of the phone shoot mode (#/video/<id>/shoot) and the 촬영 체크 tab. No imports and no DOM, so node
// tests load this file directly.
// Per-scene checks have no server API (spec 7): they live in this browser under "ytc.shoot.<video id>" as a JSON list
// of cut ids. Every storage access is guarded; when storage is blocked (private window) or a write fails, the checks
// stay in memory for this session (same module, so the shoot screen and the check tab share them).

const memory = new Map();       // storage key -> Set of cut ids (this session)
const unsaved = new Set();      // keys whose last write failed: memory is the truth for them

export const storeKey = (id) => `ytc.shoot.${id}`;

function parseIds(raw) {
  if (raw === null || raw === undefined) return new Set();
  try {
    const list = JSON.parse(raw);
    return new Set(Array.isArray(list) ? list.filter((x) => typeof x === "string" && x) : []);
  } catch {
    return new Set();
  }
}

// shootStore(id[, storage]) -> { get() -> Set<cutId>, toggle(cutId) -> now checked?, set(cutId, on), persistent() }
// storage: a Storage-like object or a function returning one (default: the browser's localStorage, read lazily
// because the getter itself throws when storage is blocked).
export function shootStore(id, storage = () => globalThis.localStorage) {
  const key = storeKey(id);
  const open = typeof storage === "function" ? storage : () => storage;
  let ok = true;

  function read() {
    if (unsaved.has(key)) { ok = false; return new Set(memory.get(key) || []); }
    try {
      const s = open();
      if (!s) throw new Error("no storage");
      const set = parseIds(s.getItem(key));
      memory.set(key, new Set(set));
      ok = true;
      return set;
    } catch {
      ok = false;
      return new Set(memory.get(key) || []);
    }
  }

  function write(set) {
    memory.set(key, new Set(set));
    try {
      const s = open();
      if (!s) throw new Error("no storage");
      if (set.size) s.setItem(key, JSON.stringify([...set])); else s.removeItem(key);
      unsaved.delete(key);
      ok = true;
    } catch {
      unsaved.add(key);
      ok = false;
    }
  }

  function set(cutId, on) {
    const ids = read();
    if (on) ids.add(cutId); else ids.delete(cutId);
    write(ids);
    return !!on;
  }

  return {
    get: read,
    set,
    toggle: (cutId) => set(cutId, !read().has(cutId)),
    // false when the checks only live in memory (they go away when the tab closes)
    persistent: () => { read(); return ok; },
  };
}

// Indexes (0..n-1) of the scenes whose id is checked; ids of scenes no longer in the conti are ignored.
export function doneIndexes(ids, cuts) {
  const out = [];
  (cuts || []).forEach((c, i) => { if (c && ids.has(c.id)) out.push(i); });
  return out;
}

export const doneCount = (ids, cuts) => doneIndexes(ids, cuts).length;

export const shootCount = (done, total) => `촬영 ${done} / ${total}장면`;

// Main button (mockup): not filmed yet -> 찍었어요 ✓; filmed with scenes left -> 다음 장면으로; all filmed -> 전부 찍었어요!
// done: Set of scene indexes.
export function mainLabel(done, i, n) {
  if (!done.has(i)) return "찍었어요 ✓";
  for (let k = 0; k < n; k += 1) if (!done.has(k)) return "다음 장면으로";
  return "전부 찍었어요!";
}

// The next scene not filmed after i (wrapping to the start), or null when every scene is filmed.
export function nextTodo(done, i, n) {
  for (let step = 1; step <= n; step += 1) {
    const k = (i + step) % n;
    if (!done.has(k)) return k;
  }
  return null;
}

// Where the screen opens: the first scene not filmed yet (all filmed or none: the first).
export function firstTodo(done, n) {
  for (let k = 0; k < n; k += 1) if (!done.has(k)) return k;
  return 0;
}

// Progress bar segments, at most `max` (an unmerged conti has hundreds of cuts: one segment each would overflow a
// phone). Segment k covers scenes [floor(k*n/m), floor((k+1)*n/m)): "done" when all of them are filmed (green), "now"
// when it holds the current scene (white), "done-now" when that segment is all filmed too (green, outlined), else "todo".
export const BAR_MAX = 40;
export function barSegments(done, i, n, max = BAR_MAX) {
  const m = Math.min(n, max);
  const out = [];
  for (let k = 0; k < m; k += 1) {
    const lo = Math.floor((k * n) / m);
    const hi = Math.floor(((k + 1) * n) / m);
    let all = hi > lo;
    for (let s = lo; s < hi && all; s += 1) if (!done.has(s)) all = false;
    const here = i >= lo && i < hi;
    out.push(here ? (all ? "done-now" : "now") : all ? "done" : "todo");
  }
  return out;
}

// Up to two shooting tips from the scene guide, one sentence each (a sentence ends at . ! ? before a space, or a
// line break; "1.5초" stays whole). A closing "." is dropped (tips read as short notes).
export function tipsFrom(guide) {
  return String(guide || "")
    .split(/\n+|(?<=[.!?。])\s+/)
    .map((t) => t.trim().replace(/[.。]+$/, "").trim())
    .filter(Boolean)
    .slice(0, 2);
}

function clock(sec) {
  const total = Math.max(0, Math.round(sec));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

// "0:45–4:00" from the scene's start/end seconds ("" when the conti has no times).
export function sceneTime(c) {
  if (!c || typeof c.start !== "number" || typeof c.end !== "number") return "";
  return `${clock(c.start)}–${clock(c.end)}`;
}
