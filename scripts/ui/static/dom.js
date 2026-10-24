// Tiny DOM helper shared by pages. Text children are always set as text (no innerHTML).
export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid);
  }
  return el;
}

// Percent-encode each segment of a work-folder relative path for /files/<path>.
export function filesUrl(relPath) {
  return "/files/" + relPath.split("/").map(encodeURIComponent).join("/");
}

// replaceChildren that skips null/undefined/false (native replaceChildren would render them as the text "null").
export function fill(el, ...kids) {
  el.replaceChildren(...kids.filter((kid) => kid !== null && kid !== undefined && kid !== false));
}
