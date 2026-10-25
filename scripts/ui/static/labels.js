// Korean display names for the fixed type codes (design doc section 5; scripts/videomd.py HOOK_TYPES,
// NARRATIVE_TYPES). Files keep the English codes: only what the screen shows changes. No imports (node tests).

export const HOOK_KO = { question: "질문형", result_first: "결과 먼저", shock_line: "충격 한마디", situation: "상황 제시" };
export const NARRATIVE_KO = { problem_solution: "문제 해결", list: "목록", journey: "여정", compare: "비교", reaction: "리액션" };
export const FORMAT_KO = { long: "롱폼", short: "쇼츠" };
export const CAMERA_KO = { static: "고정", pan: "패닝", zoom: "줌" };
// Section labels and body subtypes (scripts/videomd.py LABELS, BODY_SUBS).
export const SECTION_KO = {
  hook: "훅", intro: "인트로", body: "본문", demo: "시연", transition: "전환", cta: "행동 유도(CTA)", outro: "아웃트로",
};
export const BODY_SUB_KO = { list_item: "목록 항목", story: "이야기", explain: "설명", compare: "비교" };

// Known code -> Korean; an unknown non-empty code is shown as is (a hand-edited md stays visible); else "".
function nameIn(map, code) {
  if (typeof code !== "string" || !code) return "";
  return Object.prototype.hasOwnProperty.call(map, code) ? map[code] : code;
}
export const hookName = (code) => nameIn(HOOK_KO, code);
export const narrativeName = (code) => nameIn(NARRATIVE_KO, code);
export const formatName = (code) => nameIn(FORMAT_KO, code);
export const sectionLabelName = (code) => nameIn(SECTION_KO, code);
export const bodySubName = (code) => nameIn(BODY_SUB_KO, code);
// "body" + "explain" -> "본문(설명)"; no subtype -> just the label name.
export function sectionName(label, sub) {
  const name = sectionLabelName(label);
  const subName = bodySubName(sub);
  return subName ? `${name}(${subName})` : name;
}

const finite = (v) => typeof v === "number" && Number.isFinite(v);

// Largest camera share as { key, label, pct } (ties keep static > pan > zoom), or null when there is none
// or it rounds to 0%.
export function topCamera(shares) {
  if (!shares || typeof shares !== "object") return null;
  let best = null;
  for (const key of Object.keys(CAMERA_KO)) {
    const v = shares[key];
    if (finite(v) && v >= 0 && v <= 1 && (best === null || v > best.v)) best = { key, v };
  }
  const pct = best ? Math.round(best.v * 100) : 0;
  return pct >= 1 ? { key: best.key, label: CAMERA_KO[best.key], pct } : null;   // all zero -> no cell, never "0%"
}

// 2.38 -> "2.4초", 12 -> "12초"; non-positive or not a number -> "".
export function fmtCut(sec) {
  if (!finite(sec) || sec <= 0) return "";
  return `${sec >= 10 ? Math.round(sec) : Math.round(sec * 10) / 10}초`;
}

function fmtLen(sec) {
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const ENDING = new Set(["cta", "outro"]);

// One-line caption under the section band: hook / main part / ending lengths, e.g. "훅 0:27 · 본문 19:13 · 마무리 0:28".
// Parts with no time are left out; "" when nothing is measurable.
export function bandCaption(sections) {
  const sum = { hook: 0, main: 0, end: 0 };
  for (const s of Array.isArray(sections) ? sections : []) {
    if (!s || typeof s.label !== "string" || !finite(s.start) || !finite(s.end) || s.end <= s.start) continue;
    const part = s.label === "hook" ? "hook" : ENDING.has(s.label) ? "end" : "main";
    sum[part] += s.end - s.start;
  }
  return [["훅", sum.hook], ["본문", sum.main], ["마무리", sum.end]]
    .filter(([, t]) => t >= 0.5)
    .map(([name, t]) => `${name} ${fmtLen(t)}`)
    .join(" · ");
}
