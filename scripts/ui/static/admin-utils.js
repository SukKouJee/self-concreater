// Admin screen helpers (server edition, #/admin): pure functions only, no DOM (node tests load this file).
// The server decides who is an admin (GET /api/me, and every /api/admin call checks again); these only format.

export const PASSWORD_MIN = 10;          // server.passwords.MIN_LENGTH
export const PASSWORD_MAX = 1024;
export const LIMIT_MAX = 999999.99;      // users.daily_usd_limit numeric(8,2)
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

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

// Status chips for one user row. kind: "admin" | "off" | "self" (CSS: .admin-chip-<kind>).
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
  if (!/^\d+(\.\d{1,2})?$/.test(raw)) return { ok: false, error: "일 한도는 0 이상, 소수 둘째 자리까지의 달러 숫자입니다." };
  const value = Number(raw);
  if (value > LIMIT_MAX) return { ok: false, error: `일 한도는 ${LIMIT_MAX}달러 이하여야 합니다.` };
  return { ok: true, value: raw };
}

// Invite form -> { errors: { field: message }, body } (body only when there are no errors).
export function checkInvite({ email = "", name = "", password = "", limit = "", isAdmin = false } = {}) {
  const errors = {};
  const address = String(email).trim();
  if (!EMAIL_RE.test(address)) errors.email = "이메일 형식이 아닙니다.";
  if (String(name).trim().length > 100) errors.name = "이름은 100자 이하여야 합니다.";
  if (password !== "") {
    const bad = checkPassword(password);
    if (bad) errors.password = bad;
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

const ACTIONS = {
  add_user: "초대",
  set_password: "비밀번호 재설정",
  clear_password: "비밀번호 지움",
  disable: "비활성으로 바꿈",
  enable: "다시 쓰게 함",
  logout_all: "모든 기기에서 로그아웃",
  set_limit: "일 한도 변경",
  set_admin: "관리자 권한 변경",
};

function limitText(v) {
  return typeof v === "number" ? usd(v) : "기본";
}

// One audit entry -> { who, what, whom }. A NULL actor is the operator's CLI (make-admin, revoke-admin).
export function auditLine(entry) {
  const d = entry.detail || {};
  let what = ACTIONS[entry.action] || entry.action;
  if (entry.action === "set_admin") what = d.is_admin ? "관리자로 지정" : "관리자에서 뺌";
  if (entry.action === "set_limit") what = `일 한도 ${limitText(d.from)} → ${limitText(d.to)}`;
  if (entry.action === "add_user") what = d.is_admin ? "관리자로 초대" : "초대";
  const who = entry.actor_email || (d.via === "cli" ? "운영자(명령줄)" : "알 수 없음");
  return { who, what, whom: entry.target_email || "삭제된 사용자" };
}

// ISO timestamp -> "11. 2. 14:05" style local text (the browser's time zone).
export function shortTime(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  return t.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

// Usage rows (newest day first from the server) -> totals per email for the summary line.
export function usageTotals(rows) {
  const by = new Map();
  for (const r of rows || []) {
    const cur = by.get(r.email) || { email: r.email, calls: 0, failed: 0, cost: 0 };
    cur.calls += r.calls || 0;
    cur.failed += r.failed || 0;
    cur.cost += r.cost_usd || 0;
    by.set(r.email, cur);
  }
  return [...by.values()].sort((a, b) => b.cost - a.cost || a.email.localeCompare(b.email));
}

// Error answer -> one Korean line for the screen (403 admin_only, 404 on the local edition).
export function adminErrorText(e) {
  if (!e) return "요청이 실패했습니다.";
  if (e.status === 404 && (e.error === "not_found" || e.error === "http_error")) return "관리 화면은 서버판에서만 쓸 수 있습니다.";
  if (e.error === "admin_only") return "관리자만 볼 수 있는 화면입니다.";
  return e.message || "요청이 실패했습니다.";
}
