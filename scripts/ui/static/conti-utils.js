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

// ---- history panel ----------------------------------------------------------------------------

// Version kinds as shown in the list. `named` has no fixed text: the name the user gave is the label.
export const KIND_TEXT = {
  created: "처음 만듦", auto: "자동", named: null, before_recreate: "다시 만들기 전", external: "md 직접 수정", restored: "되돌림",
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
  sec: "길이", ref_frame: "참고 프레임", section: "섹션",
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
