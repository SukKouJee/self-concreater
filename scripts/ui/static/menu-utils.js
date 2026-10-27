// Pure helpers for the side menu (no DOM, no imports) so node tests can load them.

// Menus whose detail screens live under "<route>/..." (e.g. #/refs/<id>) stay current on those screens.
const PREFIX_ROUTES = ["#/refs", "#/conti", "#/draft", "#/diagnose", "#/report", "#/guide"];

// Routes that open another menu item's screen (#/add is the add area of the 레퍼런스 screen).
const ALIASES = { "#/refs": ["#/add"] };

export function isActive(href, hash) {
  const current = hash || "#/home";
  return current === href || (ALIASES[href] || []).includes(current)
    || (PREFIX_ROUTES.includes(href) && current.startsWith(href + "/"));
}

// Name of the group holding the current screen, or null (top-level item or unknown route).
// hrefOf maps an item to the href the menu link uses (app.js passes itemHref so "soon" items match too).
export function activeGroup(menu, hash, hrefOf = (it) => it.route) {
  for (const g of menu) {
    if (g.group && g.items.some((it) => isActive(hrefOf(it), hash))) return g.group;
  }
  return null;
}

// Stored collapsed-group list -> array of unique group names. Anything unreadable means "all open".
export function parseCollapsed(raw) {
  let value;
  try { value = JSON.parse(raw); } catch { return []; }
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((v) => typeof v === "string"))];
}

export function toggleCollapsed(collapsed, group) {
  return collapsed.includes(group) ? collapsed.filter((g) => g !== group) : [...collapsed, group];
}

// Opening a screen inside a collapsed group expands that group (and remembers it), so the current item never
// hides; the user can still collapse it again afterwards.
export function expandFor(collapsed, current) {
  return current && collapsed.includes(current) ? collapsed.filter((g) => g !== current) : collapsed;
}

// Expand only when the route moves into a group from elsewhere (prev is undefined before the first route), so a
// group the user collapsed while its screen is open stays collapsed across reloads and within-group navigation.
export function enterExpand(collapsed, prev, current) {
  return prev !== undefined && current !== prev ? expandFor(collapsed, current) : collapsed;
}
