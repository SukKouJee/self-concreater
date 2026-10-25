// Pure helpers of the help panel. No imports and no DOM access, so node can import this file directly
// (help-panel.js imports browser-absolute paths and re-exports these).
const POLL_MS = 1000;
const POLL_MAX_MS = 15000;

// "#/draft/view/x" -> "draft", "#/guide/long" -> "guide", "" / "#/" / non-route hash -> "home".
export function helpPage(hash) {
  const m = /^#\/([a-z][a-z0-9-]*)/.exec(String(hash || ""));
  return m ? m[1] : "home";
}

// Last `max` items (the server only reads the latest 6 anyway).
export function trimHistory(list, max = 6) {
  if (!Array.isArray(list) || max <= 0) return [];
  return list.slice(-max);
}

// Same back-off as the work pages (pages/motif.js): 1s, doubling per failure, capped.
export function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}
