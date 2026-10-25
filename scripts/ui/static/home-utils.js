// Pure helpers for the home screen (no DOM, no imports) so node tests can load them directly.

// Per-video steps (GET /api/status videos[].steps keys, in order).
export const STEP_NAMES = {
  draft: "기획안", polish: "다듬기", conti: "콘티", filmed: "촬영함", captions: "자막 영상", diagnose: "진단",
};

// One short reason line under the next-action headline, by the video's next step.
export const NEXT_REASON = {
  conti: "기획안을 촬영용 콘티로 바꿔요",
  filmed: "콘티대로 찍었으면 체크해 두세요",
  diagnose: "찍은 영상을 아키타입과 비교해 고칠 점을 찾아요",
};

const NEXT_VERB = { conti: "콘티 만들기", filmed: "찍었으면 촬영함 체크", diagnose: "진단해 보기" };

// Rows shown before the "N개 더 보기" button.
export const VISIBLE_VIDEOS = 3;

// Action for a video's next step: a link, the in-place filmed toggle, or nothing (video complete).
export function nextButton(step, path) {
  if (step === "conti") return { text: "콘티 만들기", href: `#/conti/${encodeURIComponent(path)}` };
  if (step === "filmed") return { text: "촬영함 체크", action: "film" };
  if (step === "diagnose") return { text: "진단하기", href: `#/diagnose/${encodeURIComponent(path)}` };
  return null;
}

export function nextHeadline(title, step) {
  return `「${title}」 ${NEXT_VERB[step] || STEP_NAMES[step] || ""}`.trim();
}

// Dot state of one video step. Done wins over optional; only the video's next (required) step is "next".
export function stepState(step, nextKey) {
  if (step.done) return "done";
  if (step.key === nextKey) return "next";
  return step.optional ? "optional" : "todo";
}

const STEP_SR = { done: "완료", next: "다음 단계", todo: "아직", optional: "선택, 아직" };
export function stepSr(state) {
  return STEP_SR[state] || "";
}

// "10월 25일 · 15분 목표" from the draft date (YYYY-MM-DD) and target length; missing parts are left out.
export function videoMeta(v) {
  const parts = [];
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v.date || "");
  if (m) parts.push(`${Number(m[2])}월 ${Number(m[3])}일`);
  const target = targetText(v.target_sec);
  if (target) parts.push(target);
  return parts.join(" · ");
}

// Target length: whole seconds under a minute ("15초 목표"), whole minutes from 60 up (90 -> "2분 목표").
export function targetText(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) return "";
  const s = Math.max(1, Math.round(sec));
  return s < 60 ? `${s}초 목표` : `${Math.round(s / 60)}분 목표`;
}

// Short stage line: the span from stage_text ("현재 단계: 3 (10개 이상)"), else the ref count.
export function stageLine(cat) {
  const m = /현재 단계:\s*(\d+)\s*\(([^)]+)\)/.exec(cat.stage_text || "");
  if (m) return `레퍼런스 ${m[2]} · ${m[1]}단계`;
  return `레퍼런스 ${cat.ref}개 · ${cat.stage}단계`;
}
