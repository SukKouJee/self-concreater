// 관리 화면 (서버판, #/admin): 초대한 사람 목록·초대·사람별 관리(비밀번호 재설정, 모든 기기 로그아웃, 비활성/다시 쓰기,
// 일 한도, 관리자 지정)·최근 7일 AI 사용량·최근 관리 기록.
//   서버가 최종 판단을 한다: /api/admin/* 는 관리자가 아니면 403, DB 함수도 따로 막는다. 이 화면은 관리자에게만 메뉴로 보인다.
//   비밀번호는 보내기만 하고 화면에 다시 보여 주지 않는다(입력칸은 저장 뒤 비운다). 확인은 화면 안 확인 상자(브라우저 창 없음).
import { h, fill } from "/static/dom.js";
import { button } from "/static/ui.js";
import {
  PASSWORD_MIN, adminErrorText, auditLine, checkInvite, checkPassword, loginMethods, parseLimit, shortTime, spend,
  statusChips, usageTotals, usd,
} from "/static/admin-utils.js";

const USAGE_DAYS = 7;
const AUDIT_LIMIT = 50;
const ignorable = (e) => !e || e.error === "aborted";

let S = null;

function alive(ctx) {
  return !!S && S.ctx === ctx && ctx.isCurrent();
}

function focusNode(node) {
  if (!node) return;
  if (!/^(INPUT|TEXTAREA|BUTTON|SELECT|A)$/.test(node.tagName)) node.setAttribute("tabindex", "-1");
  node.focus({ preventScroll: true });
}

function section(id, title, lead) {
  const body = h("div", { class: "admin-body", id: `${id}-body` });
  const sec = h("section", { class: "card admin-card", "aria-labelledby": `${id}-t` },
    h("div", { class: "card-head" }, h("h2", { id: `${id}-t` }, title), lead ? h("p", { class: "muted small" }, lead) : null),
    body);
  return { sec, body };
}

function loadingNode() {
  return h("p", { class: "muted", role: "status" }, "불러오는 중…");
}

function errorBox(text, retry) {
  return h("div", { class: "admin-error", role: "alert" }, h("p", { class: "error" }, text),
    retry ? button("다시 시도", { size: "s", onClick: retry }) : null);
}

// ---- mount ------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  const users = section("adm-users", "사용자", "상태, 로그인 방법, 오늘 AI 사용량. 이름 옆 관리 버튼으로 바꿉니다.");
  const invite = section("adm-invite", "초대", "초대한 이메일만 로그인할 수 있습니다.");
  const usage = section("adm-usage", `최근 ${USAGE_DAYS}일 AI 사용량`, "한국 시간 날짜별 합계입니다.");
  const audit = section("adm-audit", "최근 관리 기록", `최근 ${AUDIT_LIMIT}개. 관리자가 바꾼 것만 남습니다.`);
  S = {
    el, ctx, nodes: { users: users.body, invite: invite.body, usage: usage.body, audit: audit.body },
    users: null, usersError: null, defaultUsd: null, usage: null, usageError: null, audit: null, auditError: null,
    methods: null, openId: null, confirm: null, busy: false, panelError: null, inputs: null,
    inviteForm: null, inviteErrors: {}, inviteError: null, inviteBusy: false,
  };
  const page = h("div", { class: "admin-page" },
    h("h1", {}, "관리"),
    users.sec, invite.sec, usage.sec, audit.sec);
  fill(el, page);
  fill(S.nodes.users, loadingNode());
  fill(S.nodes.usage, loadingNode());
  fill(S.nodes.audit, loadingNode());
  buildInvite();
  await loadUsers(ctx);
  if (!alive(ctx)) return;
  if (S.usersError && S.usersError.blocked) {
    // not an admin (403) or the local edition (404): one line instead of the page
    fill(el, h("div", { class: "admin-page" }, h("h1", {}, "관리"),
      h("p", { class: "feature-note muted", role: "status" }, S.usersError.text)));
    return;
  }
  await Promise.all([loadUsage(ctx), loadAudit(ctx), loadMethods(ctx)]);
}

export function unmount() {
  S = null;
}

