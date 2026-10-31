// Admin screen helpers (server edition, #/admin): pure functions only, no DOM (node tests load this file).
// The server decides who is an admin (GET /api/me, and every /api/admin call checks again); these only format,
// build query strings and check form values before they are sent (the server checks them again).

export const PASSWORD_MIN = 10;          // server.passwords.MIN_LENGTH
export const PASSWORD_MAX = 1024;
export const LIMIT_MAX = 999999.99;      // users.daily_usd_limit numeric(8,2)
export const NAME_MAX = 100;
export const PER_PAGE = 50;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const DELETED = "삭제된 사용자";

export const TABS = [
  { key: "users", label: "사용자" },
  { key: "usage", label: "사용량" },
  { key: "audit", label: "감사 기록" },
  { key: "server", label: "서버 정보" },
];

export const STATUS_OPTIONS = [
  { value: "all", label: "모든 상태" }, { value: "active", label: "사용 중" }, { value: "disabled", label: "비활성" },
];
export const ROLE_OPTIONS = [
  { value: "all", label: "모든 권한" }, { value: "admin", label: "관리자" }, { value: "member", label: "일반 사용자" },
];
export const LOGIN_OPTIONS = [
  { value: "all", label: "모든 로그인 방법" }, { value: "password", label: "비밀번호" }, { value: "google", label: "Google" },
  { value: "none", label: "아직 없음" },
];
// One select for sort + order: value "<sort>:<asc|desc>".
export const SORT_OPTIONS = [
  { value: "email:asc", label: "이메일 순" },
  { value: "last_login:desc", label: "최근 로그인 순" },
  { value: "created:desc", label: "최근 추가 순" },
  { value: "created:asc", label: "오래된 추가 순" },
  { value: "today:desc", label: "오늘 사용액 많은 순" },
];
export const USAGE_DAYS = [7, 30, 90];

export const DEFAULT_USERS_QUERY = Object.freeze({ q: "", status: "all", role: "all", login: "all", sort: "email:asc", page: 1 });
export const DEFAULT_AUDIT_FILTERS = Object.freeze({ actor: "", action: "", target: "", from: "", to: "" });

// Only the server edition has /api/me; the local edition never asks (no extra request, no 404).
export async function loadIsAdmin(api, edition) {
  if (edition !== "server") return false;
  try {
    const me = await api("GET", "/api/me");
    return !!me && me.is_admin === true;
  } catch {
    return false;
  }
}

