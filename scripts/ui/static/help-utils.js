// Pure helpers of the help panel. No imports and no DOM access, so node can import this file directly
// (help-panel.js imports browser-absolute paths and re-exports these).
const POLL_MS = 1000;
const POLL_MAX_MS = 15000;

// Help page of a video workspace tab (#/video/<id>/<tab>): the screen the tab hosts.
const VIDEO_TAB_HELP = { conti: "conti", check: "conti", shoot: "conti", diagnose: "diagnose" };

// "#/draft/view/x" -> "draft", "#/guide/long" -> "guide", "" / "#/" / non-route hash -> "home".
// The video workspace uses the help of the screen its tab hosts (기획안 for the list and the plan tab);
// "#/refs?tab=library" is the 공용 라이브러리 tab of 레퍼런스.
export function helpPage(hash) {
  const text = String(hash || "");
  const m = /^#\/([a-z][a-z0-9-]*)/.exec(text);
  if (!m) return "home";
  if (m[1] === "add") return "refs";     // #/add is the 레퍼런스 screen with the add area open
  if (m[1] === "videos") return "draft";
  if (m[1] === "video") return VIDEO_TAB_HELP[text.split("?")[0].split("/")[3]] || "draft";
  if (m[1] === "refs" && /^#\/refs\?(.*&)?tab=library(&|$)/.test(text)) return "library";
  return m[1];
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