// Typed but not sent: the invite form or an open password / limit box.
export function unsavedNote() {
  if (!S) return null;
  const f = S.inviteForm;
  if (f && (f.email.value.trim() || f.password.value)) return "보내지 않은 초대 입력";
  if (S.inputs && S.inputs.password.value) return "저장하지 않은 새 비밀번호 입력";
  return null;
}

// ---- loads ------------------------------------------------------------------------

async function loadUsers(ctx) {
  try {
    const data = await ctx.api("GET", "/api/admin/users");
    if (!alive(ctx)) return;
    S.users = data.users || [];
    S.defaultUsd = data.default_daily_usd;
    S.usersError = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    const blocked = e.error === "admin_only" || e.status === 404;
    S.usersError = { text: adminErrorText(e), blocked };
  }
  renderUsers();
}

async function loadUsage(ctx) {
  try {
    const data = await ctx.api("GET", `/api/admin/usage?days=${USAGE_DAYS}`);
    if (!alive(ctx)) return;
    S.usage = data;
    S.usageError = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    S.usageError = adminErrorText(e);
  }
  renderUsage();
}

async function loadAudit(ctx) {
  try {
    const data = await ctx.api("GET", `/api/admin/audit?limit=${AUDIT_LIMIT}`);
    if (!alive(ctx)) return;
    S.audit = data.entries || [];
    S.auditError = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    S.auditError = adminErrorText(e);
  }
  renderAudit();
}

// Which login methods the server offers: the invite form's password hint follows it. Unknown = say nothing extra.
async function loadMethods(ctx) {
  try {
    const m = await ctx.api("GET", "/auth/methods");
    if (!alive(ctx)) return;
    S.methods = m;
    renderInviteHint();
  } catch { /* keep the plain hint */ }
}

// ---- users table ------------------------------------------------------------------------

function userById(id) {
  return (S.users || []).find((u) => u.id === id) || null;
}

function renderUsers() {
  const box = S.nodes.users;
  if (S.usersError) {
    fill(box, errorBox(S.usersError.text, () => { fill(box, loadingNode()); loadUsers(S.ctx); }));
    return;
  }
  if (!S.users) { fill(box, loadingNode()); return; }
  if (S.users.length === 0) {
    fill(box, h("p", { class: "muted" }, "아직 초대한 사람이 없습니다. 아래에서 초대하세요."));
    return;
  }
  const head = h("thead", {}, h("tr", {},
    h("th", { scope: "col" }, "사용자"), h("th", { scope: "col" }, "상태"), h("th", { scope: "col" }, "로그인 방법"),
    h("th", { scope: "col" }, "오늘 사용 / 한도"), h("th", { scope: "col" }, h("span", { class: "sr-only" }, "관리"))));
  const body = h("tbody", {});
  for (const u of S.users) {
    body.append(userRow(u));
    if (u.id === S.openId) body.append(panelRow(u));
  }
  fill(box, h("div", { class: "admin-table-wrap" }, h("table", { class: "admin-table" },
    h("caption", { class: "sr-only" }, "초대한 사용자"), head, body)));
}

function userRow(u) {
  const s = spend(u);
  const open = u.id === S.openId;
  const toggle = h("button", {
    type: "button", class: "btn btn-s", "aria-expanded": String(open), "aria-controls": `adm-panel-${u.id}`,
    "data-user": u.id, "aria-label": `${u.email} 관리`,
  }, open ? "닫기" : "관리");
  toggle.addEventListener("click", () => togglePanel(u.id));
  return h("tr", { class: u.disabled ? "is-off" : null },
    h("td", { "data-label": "사용자" },
      h("span", { class: "admin-name" }, u.display_name || u.email),
      u.display_name ? h("span", { class: "admin-email muted" }, u.email) : null),
    h("td", { "data-label": "상태" }, h("span", { class: "admin-chips" },
      ...statusChips(u).map((c) => h("span", { class: `admin-chip admin-chip-${c.kind}` }, c.label)))),
    h("td", { "data-label": "로그인 방법" }, loginMethods(u).join(" · ")),
    h("td", { "data-label": "오늘 사용 / 한도" },
      h("span", { class: s.over ? "admin-spend is-over" : "admin-spend", "aria-label": s.label }, s.text),
      h("span", { class: "admin-spend-note muted" }, s.note)),
    h("td", { class: "admin-actions-cell" }, toggle));
}

