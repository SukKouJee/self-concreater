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

// Read the edition and the disabled list. An older or unreachable server means the local edition with nothing
// disabled (the local edition's default).
export async function loadFeatureInfo(api) {
  try {
    const data = await api("GET", "/api/features");
    const disabled = data && typeof data.disabled === "object" && data.disabled ? data.disabled : {};
    return { edition: data && data.edition === "server" ? "server" : "local", disabled };
  } catch {
    return { edition: "local", disabled: {} };
  }
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
