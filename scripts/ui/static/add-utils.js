// Pure helpers for the add area (no DOM, no imports) so node tests can load them.

export const STATUS_TEXT = {
  new: "새 영상",
  exists: "이미 있음",
  other_role: "다른 구분으로 있음",
  dup: "중복",
  invalid: "영상 링크 아님",
  reanalyze: "다시 분석",
};

export function startLabel(count) {
  return count > 0 ? `영상 ${count}개 분석 시작` : "분석할 영상이 없어요";
}

export function splitLinks(text) {
  return String(text || "").split(/\s+/).map((x) => x.trim()).filter(Boolean);
}

// True when a job from /api/jobs/current still needs the user: it is running, or it holds finished videos
// waiting for review (also after the job stopped). A finished job with nothing left is not shown again.
export function needsAttention(job) {
  if (!job) return false;
  if (job.status === "running" || job.status === "awaiting") return true;
  return (job.items || []).some((i) => i.state === "awaiting" && i.ask === "review");
}

// Removes the first line that is exactly this link. If the link only shares a line with others, just that token goes.
export function removeLine(text, link) {
  const lines = String(text).split("\n");
  const at = lines.findIndex((l) => l.trim() === link);
  if (at >= 0) {
    lines.splice(at, 1);
    return lines.join("\n");
  }
  const shared = lines.findIndex((l) => l.split(/\s+/).includes(link));
  if (shared < 0) return String(text);
  const tokens = lines[shared].trim().split(/\s+/);
  tokens.splice(tokens.indexOf(link), 1);
  lines[shared] = tokens.join(" ");
  return lines.join("\n");
}

// "새 영상 2개 · 다시 분석 1개 · 건너뜀 1개" from the check rows.
export function summaryText(rows) {
  const n = (...keys) => rows.filter((r) => keys.includes(r.status)).length;
  const parts = [];
  if (n("new")) parts.push(`새 영상 ${n("new")}개`);
  if (n("reanalyze")) parts.push(`다시 분석 ${n("reanalyze")}개`);
  const skipped = n("exists", "other_role", "dup", "invalid");
  if (skipped) parts.push(`건너뜀 ${skipped}개`);
  return parts.join(" · ");
}

// Category of one item: its own opts (items of one job can differ), else the job's.
export function itemCategory(item, job) {
  const own = item && item.opts && item.opts.category;
  return own || (job && job.category) || "";
}

// Finished videos waiting for the user's decision in this category.
export function reviewItems(job, category) {
  return ((job && job.items) || []).filter((i) => i.ask === "review" && i.state === "awaiting"
    && !!i.opts && i.opts.category === category);
}

// One line under the current video: how many videos are still to analyse, or (queue empty) how many wait for review.
// Saved videos queued only for shot classification (step "shots") are not counted as 남은 영상.
export function queueLine(job) {
  const items = (job && job.items) || [];
  const waiting = items.filter((i) => i.state === "waiting" && i.step !== "shots").length;
  if (waiting > 0) return `남은 영상 ${waiting}개`;
  if (items.some((i) => i.state === "running")) return "";
  const review = items.filter((i) => i.ask === "review" && i.state === "awaiting").length;
  return review > 0 ? `확인을 기다리는 영상 ${review}개` : "";
}

// 모두 저장 plan: one review-route request per item, once each. `edited` maps item id -> edited analysis (only edited cards).
export function saveAllPlan(items, edited) {
  const seen = new Set();
  const plan = [];
  for (const item of items || []) {
    if (!item || seen.has(item.id)) continue;
    seen.add(item.id);
    const a = edited && Object.prototype.hasOwnProperty.call(edited, item.id) ? edited[item.id] : null;
    plan.push({ id: item.id, body: a ? { action: "save", analysis: a } : { action: "save" } });
  }
  return plan;
}

// Per-item UI state to drop when a new job is shown: ids of `keys` that the new job no longer has.
// A new job can adopt a stopped job's review cards under the same ids, so their unsaved edits must survive.
export function staleIds(keys, job) {
  const live = new Set(((job && job.items) || []).map((i) => i.id));
  return [...(keys || [])].filter((id) => !live.has(id));
}