function togglePanel(id) {
  const opening = S.openId !== id;
  S.openId = opening ? id : null;
  S.confirm = null;
  S.panelError = null;
  S.inputs = opening ? newInputs(userById(id)) : null;
  renderUsers();
  if (opening) focusNode(document.getElementById(`adm-panel-${id}`));
  else focusNode(S.nodes.users.querySelector(`button[data-user="${CSS.escape(id)}"]`));
}

function newInputs(u) {
  const password = h("input", {
    type: "password", id: "adm-new-pw", autocomplete: "new-password", minlength: String(PASSWORD_MIN),
    "aria-describedby": "adm-new-pw-hint",
  });
  const limit = h("input", {
    type: "text", id: "adm-limit", inputmode: "decimal", autocomplete: "off",
    value: u && typeof u.daily_usd_limit === "number" ? u.daily_usd_limit.toFixed(2) : "",
    placeholder: `기본 ${usd(S.defaultUsd)}`, "aria-describedby": "adm-limit-hint",
  });
  return { password, limit };
}

// ---- one user's panel ---------------------------------------------------------------------

const CONFIRMS = {
  clear: (u) => ({ text: `${u.email}의 비밀번호를 지울까요? 비밀번호로는 로그인할 수 없게 되고, 모든 기기에서 로그아웃됩니다.`,
    yes: "비밀번호 지우기", danger: true }),
  logout: (u) => ({ text: `${u.email}을(를) 모든 기기에서 로그아웃시킬까요? 계정은 그대로이고 다시 로그인하면 됩니다.`,
    yes: "모두 로그아웃", danger: false }),
  disable: (u) => ({ text: `${u.email}을(를) 비활성으로 바꿀까요? 바로 로그아웃되고 다시 로그인할 수 없습니다. 데이터는 지우지 않습니다.`,
    yes: "비활성으로 바꾸기", danger: true }),
  promote: (u) => ({ text: `${u.email}을(를) 관리자로 지정할까요? 이 화면에서 모든 사용자를 관리할 수 있게 됩니다.`,
    yes: "관리자로 지정", danger: false }),
  demote: (u) => ({ text: `${u.email}을(를) 관리자에서 뺄까요?`, yes: "관리자에서 빼기", danger: true }),
};

function panelRow(u) {
  return h("tr", { class: "admin-panel-row" }, h("td", { colspan: "5" }, panel(u)));
}

function panel(u) {
  const box = h("div", { class: "admin-panel", id: `adm-panel-${u.id}`, role: "group", "aria-label": `${u.email} 관리` });
  box.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    if (S.confirm) { S.confirm = null; renderUsers(); focusNode(document.getElementById(`adm-panel-${u.id}`)); } else togglePanel(u.id);
  });
  const dis = S.busy;
  const pw = S.inputs.password;
  const lim = S.inputs.limit;
  pw.disabled = dis;
  lim.disabled = dis;

  const savePw = button("비밀번호 저장", { size: "s", variant: "primary", disabled: dis || null,
    onClick: () => savePassword(u) });
  const clear = u.has_password ? button("비밀번호 지우기", { size: "s", disabled: dis || null, onClick: () => ask("clear") }) : null;
  const pwGroup = h("div", { class: "admin-group" },
    h("h3", {}, "비밀번호"),
    h("label", { for: "adm-new-pw" }, "새 비밀번호"),
    h("div", { class: "admin-inline" }, pw, savePw),
    h("p", { class: "muted small", id: "adm-new-pw-hint" },
      `${PASSWORD_MIN}자 이상. 저장하면 이 사람은 모든 기기에서 로그아웃됩니다. 정한 비밀번호는 직접 알려 주세요(화면에 다시 나오지 않습니다).`),
    clear ? h("div", { class: "btn-row" }, clear) : null);

  const saveLimit = button("한도 저장", { size: "s", disabled: dis || null, onClick: () => saveLimitValue(u) });
  const limGroup = h("div", { class: "admin-group" },
    h("h3", {}, "하루 AI 한도"),
    h("label", { for: "adm-limit" }, "달러"),
    h("div", { class: "admin-inline" }, lim, saveLimit),
    h("p", { class: "muted small", id: "adm-limit-hint" }, `비워 두면 서버 기본값(${usd(S.defaultUsd)})을 씁니다.`));

  const logout = button("모든 기기에서 로그아웃", { size: "s", disabled: dis || null, onClick: () => ask("logout") });
  const roleBtn = u.is_admin
    ? (u.is_self ? null : button("관리자에서 빼기", { size: "s", disabled: dis || null, onClick: () => ask("demote") }))
    : button("관리자로 지정", { size: "s", disabled: dis || null, onClick: () => ask("promote") });
  let stateBtn = null;
  if (u.disabled) stateBtn = button("다시 쓰게 하기", { size: "s", variant: "primary", disabled: dis || null, onClick: () => act(u, "enable") });
  else if (!u.is_self) stateBtn = button("비활성으로 바꾸기", { size: "s", disabled: dis || null, onClick: () => ask("disable") });
  const accGroup = h("div", { class: "admin-group" },
    h("h3", {}, "계정"),
    h("div", { class: "btn-row" }, logout, roleBtn, stateBtn),
    u.is_self ? h("p", { class: "muted small" }, "내 계정은 비활성으로 바꾸거나 관리자에서 뺄 수 없습니다.") : null);

  box.append(pwGroup, limGroup, accGroup);
  if (S.confirm) box.append(confirmBox(u));
  if (S.panelError) box.append(h("p", { class: "error", role: "alert", id: "adm-panel-err" }, S.panelError));
  return box;
}

