// Pure timing helpers (no remotion import) so they can be tested with plain node.

export const FADE_IN_FRAMES = 8;
export const FADE_OUT_FRAMES = 4;
export const MIN_ANIMATED_SEC = 0.4;
export const START_SCALE = 0.85;

export function toFrames(sec, fps) {
  return Math.round(sec * fps);
}

// Convert second-based captions to frame ranges. Overlaps are cut at the next start;
// entries whose length is <= 0 frames are dropped.
export function captionFrames(captions, fps) {
  const items = captions
    .map((c) => ({ from: toFrames(c.start, fps), end: toFrames(c.end, fps), text: c.text }))
    .filter((c) => c.end > c.from)
    .sort((a, b) => a.from - b.from);
  const result = [];
  for (let i = 0; i < items.length; i += 1) {
    const cur = items[i];
    const next = items[i + 1];
    const end = next ? Math.min(cur.end, next.from) : cur.end;
    const duration = end - cur.from;
    if (duration > 0) result.push({ from: cur.from, duration, text: cur.text });
  }
  return result;
}

// Split on whitespace; words containing a digit are emphasised.
export function emphasisTokens(text) {
  return text
    .split(/\s+/)
    .filter((w) => w.length > 0)
    .map((w) => ({ text: w, emphasis: /\d/.test(w) }));
}

// Critically damped spring, 0 -> ~1 over FADE_IN_FRAMES.
function springProgress(frame, fps) {
  if (frame >= FADE_IN_FRAMES) return 1;
  if (frame <= 0) return 0;
  const t = frame / fps;
  const w = 6.64 / (FADE_IN_FRAMES / fps);
  return 1 - (1 + w * t) * Math.exp(-w * t);
}

export function animate(frame, duration, fps) {
  if (duration / fps < MIN_ANIMATED_SEC) return { scale: 1, opacity: 1 };
  const p = springProgress(frame, fps);
  const scale = START_SCALE + (1 - START_SCALE) * p;
  const fadeIn = frame >= FADE_IN_FRAMES ? 1 : Math.min(1, p * 2);
  const remaining = duration - frame;
  const fadeOut = remaining >= FADE_OUT_FRAMES ? 1 : Math.max(0, remaining / FADE_OUT_FRAMES);
  return { scale, opacity: Math.min(fadeIn, fadeOut) };
}

// Rough text width in em: CJK / emoji / symbols = 1em, latin & digits ~0.6em.
function textUnits(text) {
  let units = 0;
  for (const ch of text) units += ch.codePointAt(0) < 0x2000 ? 0.6 : 1;
  return units;
}

// Largest font size (>= 60% of base) whose estimated wrapping fits in maxLines.
export function fitFontSize(text, baseSize, maxWidth, maxLines = 2) {
  const total = textUnits(text.replace(/\s+/g, " ").trim());
  const wordSlack = 1.15; // word-wrap wastes some of each line
  for (let ratio = 1; ratio >= 0.6 - 1e-9; ratio -= 0.05) {
    const size = baseSize * ratio;
    const lines = Math.ceil((total * size * wordSlack) / maxWidth);
    if (lines <= maxLines) return size;
  }
  return baseSize * 0.6;
}
