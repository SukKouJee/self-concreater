// Disabled features (GET /api/features -> { edition, disabled: { <feature>: <text> } }).
// Elements marked data-feature="<feature>" are hidden when that feature is disabled, and one line of text takes their
// place. The server guard (403 feature_disabled) is the final word; this only keeps dead buttons off the screen.
import { h } from "./dom.js";

function defaultNote(text, feature) {
  return h("p", { class: "feature-note muted small", "data-feature-note": feature }, text);
}

// Hide every [data-feature] element under root whose feature is in `disabled`. Idempotent: the note is added once.
// `makeNote(text, feature)` is injectable so node tests need no DOM.
export function applyFeatures(root, disabled, makeNote = defaultNote) {
  if (!disabled) return;
  for (const el of root.querySelectorAll("[data-feature]")) {
    const feature = el.dataset.feature;
    if (!Object.hasOwn(disabled, feature)) continue;
    el.hidden = true;
    const next = el.nextElementSibling;
    if (next && next.dataset && next.dataset.featureNote === feature) continue;
    el.after(makeNote(disabled[feature], feature));
  }
}

// The server marks its shell (<html data-edition="server">, server/asgi.py); the local file has no mark.
export function pageEdition() {
  if (typeof document === "undefined") return "local";
  return document.documentElement.dataset.edition === "server" ? "server" : "local";
}

// What the server edition always turns off (= scripts/ui/server.py FEATURE_TEXT_SERVER, tests compare them).
// Used when /api/features cannot be read on a server page, so dead buttons still stay hidden.
export const SERVER_DISABLED = {
  library: "공용 라이브러리는 이 서버에서 아직 쓸 수 없습니다",
  caption_video: "자막 영상 만들기는 이 서버에서 쓸 수 없습니다",
  srtfit: "자막 시간 맞추기는 이 서버에서 쓸 수 없습니다(편집한 영상 파일이 필요해요)",
  footage: "촬영 영상 처리는 이 서버에서 쓸 수 없습니다",
};

// Read the edition and the disabled list. Local page: an older or unreachable server means the local edition with
// nothing disabled (the local edition's default). Server page: one retry, then the server edition with its fixed
// disabled list (never the local UI).
export async function loadFeatureInfo(api, hint = pageEdition()) {
  const tries = hint === "server" ? 2 : 1;
  for (let i = 0; i < tries; i += 1) {
    try {
      const data = await api("GET", "/api/features");
      const disabled = data && typeof data.disabled === "object" && data.disabled ? data.disabled : {};
      const named = data && (data.edition === "server" || data.edition === "local") ? data.edition : null;
      return { edition: named || hint, disabled };
    } catch {
      // try again (server page) or fall through to the default below
    }
  }
  return hint === "server" ? { edition: "server", disabled: { ...SERVER_DISABLED } } : { edition: "local", disabled: {} };
}

// Read the disabled list only.
export async function loadFeatures(api) {
  return (await loadFeatureInfo(api)).disabled;
}

// Pages draw their boxes late and redraw them (replaceWith), so keep applying while the DOM changes.
// Our own edits are dropped from the observer's queue so it never re-triggers itself.
export function watchFeatures(root, disabled) {
  if (!disabled || Object.keys(disabled).length === 0) return null;
  const run = () => { applyFeatures(root, disabled); observer.takeRecords(); };
  const observer = new MutationObserver(run);
  observer.observe(root, { childList: true, subtree: true });
  run();
  return observer;
}