function ask(kind) {
  S.confirm = kind;
  S.panelError = null;
  renderUsers();
  const no = document.getElementById("adm-confirm-no");
  if (no) no.focus();
}

function confirmBox(u) {
  const c = CONFIRMS[S.confirm](u);
  const run = {
    clear: () => act(u, "clear-password"), logout: () => act(u, "logout-all"), disable: () => act(u, "disable"),
    promote: () => act(u, "admin", { is_admin: true }), demote: () => act(u, "admin", { is_admin: false }),
  }[S.confirm];
  const yes = button(c.yes, { size: "s", variant: c.danger ? "outline" : "primary", disabled: S.busy || null, onClick: run });
  if (c.danger) yes.classList.add("btn-danger");
  const no = button("취소", { size: "s", id: "adm-confirm-no", disabled: S.busy || null, onClick: () => {
    S.confirm = null;
    renderUsers();
    focusNode(document.getElementById(`adm-panel-${u.id}`));
  } });
  return h("div", { class: "inline-confirm", role: "alert" }, h("p", { class: "confirm-text" }, c.text),
    h("div", { class: "btn-row" }, yes, no));
}

const DONE = {
  "clear-password": "비밀번호를 지웠습니다",
  "logout-all": "모든 기기에서 로그아웃시켰습니다",
  disable: "비활성으로 바꿨습니다",
  enable: "다시 쓸 수 있게 했습니다",
  admin: "관리자 권한을 바꿨습니다",
  limit: "하루 한도를 바꿨습니다",
  password: "비밀번호를 바꿨습니다. 모든 기기에서 로그아웃됩니다",
};

async function act(u, action, body = {}) {
  const ctx = S.ctx;
  if (S.busy) return;
  S.busy = true;
  S.panelError = null;
  renderUsers();
  try {
    await ctx.api("POST", `/api/admin/users/${encodeURIComponent(u.id)}/${action}`, body);
    if (!alive(ctx)) return;
    ctx.toast(`${u.email}: ${DONE[action]}`, "info");
    S.confirm = null;
    if (action === "password") S.inputs.password.value = "";
    S.busy = false;
    await Promise.all([loadUsers(ctx), loadAudit(ctx)]);
    if (!alive(ctx)) return;
    focusNode(document.getElementById(`adm-panel-${u.id}`));
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    S.busy = false;
    S.panelError = adminErrorText(e);
    renderUsers();
    focusNode(document.getElementById("adm-panel-err"));
  }
}

function savePassword(u) {
  const value = S.inputs.password.value;
  const bad = checkPassword(value);
  if (bad) {
    S.panelError = bad;
    renderUsers();
    S.inputs.password.setAttribute("aria-invalid", "true");
    S.inputs.password.focus();
    return;
  }
  S.inputs.password.removeAttribute("aria-invalid");
  act(u, "password", { password: value });
}

