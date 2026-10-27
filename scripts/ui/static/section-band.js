// Section band (colored bar of a video's section flow), shared by the reference cards and the add area's review cards.
import { h } from "/static/dom.js";
import { sectionLabelName, sectionName } from "/static/labels.js";

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(num(sec)));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtRange(s) {
  return `${fmtTime(s.start)}–${fmtTime(s.end)}`;
}

// Segment widths are percentages of the video length (or of the last section end when the length is unknown).
export function sectionBand(sections, duration) {
  const segs = [...(sections || []).filter((s) => s && typeof s.label === "string" && s.label)]
    .sort((a, b) => num(a.start) - num(b.start));
  if (segs.length === 0) return null;
  const total = num(duration) > 0 ? num(duration) : Math.max(...segs.map((s) => num(s.end)));
  if (!(total > 0)) return null;
  const band = h("div", {
    class: "sec-band", role: "img",
    "aria-label": `섹션 흐름: ${segs.map((s) => sectionLabelName(s.label)).join(" → ")}`,
  });
  // Position by start: a gap before a section becomes a left margin; overlaps are clamped to the previous end.
  let cursor = 0;
  for (const s of segs) {
    const from = Math.max(num(s.start), cursor);
    const gap = Math.min(100, (Math.max(0, from - cursor) / total) * 100);
    const width = Math.max(0, Math.min(100, ((num(s.end) - from) / total) * 100));
    const text = `${sectionName(s.label, s.sub)} ${fmtRange(s)}`;
    const seg = h("span", { class: `lb-${s.label}`, title: text, "aria-label": text });
    seg.style.width = `${width}%`;
    if (gap > 0) seg.style.marginLeft = `${gap}%`;
    band.append(seg);
    cursor = Math.max(cursor, num(s.end));
  }
  return band;
}
