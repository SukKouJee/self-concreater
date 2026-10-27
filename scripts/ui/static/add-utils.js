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