function saveLimitValue(u) {
  const parsed = parseLimit(S.inputs.limit.value);
  if (!parsed.ok) {
    S.panelError = parsed.error;
    S.inputs.limit.setAttribute("aria-invalid", "true");
    renderUsers();
    S.inputs.limit.focus();
    return;
  }
  S.inputs.limit.removeAttribute("aria-invalid");
  act(u, "limit", { daily_usd_limit: parsed.value });
}

// ---- invite -------------------------------------------------------------------------------

function field(id, label, input, hint) {
  const err = h("p", { class: "field-error error", id: `${id}-err`, hidden: true });
  input.setAttribute("aria-describedby", hint ? `${id}-hint ${id}-err` : `${id}-err`);
  return {
    node: h("div", { class: "field" }, h("label", { for: id }, label), input,
      hint ? h("p", { class: "muted small", id: `${id}-hint` }, hint) : null, err),
    err,
  };
}

function buildInvite() {
  const email = h("input", { type: "email", id: "adm-inv-email", autocomplete: "off", required: true, spellcheck: "false" });
  const name = h("input", { type: "text", id: "adm-inv-name", autocomplete: "off", maxlength: "100" });
  const password = h("input", { type: "password", id: "adm-inv-pw", autocomplete: "new-password" });
  const limit = h("input", { type: "text", id: "adm-inv-limit", inputmode: "decimal", autocomplete: "off" });
  const isAdmin = h("input", { type: "checkbox", id: "adm-inv-admin" });
  const fields = {
    email: field("adm-inv-email", "이메일", email, "이 사람이 로그인할 이메일(Google 계정 또는 비밀번호 로그인)."),
    name: field("adm-inv-name", "이름 (선택)", name, null),
    password: field("adm-inv-pw", "첫 비밀번호 (선택)", password, passwordHint()),
    limit: field("adm-inv-limit", "하루 AI 한도 (선택, 달러)", limit, "비워 두면 서버 기본값을 씁니다."),
  };
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "초대");
  const status = h("div", { id: "adm-inv-status" });
  const form = h("form", { class: "add-form edit-form admin-invite", novalidate: true },
    fields.email.node, fields.name.node, fields.password.node, fields.limit.node,
    h("label", { class: "admin-check", for: "adm-inv-admin" }, isAdmin, h("span", {}, "관리자로 초대 (이 화면을 쓸 수 있습니다)")),
    h("div", { class: "btn-row" }, submit), status);
  form.addEventListener("submit", (e) => { e.preventDefault(); sendInvite(); });
  S.inviteForm = { form, email, name, password, limit, isAdmin, fields, submit, status };
  fill(S.nodes.invite, form);
}

function passwordHint() {
  const base = `${PASSWORD_MIN}자 이상. 비워 두면 Google 로그인만 됩니다. 정한 비밀번호는 직접 알려 주세요.`;
  if (!S || !S.methods) return base;
  if (!S.methods.password) return `${base} 지금 서버는 비밀번호 로그인이 꺼져 있습니다.`;
  if (!S.methods.google) return `${PASSWORD_MIN}자 이상. 이 서버는 Google 로그인이 꺼져 있어 비밀번호가 있어야 로그인할 수 있습니다.`;
  return base;
}

function renderInviteHint() {
  const hint = document.getElementById("adm-inv-pw-hint");
  if (hint) hint.textContent = passwordHint();
}

function showInviteErrors(errors) {
  const f = S.inviteForm;
  let first = null;
  for (const key of ["email", "name", "password", "limit"]) {
    const input = { email: f.email, name: f.name, password: f.password, limit: f.limit }[key];
    const msg = errors[key];
    f.fields[key].err.hidden = !msg;
    f.fields[key].err.textContent = msg || "";
    if (msg) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
    if (msg && !first) first = input;
  }
  return first;
}

