// Pure helpers of the typify page: the archetype profile lines, the ratio legend and the outlier phrase.
// Relative import only (node tests load this file directly).
import { CAMERA_KO, SECTION_KO, fmtCut, hookName, narrativeName, sectionName, topCamera } from "./labels.js";

export const AXIS_NAMES = {
  structure: "구조", hook: "훅", narrative: "서사", title: "제목", pacing: "컷 속도", shots: "샷",
};
export const EMPTY = "—";
const CAMERA_MIN = 5;   // camera shares under 5% are left out (the largest one always stays)
const SCREEN_PARTS = [["face", "얼굴"], ["hands_only", "손만"], ["broll", "B롤"], ["text_overlay", "자막"]];

const finite = (v) => typeof v === "number" && Number.isFinite(v);
const pct = (v) => Math.round(v * 100);
const join = (parts) => (parts.length ? parts.join(" · ") : EMPTY);

// { code: count } -> top two as "결과 먼저 44% · 질문형 28%" (share of all counted videos).
function topCounts(counts, name) {
  const entries = Object.entries(counts || {}).filter(([, n]) => finite(n) && n > 0);
  const total = entries.reduce((s, [, n]) => s + n, 0);
  if (!total) return EMPTY;
  return join([...entries].sort((x, y) => y[1] - x[1]).slice(0, 2)
    .map(([code, n]) => `${name(code)} ${Math.round((n / total) * 100)}%`));
}

function pacingText(pacing) {
  const p = pacing || {};
  const parts = [];
  const avg = fmtCut(p.avg_cut_sec);
  if (avg) parts.push(`평균 ${avg}`);
  const by = p.by_section || {};
  for (const label of Object.keys(SECTION_KO)) {
    const t = fmtCut(by[label]);
    if (t) parts.push(`${sectionName(label)} ${t}`);
  }
  return join(parts);
}

function cameraText(camera) {
  const top = topCamera(camera);
  if (!top) return EMPTY;
  const shown = Object.keys(CAMERA_KO)
    .filter((k) => finite(camera[k]) && pct(camera[k]) >= CAMERA_MIN)
    .sort((x, y) => camera[y] - camera[x]);
  if (!shown.length) shown.push(top.key);
  return join(shown.map((k) => `${CAMERA_KO[k]} ${pct(camera[k])}%`));
}

function shotText(shots) {
  const sizes = Array.isArray(shots && shots.top_sizes) ? shots.top_sizes : [];
  return join(sizes.filter((s) => Array.isArray(s) && s[0] && finite(s[1]) && pct(s[1]) >= 1)
    .map(([name, v]) => `${name} ${pct(v)}%`));
}

function screenText(shots) {
  const s = shots || {};
  return join(SCREEN_PARTS.filter(([k]) => finite(s[k])).map(([k, name]) => `${name} ${pct(s[k])}%`));
}

// 1605.67 -> "26분 46초", 45 -> "45초", 3725 -> "1시간 2분".
export function fmtLength(sec) {
  if (!finite(sec) || sec <= 0) return EMPTY;
  const total = Math.round(sec);
  const hours = Math.floor(total / 3600);
  const min = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (hours) return `${hours}시간 ${min}분`;
  return min ? `${min}분 ${s}초` : `${s}초`;
}

// The same seven lines for every archetype, in the same order; a line with no value shows "—" so cards line up.
export function profileRows(a) {
  const x = a || {};
  return [
    { label: "훅", value: topCounts(x.hook_type, hookName) },
    { label: "서사", value: topCounts(x.narrative_type, narrativeName) },
    { label: "컷 속도", value: pacingText(x.pacing) },
    { label: "카메라", value: cameraText(x.camera) },
    { label: "샷", value: shotText(x.shots) },
    { label: "화면 요소", value: screenText(x.shots) },
    { label: "평균 길이", value: fmtLength(x.mean_duration) },
  ];
}

// Mean section shares (already parts of the whole, as the server's notes quote them) as
// [{ label, name, pct }], under 1% left out. The name takes the body subtype written
// in the skeleton ("body/story" -> "본문(이야기)"), so the legend reads like the chips above it.
export function ratioLegend(ratio, skeleton) {
  const entries = Object.entries(ratio || {}).filter(([, v]) => finite(v) && v > 0);
  const subs = {};
  for (const part of String(skeleton || "").split("→")) {
    const [base, sub] = part.split("/").map((s) => s.trim());
    if (base && sub && !subs[base]) subs[base] = sub;
  }
  return entries
    .map(([label, v]) => ({ label, name: sectionName(label, subs[label]), pct: pct(v) }))
    .filter((e) => e.pct >= 1);
}

export const legendText = (legend) => legend.map((e) => `${e.name} ${e.pct}%`).join(" · ");

// "컷 속도에서 혼자 떨어짐"; several axes are joined with "·"; unknown codes are shown as they are.
export function outlierText(o) {
  const names = ((o && o.axes) || []).map((axis) => AXIS_NAMES[axis] || String(axis));
  return `${names.length ? names.join("·") : "일부 축"}에서 혼자 떨어짐`;
}