export function usd(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(2)}`;
}

// Today's spend against the limit that applies (the user's own, or the server default).
export function spend(user) {
  const used = typeof user.today_usd === "number" ? user.today_usd : 0;
  const limit = typeof user.effective_daily_usd === "number" ? user.effective_daily_usd : null;
  const own = typeof user.daily_usd_limit === "number";
  const over = limit !== null && used >= limit;
  return {
    text: `${usd(used)} / ${usd(limit)}`,
    note: own ? "개별 한도" : "기본 한도",
    over,
    label: `오늘 ${usd(used)} 사용, 한도 ${usd(limit)}(${own ? "개별" : "기본"})${over ? ", 한도에 닿음" : ""}`,
  };
}

// Status chips for one user row. kind: "on" | "off" | "admin" | "self" (CSS: .admin-chip-<kind>).
export function statusChips(user) {
  const out = [];
  if (user.disabled) out.push({ label: "비활성", kind: "off" });
  else out.push({ label: "사용 중", kind: "on" });
  if (user.is_admin) out.push({ label: "관리자", kind: "admin" });
  if (user.is_self) out.push({ label: "나", kind: "self" });
  return out;
}

export function loginMethods(user) {
  const out = [];
  if (user.has_password) out.push("비밀번호");
  if (user.has_google) out.push("Google");
  return out.length ? out : ["아직 없음"];
}

export function checkPassword(text) {
  if (typeof text !== "string" || !text.trim() || text.length < PASSWORD_MIN) {
    return `비밀번호는 ${PASSWORD_MIN}자 이상이어야 합니다(공백만으로는 안 됩니다).`;
  }
  if (text.length > PASSWORD_MAX) return `비밀번호는 ${PASSWORD_MAX}자 이하여야 합니다.`;
  return null;
}

// "" -> null (the server default). Otherwise dollars with at most two decimals, 0..LIMIT_MAX.
export function parseLimit(text) {
  const raw = String(text === null || text === undefined ? "" : text).trim();
  if (!raw) return { ok: true, value: null };
  if (!/^\d+(\.\d{1,2})?$/.test(raw)) return { ok: false, error: "하루 한도는 0 이상, 소수 둘째 자리까지의 달러 숫자입니다." };
  const value = Number(raw);
  if (value > LIMIT_MAX) return { ok: false, error: `하루 한도는 ${LIMIT_MAX}달러 이하여야 합니다.` };
  return { ok: true, value: raw };
}

function checkEmail(text) {
  return EMAIL_RE.test(String(text).trim()) ? null : "이메일 형식이 아닙니다.";
}

function checkName(text) {
  return String(text).trim().length > NAME_MAX ? `이름은 ${NAME_MAX}자 이하여야 합니다.` : null;
}

// Create form -> { errors: { field: message }, body } (body only when there are no errors).
// passwordRequired: the server has no Google login, so an account without a password could never log in.
export function checkCreate({ email = "", name = "", password = "", limit = "", isAdmin = false,
  passwordRequired = false } = {}) {
  const errors = {};
  const address = String(email).trim();
  const badEmail = checkEmail(address);
  if (badEmail) errors.email = badEmail;
  const badName = checkName(name);
  if (badName) errors.name = badName;
  if (password !== "") {
    const bad = checkPassword(password);
    if (bad) errors.password = bad;
  } else if (passwordRequired) {
    errors.password = `이 서버는 Google 로그인이 꺼져 있어 비밀번호가 필요합니다(${PASSWORD_MIN}자 이상).`;
  }
  const lim = parseLimit(limit);
  if (!lim.ok) errors.limit = lim.error;
  if (Object.keys(errors).length) return { errors, body: null };
  return {
    errors,
    body: { email: address, display_name: String(name).trim() || null, password: password || null,
      daily_usd_limit: lim.value, is_admin: !!isAdmin },
  };
}

function limitText(v) {
  return typeof v === "number" ? usd(v) : "기본";
}

function sameLimit(current, parsed) {
  if (parsed === null) return current === null || current === undefined;
  return typeof current === "number" && Math.abs(current - Number(parsed)) < 0.005;
}

// Edit form against the loaded user -> { errors, body (only the changed fields, null when nothing changed or
// there are errors), changed: [field], warnings: [text] }. Warnings need an on-screen yes before saving.
export function checkEdit(user, { email = "", name = "", limit = "", isAdmin = false, enabled = true } = {}) {
  const errors = {};
  const body = {};
  const warnings = [];
  const address = String(email).trim().toLowerCase();
  const badEmail = checkEmail(address);
  if (badEmail) errors.email = badEmail;
  else if (address !== user.email) {
    body.email = address;
    warnings.push(user.has_google
      ? "이메일을 바꾸면 Google 계정 연결이 풀리고, 이 사람은 모든 기기에서 로그아웃됩니다. 새 이메일로 다시 로그인해야 합니다."
      : "이메일을 바꾸면 이 사람은 모든 기기에서 로그아웃되고 새 이메일로 다시 로그인해야 합니다.");
    if (user.is_self) warnings.push("내 이메일입니다. 저장하면 나도 로그아웃됩니다.");
  }
  const badName = checkName(name);
  if (badName) errors.name = badName;
  else {
    const trimmed = String(name).trim() || null;
    if (trimmed !== (user.display_name || null)) body.display_name = trimmed;
  }
  const lim = parseLimit(limit);
  if (!lim.ok) errors.limit = lim.error;
  else if (!sameLimit(user.daily_usd_limit, lim.value)) body.daily_usd_limit = lim.value;
  if (!!isAdmin !== !!user.is_admin) {
    body.is_admin = !!isAdmin;
    warnings.push(isAdmin ? "관리자가 되면 이 화면에서 모든 사용자를 관리할 수 있습니다."
      : "관리자에서 빼면 이 사람은 관리 화면을 더 쓸 수 없습니다.");
  }
  if (!!enabled === !!user.disabled) {
    body.disabled = !enabled;
    if (!enabled) warnings.push("비활성으로 바꾸면 바로 로그아웃되고 다시 로그인할 수 없습니다. 데이터는 지우지 않습니다.");
  }
  if (Object.keys(errors).length) return { errors, body: null, changed: [], warnings: [] };
  const changed = Object.keys(body);
  return { errors, body: changed.length ? body : null, changed, warnings };
}

// The delete box: the typed text must be the user's email (spaces around and letter case do not matter).
export function deleteMatches(typed, email) {
  return typeof typed === "string" && typeof email === "string" && !!email
    && typed.trim().toLowerCase() === email.toLowerCase();
}

// Users list state -> query string for GET /api/admin/users (defaults are left out).
export function usersQuery(state = DEFAULT_USERS_QUERY) {
  const p = new URLSearchParams();
  const q = String(state.q || "").trim();
  if (q) p.set("q", q);
  for (const key of ["status", "role", "login"]) {
    if (state[key] && state[key] !== "all") p.set(key, state[key]);
  }
  const [sort, order] = String(state.sort || "email:asc").split(":");
  if (sort && sort !== "email") p.set("sort", sort);
  if (order === "desc") p.set("order", "desc");
  if (state.page && state.page > 1) p.set("page", String(state.page));
  const text = p.toString();
  return text ? `?${text}` : "";
}

export function hasUserFilters(state) {
  return !!String(state.q || "").trim() || ["status", "role", "login"].some((k) => state[k] && state[k] !== "all");
}

export function auditQuery(filters = DEFAULT_AUDIT_FILTERS, page = 1) {
  const p = new URLSearchParams();
  for (const key of ["actor", "action", "target", "from", "to"]) {
    const v = String(filters[key] || "").trim();
    if (v) p.set(key, v);
  }
  if (page > 1) p.set("page", String(page));
  const text = p.toString();
  return text ? `?${text}` : "";
}

export function hasAuditFilters(filters) {
  return ["actor", "action", "target", "from", "to"].some((k) => String(filters[k] || "").trim());
}

// Date range check for the audit filter (YYYY-MM-DD from <input type=date>); null when fine.
export function checkRange(from, to) {
  if (from && to && from > to) return "시작 날짜가 끝 날짜보다 늦습니다.";
  return null;
}

// "1–50 / 120명" style line for a page of a list.
export function pageText({ page = 1, per_page: perPage = PER_PAGE, total = 0 } = {}, unit = "명") {
  if (!total) return `0${unit}`;
  const from = (page - 1) * perPage + 1;
  const to = Math.min(total, page * perPage);
  return from > total ? `${total}${unit}` : `${total}${unit} 중 ${from}–${to}`;
}

export function bytesText(n) {
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

// ISO timestamp -> "2026. 11. 2." (the browser's time zone).
export function dateText(iso) {
  if (!iso) return "—";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "—";
  return t.toLocaleDateString("ko-KR", { year: "numeric", month: "numeric", day: "numeric" });
}

// Last login -> short relative text. now is ms since epoch (tests pass a fixed value).
export function loginText(iso, now = Date.now()) {
  if (!iso) return "아직 없음";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const minutes = Math.floor((now - t) / 60000);
  if (minutes < 1) return "방금";
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  const days = Math.floor(hours / 24);
  if (days <= 30) return `${days}일 전`;
  return dateText(iso);
}

export const ACTION_LABELS = {
  add_user: "사용자 추가",
  set_email: "이메일 변경",
  set_name: "이름 변경",
  set_password: "비밀번호 정함",
  clear_password: "비밀번호 지움",
  disable: "비활성으로 바꿈",
  enable: "다시 쓰게 함",
  logout_all: "모든 기기에서 로그아웃",
  set_limit: "하루 한도 변경",
  set_admin: "관리자 권한 변경",
  delete_user: "사용자 삭제",
};

// The audit filter's action choices: the server's list (unknown names keep their raw text).
export function actionOptions(actions) {
  return [{ value: "", label: "모든 동작" },
    ...(actions || Object.keys(ACTION_LABELS)).map((a) => ({ value: a, label: ACTION_LABELS[a] || a }))];
}

// One audit entry -> { who, what, whom }. A NULL actor with via: cli is the operator's command line.
export function auditLine(entry) {
  const d = entry.detail || {};
  let what = ACTION_LABELS[entry.action] || entry.action;
  if (entry.action === "set_admin") what = d.is_admin ? "관리자로 지정" : "관리자에서 뺌";
  if (entry.action === "set_limit") what = `하루 한도 ${limitText(d.from)} → ${limitText(d.to)}`;
  if (entry.action === "add_user") what = d.is_admin ? "관리자로 추가" : "사용자 추가";
  if (entry.action === "set_email") what = `이메일 변경 ${d.from || "?"} → ${d.to || "?"}`;
  if (entry.action === "set_name") what = `이름 변경 ${d.from || "(없음)"} → ${d.to || "(없음)"}`;
  if (entry.action === "delete_user") {
    what = `사용자 삭제(문서 ${d.docs || 0}개, 파일 ${d.objects || 0}개, 휴지통 ${d.trash || 0}개)`;
  }
  const who = entry.actor_email || (d.via === "cli" ? "운영자(관리 명령)" : "알 수 없음");
  return { who, what, whom: entry.target_email || DELETED };
}

// ISO timestamp -> "11. 2. 14:05" style local text (the browser's time zone).
export function shortTime(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  return t.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

// A usage row's email; deleted users' spend stays with no email.
export function usageEmail(email) {
  return email || DELETED;
}

// Usage rows (newest day first from the server) -> totals per email for the summary line.
export function usageTotals(rows) {
  const by = new Map();
  for (const r of rows || []) {
    const email = usageEmail(r.email);
    const cur = by.get(email) || { email, calls: 0, failed: 0, cost: 0 };
    cur.calls += r.calls || 0;
    cur.failed += r.failed || 0;
    cur.cost += r.cost_usd || 0;
    by.set(email, cur);
  }
  return [...by.values()].sort((a, b) => b.cost - a.cost || a.email.localeCompare(b.email));
}

// Delete answer -> toast text and whether part of the storage is left (the server logged it for S10).
export function deleteDoneText(out) {
  const d = (out && out.deleted) || {};
  const base = `${out.email} 계정을 지웠습니다(문서 ${d.docs || 0}개, 파일 ${d.objects || 0}개, ${bytesText(d.bytes || 0)}).`;
  const s = (out && out.storage) || {};
  if (s.ok === false) {
    return { text: `${base} 저장소의 파일 ${s.left}개는 지우지 못해 서버 기록에 남겼습니다.`, warn: true };
  }
  return { text: base, warn: false };
}

// Server info: login methods as lines { label, on, note }.
export function loginLines(login = {}) {
  return [
    { label: "Google 로그인", on: !!login.google, note: login.google ? "초대한 이메일의 Google 계정으로 로그인합니다." : "꺼짐" },
    { label: "이메일 + 비밀번호", on: !!login.password,
      note: login.password ? "관리자가 정한 비밀번호로 로그인합니다." : "꺼짐(비밀번호를 정해 둬도 쓸 수 없습니다)" },
    { label: "개발용 로그인", on: !!login.dev,
      note: login.dev ? "켜져 있습니다. 실제 운영 서버에서는 꺼 두세요." : "꺼짐", warn: !!login.dev },
  ];
}

// Error answer -> one Korean line for the screen (403 admin_only, 404 on the local edition).
export function adminErrorText(e) {
  if (!e) return "요청이 실패했습니다.";
  if (e.status === 404 && (e.error === "not_found" || e.error === "http_error")) return "관리 화면은 서버판에서만 쓸 수 있습니다.";
  if (e.error === "admin_only") return "관리자만 볼 수 있는 화면입니다.";
  return e.message || "요청이 실패했습니다.";
}