async function sendInvite() {
  const ctx = S.ctx;
  const f = S.inviteForm;
  if (S.inviteBusy) return;
  const { errors, body } = checkInvite({ email: f.email.value, name: f.name.value, password: f.password.value,
    limit: f.limit.value, isAdmin: f.isAdmin.checked });
  const first = showInviteErrors(errors);
  fill(f.status);
  if (!body) { first.focus(); return; }
  S.inviteBusy = true;
  f.submit.disabled = true;
  f.submit.textContent = "초대하는 중…";
  try {
    await ctx.api("POST", "/api/admin/users", body);
    if (!alive(ctx)) return;
    ctx.toast(`초대했습니다: ${body.email}`, "info");
    f.form.reset();
    await Promise.all([loadUsers(ctx), loadAudit(ctx)]);
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    const err = e.error === "user_exists" ? { email: adminErrorText(e) }
      : e.error === "weak_password" ? { password: adminErrorText(e) }
        : e.error === "bad_email" ? { email: adminErrorText(e) }
          : e.error === "bad_limit" ? { limit: adminErrorText(e) } : null;
    if (err) showInviteErrors(err).focus();
    else { fill(f.status, h("p", { class: "error", role: "alert" }, adminErrorText(e))); }
  } finally {
    if (alive(ctx)) {
      S.inviteBusy = false;
      f.submit.disabled = false;
      f.submit.textContent = "초대";
    }
  }
}

// ---- usage --------------------------------------------------------------------------------

function renderUsage() {
  const box = S.nodes.usage;
  if (S.usageError) {
    fill(box, errorBox(S.usageError, () => { fill(box, loadingNode()); loadUsage(S.ctx); }));
    return;
  }
  const data = S.usage;
  if (!data) { fill(box, loadingNode()); return; }
  if (!data.rows || data.rows.length === 0) {
    fill(box, h("p", { class: "muted" }, `최근 ${USAGE_DAYS}일 동안 AI를 쓴 기록이 없습니다.`));
    return;
  }
  const totals = usageTotals(data.rows);
  const summary = h("ul", { class: "admin-totals" }, ...totals.map((t) => h("li", {},
    h("span", { class: "admin-name" }, t.email), h("span", {}, `${usd(t.cost)} · 호출 ${t.calls}번${t.failed ? `(실패 ${t.failed})` : ""}`))));
  const rows = data.rows.map((r) => h("tr", {},
    h("td", { "data-label": "날짜" }, r.day), h("td", { "data-label": "사용자" }, r.email),
    h("td", { "data-label": "호출", class: "num" }, r.failed ? `${r.calls} (실패 ${r.failed})` : String(r.calls)),
    h("td", { "data-label": "토큰(입력/출력)", class: "num" }, `${r.input_tokens.toLocaleString("ko-KR")} / ${r.output_tokens.toLocaleString("ko-KR")}`),
    h("td", { "data-label": "비용", class: "num" }, usd(r.cost_usd))));
  fill(box,
    h("p", { class: "admin-total" }, `합계 ${usd(data.total_usd)}`),
    summary,
    h("div", { class: "admin-table-wrap" }, h("table", { class: "admin-table admin-usage" },
      h("caption", { class: "sr-only" }, `최근 ${USAGE_DAYS}일 날짜·사용자별 AI 사용량`),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "날짜"), h("th", { scope: "col" }, "사용자"),
        h("th", { scope: "col", class: "num" }, "호출"), h("th", { scope: "col", class: "num" }, "토큰(입력/출력)"),
        h("th", { scope: "col", class: "num" }, "비용"))),
      h("tbody", {}, ...rows))));
}

// ---- audit --------------------------------------------------------------------------------

function renderAudit() {
  const box = S.nodes.audit;
  if (S.auditError) {
    fill(box, errorBox(S.auditError, () => { fill(box, loadingNode()); loadAudit(S.ctx); }));
    return;
  }
  if (!S.audit) { fill(box, loadingNode()); return; }
  if (S.audit.length === 0) {
    fill(box, h("p", { class: "muted" }, "아직 관리 기록이 없습니다."));
    return;
  }
  fill(box, h("ol", { class: "admin-audit" }, ...S.audit.map((entry) => {
    const line = auditLine(entry);
    return h("li", {},
      h("time", { class: "muted", datetime: entry.at }, shortTime(entry.at)),
      h("span", {}, h("strong", {}, line.who), ` → ${line.whom}: ${line.what}`));
  })));
}
