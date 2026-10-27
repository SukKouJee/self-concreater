// Pure helpers for the conti cut list (no DOM, no imports) so node tests can load them.
// Cuts here are the objects from GET /api/conti: { id, section (number), ... } in play order.

export const MIN_SEC = 0.5;
export const MAX_SEC = 120;

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

// Length in seconds: 0.5 steps, 0.5..120.
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
