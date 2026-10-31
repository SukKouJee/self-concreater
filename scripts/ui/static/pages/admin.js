// 관리 화면 (서버판, #/admin): 탭 네 개 — 사용자(검색·필터·정렬·쪽, 추가, 자세히: 고치기·비밀번호·로그아웃·삭제) /
// 사용량(7·30·90일) / 감사 기록(필터·쪽) / 서버 정보(사람 수, 사용액, 로그인 방법, 기본 한도, 꺼진 기능).
//   서버가 최종 판단을 한다: /api/admin/* 는 관리자가 아니면 403, DB 함수도 따로 막는다. 이 화면은 관리자에게만 메뉴로 보인다.
//   비밀번호는 보내기만 하고 화면에 다시 보여 주지 않는다(입력칸은 저장 뒤 비운다). 확인은 화면 안 확인 상자(브라우저 창 없음).
//   사용자 삭제는 그 사람의 이메일을 그대로 입력해야 버튼이 켜진다(서버도 다시 확인한다).
import { h, fill } from "/static/dom.js";
import { button, tabs } from "/static/ui.js";
import {
  DEFAULT_AUDIT_FILTERS, DEFAULT_USERS_QUERY, LOGIN_OPTIONS, PASSWORD_MIN, ROLE_OPTIONS, SORT_OPTIONS, STATUS_OPTIONS,
  TABS, USAGE_DAYS, actionOptions, adminErrorText, auditLine, auditQuery, bytesText, checkCreate, checkEdit,
  checkPassword, checkRange, dateText, deleteDoneText, deleteMatches, hasAuditFilters, hasUserFilters, loginLines,
  loginMethods, loginText, pageText, shortTime, spend, statusChips, usageEmail, usageTotals, usd, usersQuery,
} from "/static/admin-utils.js";

const SEARCH_DELAY = 300;
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

function loadingNode(text = "불러오는 중…") {
  return h("p", { class: "muted admin-loading", role: "status" }, text);
}

function errorBox(text, retry) {
  return h("div", { class: "admin-error", role: "alert" }, h("p", { class: "error" }, text),
    retry ? button("다시 시도", { size: "s", onClick: retry }) : null);
}

function select(id, options, value, onChange) {
  const el = h("select", { id }, ...options.map((o) => h("option", { value: o.value, selected: o.value === value || null },
    o.label)));
  el.value = value;
  if (onChange) el.addEventListener("change", () => onChange(el.value));
  return el;
}

function labelled(id, label, control, extra) {
  return h("div", { class: "admin-field" }, h("label", { for: id }, label), control, extra || null);
}

function pager(data, unit, onPage) {
  const page = data.page || 1;
  const pages = data.pages || 1;
  const prev = button("이전", { size: "s", disabled: page <= 1 || null, onClick: () => onPage(page - 1) });
  const next = button("다음", { size: "s", disabled: page >= pages || null, onClick: () => onPage(page + 1) });
  return h("nav", { class: "admin-pager", "aria-label": "쪽 이동" },
    h("span", { class: "muted small" }, pageText(data, unit)),
    h("span", { class: "admin-pager-btns" }, prev, h("span", { class: "admin-pager-at" }, `${page} / ${pages}쪽`), next));
}

// ---- mount --------------------------------------------------------------------------------

export async function mount(el, params, ctx) {
  S = {
    el, ctx, tab: "users", panel: null, methods: null,
    users: { query: { ...DEFAULT_USERS_QUERY }, data: null, error: null, busy: false, timer: null, seq: 0 },
    usage: { days: USAGE_DAYS[0], data: null, error: null },
    audit: { filters: { ...DEFAULT_AUDIT_FILTERS }, page: 1, data: null, error: null, rangeError: null },
    server: { data: null, error: null },
    create: null,
    detail: null,
  };
  const panel = h("div", { class: "admin-tabpanel", role: "tabpanel", id: "adm-panel", "aria-labelledby": "adm-tab-users",
    tabindex: "-1" });
  const tabList = tabs(TABS, S.tab, (key) => switchTab(key), { label: "관리 화면 구역" });
  for (const b of tabList.querySelectorAll('[role="tab"]')) {
    b.id = `adm-tab-${b.getAttribute("data-key")}`;
    b.setAttribute("aria-controls", "adm-panel");
  }
  S.panel = panel;
  fill(el, h("div", { class: "admin-page" }, h("h1", {}, "관리"), tabList, panel));
  fill(panel, loadingNode());
  await loadUsers(ctx);
  if (!alive(ctx)) return;
  if (S.users.error && S.users.error.blocked) {
    // not an admin (403) or the local edition (404): one line instead of the page
    fill(el, h("div", { class: "admin-page" }, h("h1", {}, "관리"),
      h("p", { class: "feature-note muted", role: "status" }, S.users.error.text)));
    return;
  }
  loadMethods(ctx);
}

export function unmount() {
  if (S && S.users.timer) clearTimeout(S.users.timer);
  if (S && S.detail && S.detail.dialog.open) S.detail.dialog.close();
  if (S && S.create && S.create.dialog.open) S.create.dialog.close();
  S = null;
}

// Typed but not sent: the add dialog, the edit form, a new password or the delete box.
export function unsavedNote() {
  if (!S) return null;
  const c = S.create;
  if (c && c.dialog.open && (c.email.value.trim() || c.password.value)) return "보내지 않은 사용자 추가 입력";
  const d = S.detail;
  if (d && d.dialog.open && d.user) {
    if (d.password && d.password.value) return "저장하지 않은 새 비밀번호 입력";
    if (d.form && checkEdit(d.user, editValues(d)).changed.length) return "저장하지 않은 사용자 정보 변경";
  }
  return null;
}

function switchTab(key) {
  S.tab = key;
  S.panel.setAttribute("aria-labelledby", `adm-tab-${key}`);
  renderTab();
  const ctx = S.ctx;
  if (key === "users" && !S.users.data) loadUsers(ctx);
  if (key === "usage" && !S.usage.data) loadUsage(ctx);
  if (key === "audit" && !S.audit.data) loadAudit(ctx);
  if (key === "server" && !S.server.data) loadServer(ctx);
}

function renderTab() {
  if (S.tab === "users") renderUsers();
  else if (S.tab === "usage") renderUsage();
  else if (S.tab === "audit") renderAudit();
  else renderServer();
}

// Which login methods the server offers: the add dialog's password rule follows it. Unknown = optional password.
async function loadMethods(ctx) {
  try {
    const m = await ctx.api("GET", "/auth/methods");
    if (!alive(ctx)) return;
    S.methods = m;
  } catch { /* keep the plain hint; the server still refuses a password-less account when Google is off */ }
}

// Data changed: lists that show it read again when next opened (the users tab reloads now).
function changed(ctx) {
  S.audit.data = null;
  S.server.data = null;
  S.usage.data = null;
  return loadUsers(ctx);
}

// ---- users tab ---------------------------------------------------------------------------------

async function loadUsers(ctx) {
  const u = S.users;
  const seq = ++u.seq;
  u.busy = true;
  if (S.tab === "users" && u.data) renderUsers();
  try {
    const data = await ctx.api("GET", `/api/admin/users${usersQuery(u.query)}`);
    if (!alive(ctx) || seq !== u.seq) return;
    if (data.page > data.pages && data.pages >= 1) {     // the last page emptied (deleted users): step back
      u.query.page = data.pages;
      loadUsers(ctx);
      return;
    }
    u.data = data;
    u.error = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || seq !== u.seq) return;
    u.error = { text: adminErrorText(e), blocked: e.error === "admin_only" || e.status === 404 };
  }
  u.busy = false;
  if (S.tab === "users") renderUsers();
}

function setQuery(patch, { delay = 0 } = {}) {
  const u = S.users;
  Object.assign(u.query, patch);
  if (!("page" in patch)) u.query.page = 1;
  if (u.timer) clearTimeout(u.timer);
  const ctx = S.ctx;
  if (delay) u.timer = setTimeout(() => { if (alive(ctx)) loadUsers(ctx); }, delay);
  else loadUsers(ctx);
}

function usersToolbar() {
  const q = S.users.query;
  const search = h("input", { type: "search", id: "adm-q", value: q.q, autocomplete: "off", spellcheck: "false",
    placeholder: "이메일 또는 이름", maxlength: "200" });
  search.addEventListener("input", () => setQuery({ q: search.value }, { delay: SEARCH_DELAY }));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); setQuery({ q: search.value }); }
  });
  const add = button("사용자 추가", { variant: "primary", onClick: openCreate, id: "adm-add" });
  const clear = hasUserFilters(q)
    ? button("필터 지우기", { size: "s", variant: "ghost", onClick: () => {
      S.users.query = { ...DEFAULT_USERS_QUERY, sort: q.sort };
      renderUsers();
      loadUsers(S.ctx);
    } })
    : null;
  return h("div", { class: "admin-toolbar", role: "search", "aria-label": "사용자 찾기" },
    labelled("adm-q", "검색", search),
    labelled("adm-f-status", "상태", select("adm-f-status", STATUS_OPTIONS, q.status, (v) => setQuery({ status: v }))),
    labelled("adm-f-role", "권한", select("adm-f-role", ROLE_OPTIONS, q.role, (v) => setQuery({ role: v }))),
    labelled("adm-f-login", "로그인 방법", select("adm-f-login", LOGIN_OPTIONS, q.login, (v) => setQuery({ login: v }))),
    labelled("adm-sort", "정렬", select("adm-sort", SORT_OPTIONS, q.sort, (v) => setQuery({ sort: v }))),
    h("div", { class: "admin-toolbar-end" }, clear, add));
}

function renderUsers() {
  const u = S.users;
  const keep = document.activeElement && S.panel.contains(document.activeElement) ? document.activeElement.id : null;
  if (u.error && !u.data) {
    fill(S.panel, errorBox(u.error.text, () => { fill(S.panel, loadingNode()); loadUsers(S.ctx); }));
    return;
  }
  const toolbar = usersToolbar();
  let body;
  if (!u.data) body = loadingNode();
  else if (u.data.users.length === 0) {
    body = hasUserFilters(u.query)
      ? h("p", { class: "muted admin-empty" }, "조건에 맞는 사용자가 없습니다. 검색어나 필터를 바꿔 보세요.")
      : h("p", { class: "muted admin-empty" }, "아직 사용자가 없습니다. 사용자 추가로 첫 사람을 등록하세요.");
  } else body = usersTable(u.data);
  fill(S.panel, toolbar,
    u.error ? h("p", { class: "error", role: "alert" }, u.error.text) : null,
    h("div", { class: "admin-list", "aria-busy": String(u.busy) },
      u.busy && u.data ? h("p", { class: "muted small admin-loading", role: "status" }, "불러오는 중…") : null,
      body),
    u.data && u.data.total > 0 ? pager(u.data, "명", (page) => setQuery({ page })) : null);
  if (keep) {
    const again = document.getElementById(keep);
    if (again) {
      const pos = again.selectionStart;
      again.focus({ preventScroll: true });
      if (again.type === "search" && typeof pos === "number") again.setSelectionRange(pos, pos);
    }
  }
}

function usersTable(data) {
  const head = h("thead", {}, h("tr", {},
    h("th", { scope: "col" }, "사용자"), h("th", { scope: "col" }, "상태"), h("th", { scope: "col" }, "로그인 방법"),
    h("th", { scope: "col" }, "최근 로그인"), h("th", { scope: "col" }, "오늘 사용 / 한도"),
    h("th", { scope: "col" }, h("span", { class: "sr-only" }, "자세히"))));
  const body = h("tbody", {}, ...data.users.map(userRow));
  return h("div", { class: "admin-table-wrap" }, h("table", { class: "admin-table" },
    h("caption", { class: "sr-only" }, `사용자 목록, ${pageText(data, "명")}`), head, body));
}

function userRow(u) {
  const s = spend(u);
  const more = h("button", { type: "button", class: "btn btn-s", "data-user": u.id, "aria-label": `${u.email} 자세히` },
    "자세히");
  more.addEventListener("click", () => openDetail(u.id));
  return h("tr", { class: u.disabled ? "is-off" : null },
    h("td", { "data-label": "사용자" },
      h("span", { class: "admin-name" }, u.display_name || u.email),
      u.display_name ? h("span", { class: "admin-email muted" }, u.email) : null),
    h("td", { "data-label": "상태" }, h("span", { class: "admin-chips" },
      ...statusChips(u).map((c) => h("span", { class: `admin-chip admin-chip-${c.kind}` }, c.label)))),
    h("td", { "data-label": "로그인 방법" }, loginMethods(u).join(" · ")),
    h("td", { "data-label": "최근 로그인" },
      h("time", { datetime: u.last_login_at || null, title: u.last_login_at ? shortTime(u.last_login_at) : null },
        loginText(u.last_login_at))),
    h("td", { "data-label": "오늘 사용 / 한도" },
      h("span", { class: s.over ? "admin-spend is-over" : "admin-spend", "aria-label": s.label }, s.text),
      h("span", { class: "admin-spend-note muted" }, s.note)),
    h("td", { class: "admin-actions-cell" }, more));
}

// ---- dialogs (native <dialog>: focus stays inside, Esc closes) ------------------------------------

function dialogShell(id, title, onClose) {
  const titleId = `${id}-t`;
  const closeBtn = button("닫기", { size: "s", variant: "ghost", onClick: () => dialog.close() });
  const body = h("div", { class: "admin-dialog-body" });
  const dialog = h("dialog", { class: "admin-dialog", id, "aria-labelledby": titleId },
    h("div", { class: "admin-dialog-head" }, h("h2", { id: titleId }, title), closeBtn), body);
  dialog.addEventListener("close", () => { dialog.remove(); if (S) onClose(); });   // close fires late: S may be gone
  return { dialog, body, titleId };
}

function field(id, label, input, hint) {
  const err = h("p", { class: "field-error error", id: `${id}-err`, hidden: true });
  input.setAttribute("aria-describedby", hint ? `${id}-hint ${id}-err` : `${id}-err`);
  return {
    node: h("div", { class: "field" }, h("label", { for: id }, label), input,
      hint ? h("p", { class: "muted small", id: `${id}-hint` }, hint) : null, err),
    err, input,
  };
}

function showErrors(fields, errors) {
  let first = null;
  for (const [key, f] of Object.entries(fields)) {
    const msg = errors[key];
    f.err.hidden = !msg;
    f.err.textContent = msg || "";
    if (msg) f.input.setAttribute("aria-invalid", "true"); else f.input.removeAttribute("aria-invalid");
    if (msg && !first) first = f.input;
  }
  return first;
}

// ---- add a user ---------------------------------------------------------------------------------

function passwordRequired() {
  return !!S.methods && S.methods.google === false;
}

function openCreate() {
  const shell = dialogShell("adm-create", "사용자 추가", () => {
    S.create = null;
    focusNode(document.getElementById("adm-add"));
  });
  const email = h("input", { type: "email", id: "adm-c-email", autocomplete: "off", required: true, spellcheck: "false" });
  const name = h("input", { type: "text", id: "adm-c-name", autocomplete: "off", maxlength: "100" });
  const password = h("input", { type: "password", id: "adm-c-pw", autocomplete: "new-password" });
  const limit = h("input", { type: "text", id: "adm-c-limit", inputmode: "decimal", autocomplete: "off" });
  const isAdmin = h("input", { type: "checkbox", id: "adm-c-admin" });
  const need = passwordRequired();
  const fields = {
    email: field("adm-c-email", "이메일", email, "이 사람이 로그인할 이메일입니다."),
    name: field("adm-c-name", "이름 (선택)", name, null),
    password: field("adm-c-pw", need ? "첫 비밀번호" : "첫 비밀번호 (선택)", password,
      need ? `${PASSWORD_MIN}자 이상. 이 서버는 Google 로그인이 꺼져 있어 비밀번호가 있어야 로그인할 수 있습니다. 정한 비밀번호는 직접 알려 주세요.`
        : `${PASSWORD_MIN}자 이상. 비워 두면 Google 계정으로만 로그인합니다. 정한 비밀번호는 직접 알려 주세요.`),
    limit: field("adm-c-limit", "하루 AI 한도 (선택, 달러)", limit, "비워 두면 서버 기본값을 씁니다."),
  };
  if (need) password.required = true;
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "추가");
  const cancel = button("취소", { onClick: () => shell.dialog.close() });
  const status = h("div", { id: "adm-c-status" });
  const form = h("form", { class: "admin-form", novalidate: true },
    fields.email.node, fields.name.node, fields.password.node, fields.limit.node,
    h("label", { class: "admin-check", for: "adm-c-admin" }, isAdmin,
      h("span", {}, "관리자 (이 관리 화면을 쓸 수 있습니다)")),
    status, h("div", { class: "btn-row admin-dialog-actions" }, submit, cancel));
  form.addEventListener("submit", (e) => { e.preventDefault(); sendCreate(); });
  fill(shell.body, form);
  S.create = { ...shell, form, email, name, password, limit, isAdmin, fields, submit, status, busy: false };
  document.body.append(shell.dialog);
  shell.dialog.showModal();
  email.focus();
}

async function sendCreate() {
  const ctx = S.ctx;
  const c = S.create;
  if (!c || c.busy) return;
  const { errors, body } = checkCreate({ email: c.email.value, name: c.name.value, password: c.password.value,
    limit: c.limit.value, isAdmin: c.isAdmin.checked, passwordRequired: passwordRequired() });
  const first = showErrors(c.fields, errors);
  fill(c.status);
  if (!body) { first.focus(); return; }
  c.busy = true;
  c.submit.disabled = true;
  c.submit.textContent = "추가하는 중…";
  try {
    const out = await ctx.api("POST", "/api/admin/users", body);
    if (!alive(ctx)) return;
    ctx.toast(`사용자를 추가했습니다: ${(out && out.email) || body.email}`, "info");
    c.password.value = "";
    c.dialog.close();
    await changed(ctx);
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || S.create !== c) return;
    const map = { user_exists: "email", bad_email: "email", weak_password: "password", password_required: "password",
      bad_limit: "limit" };
    if (map[e.error]) showErrors(c.fields, { [map[e.error]]: adminErrorText(e) }).focus();
    else fill(c.status, h("p", { class: "error", role: "alert" }, adminErrorText(e)));
  } finally {
    if (alive(ctx) && S.create === c) {
      c.busy = false;
      c.submit.disabled = false;
      c.submit.textContent = "추가";
    }
  }
}

// ---- one user: details, edit, password, sessions, delete ------------------------------------------

function openDetail(id) {
  const shell = dialogShell("adm-detail", "사용자 정보", () => {
    S.detail = null;
    focusNode(S.panel.querySelector(`button[data-user="${CSS.escape(id)}"]`) || document.getElementById("adm-q"));
  });
  shell.dialog.classList.add("admin-drawer");
  // Esc inside an on-screen question closes the question first, not the whole panel
  shell.dialog.addEventListener("cancel", (e) => {
    if (S.detail && S.detail.confirm) {
      e.preventDefault();
      S.detail.confirm = null;
      renderDetail();
    }
  });
  S.detail = { ...shell, id, user: null, error: null, busy: false, confirm: null, notice: null, form: null,
    password: null, typed: "" };
  fill(shell.body, loadingNode());
  document.body.append(shell.dialog);
  shell.dialog.showModal();
  loadDetail(S.ctx);
}

async function loadDetail(ctx) {
  const d = S.detail;
  if (!d) return;
  try {
    const data = await ctx.api("GET", `/api/admin/users/${encodeURIComponent(d.id)}`);
    if (!alive(ctx) || S.detail !== d) return;
    d.user = data.user;
    d.defaultUsd = data.default_daily_usd;
    d.error = null;
    d.form = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || S.detail !== d) return;
    d.error = adminErrorText(e);
  }
  renderDetail();
}

function editValues(d) {
  const f = d.form;
  return { email: f.email.value, name: f.name.value, limit: f.limit.value, isAdmin: f.isAdmin.checked,
    enabled: f.enabled.checked };
}

function buildForm(u) {
  const email = h("input", { type: "email", id: "adm-e-email", value: u.email, autocomplete: "off", spellcheck: "false" });
  const name = h("input", { type: "text", id: "adm-e-name", value: u.display_name || "", autocomplete: "off",
    maxlength: "100" });
  const limit = h("input", { type: "text", id: "adm-e-limit", inputmode: "decimal", autocomplete: "off",
    value: typeof u.daily_usd_limit === "number" ? u.daily_usd_limit.toFixed(2) : "",
    placeholder: `기본 ${usd(S.detail.defaultUsd)}` });
  const isAdmin = h("input", { type: "checkbox", id: "adm-e-admin", checked: u.is_admin || null });
  const enabled = h("input", { type: "checkbox", id: "adm-e-enabled", checked: !u.disabled || null });
  if (u.is_self) { isAdmin.disabled = true; enabled.disabled = true; }
  return {
    email, name, limit, isAdmin, enabled,
    fields: {
      email: field("adm-e-email", "이메일", email, u.has_google ? "바꾸면 Google 계정 연결이 풀립니다." : null),
      name: field("adm-e-name", "이름", name, null),
      limit: field("adm-e-limit", "하루 AI 한도 (달러)", limit, `비워 두면 서버 기본값(${usd(S.detail.defaultUsd)})을 씁니다.`),
    },
  };
}

function facts(u) {
  const rows = [
    ["추가한 날", dateText(u.created_at)],
    ["최근 로그인", u.last_login_at ? `${loginText(u.last_login_at)} (${shortTime(u.last_login_at)})` : "아직 없음"],
    ["로그인 방법", loginMethods(u).join(" · ")],
    ["문서", `${u.docs}개`],
    ["파일", `${u.objects}개`],
    ["휴지통", `${u.trash}개`],
    ["저장 용량", bytesText(u.storage_bytes)],
    ["오늘 AI 사용", `${usd(u.today_usd)} / ${usd(u.effective_daily_usd)}`],
    ["이번 달 AI 사용", usd(u.month_usd)],
  ];
  return h("dl", { class: "admin-facts" }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function renderDetail() {
  const d = S.detail;
  if (!d) return;
  const keep = document.activeElement && d.dialog.contains(document.activeElement) ? document.activeElement.id : null;
  if (d.error && !d.user) {
    fill(d.body, errorBox(d.error, () => { fill(d.body, loadingNode()); loadDetail(S.ctx); }));
    return;
  }
  const u = d.user;
  if (!u) { fill(d.body, loadingNode()); return; }
  if (!d.form) d.form = buildForm(u);
  const f = d.form;
  const title = document.getElementById(d.titleId);
  if (title) title.textContent = u.display_name ? `${u.display_name}` : u.email;
  for (const el of [f.email, f.name, f.limit]) el.disabled = d.busy;
  if (!u.is_self) { f.isAdmin.disabled = d.busy; f.enabled.disabled = d.busy; }

  const summary = h("div", { class: "admin-detail-summary" },
    h("p", { class: "admin-email" }, u.email),
    h("span", { class: "admin-chips" },
      ...statusChips(u).map((c) => h("span", { class: `admin-chip admin-chip-${c.kind}` }, c.label))));

  const save = h("button", { type: "submit", class: "btn btn-primary btn-s", disabled: d.busy || null }, "변경 저장");
  const form = h("form", { class: "admin-form", novalidate: true, "aria-labelledby": "adm-e-h" },
    h("h3", { id: "adm-e-h" }, "계정 정보"),
    f.fields.email.node, f.fields.name.node, f.fields.limit.node,
    h("label", { class: "admin-check", for: "adm-e-admin" }, f.isAdmin, h("span", {}, "관리자")),
    h("label", { class: "admin-check", for: "adm-e-enabled" }, f.enabled, h("span", {}, "로그인할 수 있음 (끄면 비활성)")),
    u.is_self ? h("p", { class: "muted small" }, "내 계정은 비활성으로 바꾸거나 관리자에서 뺄 수 없습니다.") : null,
    d.confirm && d.confirm.kind === "save" ? confirmBox(d.confirm) : null,
    h("div", { class: "btn-row" }, save));
  form.addEventListener("submit", (e) => { e.preventDefault(); askSave(); });

  fill(d.body,
    summary,
    d.notice ? h("p", { class: d.notice.warn ? "error" : "admin-ok", role: "status", id: "adm-d-note" }, d.notice.text) : null,
    d.error ? h("p", { class: "error", role: "alert", id: "adm-d-err" }, d.error) : null,
    h("section", { class: "admin-group", "aria-label": "요약" }, facts(u)),
    h("section", { class: "admin-group" }, form),
    passwordGroup(u),
    sessionGroup(u),
    dangerZone(u));
  if (keep) {
    const again = document.getElementById(keep);
    if (again && !again.disabled) again.focus({ preventScroll: true });
  }
}

function confirmBox(c) {
  const yes = button(c.yes, { size: "s", variant: c.danger ? "outline" : "primary", disabled: S.detail.busy || null,
    onClick: c.run, id: "adm-confirm-yes" });
  if (c.danger) yes.classList.add("btn-danger");
  const no = button("취소", { size: "s", id: "adm-confirm-no", disabled: S.detail.busy || null, onClick: () => {
    S.detail.confirm = null;
    renderDetail();
  } });
  return h("div", { class: "inline-confirm", role: "alert" },
    ...c.lines.map((line) => h("p", { class: "confirm-text" }, line)),
    h("div", { class: "btn-row" }, yes, no));
}

function ask(kind, lines, yes, run, danger) {
  S.detail.confirm = { kind, lines, yes, run, danger };
  renderDetail();
  focusNode(document.getElementById("adm-confirm-no"));
}

function askSave() {
  const d = S.detail;
  const result = checkEdit(d.user, editValues(d));
  const first = showErrors(d.form.fields, result.errors);
  if (first) { first.focus(); return; }
  if (!result.body) {
    d.notice = { text: "바뀐 내용이 없습니다.", warn: false };
    renderDetail();
    return;
  }
  if (result.warnings.length) {
    ask("save", result.warnings, "저장", () => update(result.body, "정보를 저장했습니다"), result.body.disabled === true);
    return;
  }
  update(result.body, "정보를 저장했습니다");
}

const FIELD_ERRORS = { user_exists: "email", bad_email: "email", bad_limit: "limit" };

async function update(body, done) {
  const ctx = S.ctx;
  const d = S.detail;
  if (!d || d.busy) return;
  d.busy = true;
  d.error = null;
  d.notice = null;
  renderDetail();
  try {
    const out = await ctx.api("POST", `/api/admin/users/${encodeURIComponent(d.id)}/update`, body);
    if (!alive(ctx) || S.detail !== d) return;
    if (d.password) d.password.value = "";
    if (d.user.is_self && out.sessions_ended) {
      ctx.toast("내 계정 정보가 바뀌어 다시 로그인해야 합니다", "info");
      location.assign("/auth/login");
      return;
    }
    d.busy = false;
    d.confirm = null;
    d.notice = { text: `${done}.${out.sessions_ended ? " 이 사람은 모든 기기에서 로그아웃됐습니다." : ""}`, warn: false };
    ctx.toast(`${d.user.email}: ${done}`, "info");
    await loadDetail(ctx);
    if (!alive(ctx)) return;
    focusNode(document.getElementById("adm-d-note"));
    changed(ctx);
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || S.detail !== d) return;
    d.busy = false;
    d.confirm = null;
    const key = FIELD_ERRORS[e.error];
    if (key && d.form.fields[key]) {
      renderDetail();
      showErrors(d.form.fields, { [key]: adminErrorText(e) }).focus();
      return;
    }
    d.error = adminErrorText(e);
    renderDetail();
    focusNode(document.getElementById("adm-d-err"));
  }
}

function passwordGroup(u) {
  const d = S.detail;
  if (!d.password) {
    d.password = h("input", { type: "password", id: "adm-new-pw", autocomplete: "new-password",
      minlength: String(PASSWORD_MIN), "aria-describedby": "adm-new-pw-hint" });
  }
  const pw = d.password;
  pw.disabled = d.busy;
  const setPw = button("비밀번호 정하기", { size: "s", disabled: d.busy || null, onClick: () => {
    const bad = checkPassword(pw.value);
    if (bad) {
      d.error = bad;
      pw.setAttribute("aria-invalid", "true");
      renderDetail();
      pw.focus();
      return;
    }
    pw.removeAttribute("aria-invalid");
    update({ password: pw.value }, "비밀번호를 정했습니다");
  } });
  const clear = u.has_password
    ? button("비밀번호 지우기", { size: "s", disabled: d.busy || null, onClick: () => ask("clear",
      [`${u.email}의 비밀번호를 지울까요? 비밀번호로는 로그인할 수 없게 되고, 모든 기기에서 로그아웃됩니다.`],
      "비밀번호 지우기", () => update({ clear_password: true }, "비밀번호를 지웠습니다"), true) })
    : null;
  return h("section", { class: "admin-group", "aria-labelledby": "adm-pw-h" },
    h("h3", { id: "adm-pw-h" }, "비밀번호"),
    h("label", { for: "adm-new-pw" }, u.has_password ? "새 비밀번호" : "비밀번호 정하기"),
    h("div", { class: "admin-inline" }, pw, setPw),
    h("p", { class: "muted small", id: "adm-new-pw-hint" },
      `${PASSWORD_MIN}자 이상. 정하면 이 사람은 모든 기기에서 로그아웃됩니다. 정한 비밀번호는 직접 알려 주세요(화면에 다시 나오지 않습니다).`),
    clear ? h("div", { class: "btn-row" }, clear) : null,
    d.confirm && d.confirm.kind === "clear" ? confirmBox(d.confirm) : null);
}

function sessionGroup(u) {
  const d = S.detail;
  const out = button("모든 기기에서 로그아웃", { size: "s", disabled: d.busy || null, onClick: () => ask("logout",
    [`${u.email}을(를) 모든 기기에서 로그아웃시킬까요? 계정은 그대로이고 다시 로그인하면 됩니다.`],
    "모두 로그아웃", () => act("logout-all", "모든 기기에서 로그아웃시켰습니다"), false) });
  return h("section", { class: "admin-group", "aria-labelledby": "adm-s-h" },
    h("h3", { id: "adm-s-h" }, "로그인 세션"),
    h("p", { class: "muted small" }, "기기를 잃어버렸을 때처럼, 이 사람이 로그인한 모든 곳에서 로그아웃시킵니다."),
    h("div", { class: "btn-row" }, out),
    d.confirm && d.confirm.kind === "logout" ? confirmBox(d.confirm) : null);
}

async function act(action, done) {
  const ctx = S.ctx;
  const d = S.detail;
  if (!d || d.busy) return;
  d.busy = true;
  d.error = null;
  renderDetail();
  try {
    await ctx.api("POST", `/api/admin/users/${encodeURIComponent(d.id)}/${action}`, {});
    if (!alive(ctx) || S.detail !== d) return;
    if (d.user.is_self && action === "logout-all") { location.assign("/auth/login"); return; }
    d.busy = false;
    d.confirm = null;
    d.notice = { text: `${done}.`, warn: false };
    ctx.toast(`${d.user.email}: ${done}`, "info");
    await loadDetail(ctx);
    if (!alive(ctx)) return;
    focusNode(document.getElementById("adm-d-note"));
    changed(ctx);
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || S.detail !== d) return;
    d.busy = false;
    d.confirm = null;
    d.error = adminErrorText(e);
    renderDetail();
    focusNode(document.getElementById("adm-d-err"));
  }
}

function dangerZone(u) {
  const d = S.detail;
  if (u.is_self) {
    return h("section", { class: "admin-group admin-danger", "aria-labelledby": "adm-del-h" },
      h("h3", { id: "adm-del-h" }, "사용자 삭제"),
      h("p", { class: "muted small" }, "내 계정은 여기서 지울 수 없습니다. 다른 관리자에게 부탁하세요."));
  }
  const typed = h("input", { type: "text", id: "adm-del-typed", value: d.typed, autocomplete: "off", spellcheck: "false",
    autocapitalize: "off", "aria-describedby": "adm-del-what" });
  typed.disabled = d.busy;
  const go = button("사용자 영구 삭제", { size: "s", variant: "outline", id: "adm-del-go",
    disabled: d.busy || !deleteMatches(d.typed, u.email) || null, onClick: () => removeUser(u) });
  go.classList.add("btn-danger");
  typed.addEventListener("input", () => {
    d.typed = typed.value;
    go.disabled = d.busy || !deleteMatches(d.typed, u.email);
  });
  typed.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !go.disabled) { e.preventDefault(); removeUser(u); }
  });
  return h("section", { class: "admin-group admin-danger", "aria-labelledby": "adm-del-h" },
    h("h3", { id: "adm-del-h" }, "사용자 삭제"),
    h("p", { class: "small", id: "adm-del-what" },
      `계정과 함께 문서 ${u.docs}개, 파일 ${u.objects}개, 휴지통 ${u.trash}개(${bytesText(u.storage_bytes)})가 모두 지워지고 `
      + "되돌릴 수 없습니다. AI 사용 기록은 이름 없이 남습니다. 잠시 막기만 하려면 위에서 비활성으로 바꾸세요."),
    h("label", { for: "adm-del-typed" }, `확인을 위해 이메일 ${u.email} 을(를) 그대로 입력하세요`),
    h("div", { class: "admin-inline" }, typed, go));
}

async function removeUser(u) {
  const ctx = S.ctx;
  const d = S.detail;
  if (!d || d.busy || !deleteMatches(d.typed, u.email)) return;
  d.busy = true;
  d.error = null;
  renderDetail();
  try {
    const out = await ctx.api("POST", `/api/admin/users/${encodeURIComponent(d.id)}/delete`, { confirm_email: d.typed });
    if (!alive(ctx) || S.detail !== d) return;
    const note = deleteDoneText(out);
    ctx.toast(note.text, note.warn ? "error" : "info");
    d.dialog.close();
    await changed(ctx);
  } catch (e) {
    if (ignorable(e) || !alive(ctx) || S.detail !== d) return;
    d.busy = false;
    d.error = adminErrorText(e);
    renderDetail();
    focusNode(document.getElementById("adm-d-err"));
  }
}

// ---- usage tab -----------------------------------------------------------------------------------

async function loadUsage(ctx) {
  const days = S.usage.days;
  try {
    const data = await ctx.api("GET", `/api/admin/usage?days=${days}`);
    if (!alive(ctx) || S.usage.days !== days) return;
    S.usage.data = data;
    S.usage.error = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    S.usage.error = adminErrorText(e);
  }
  if (S.tab === "usage") renderUsage();
}

function renderUsage() {
  const u = S.usage;
  const picks = h("div", { class: "admin-seg", role: "group", "aria-label": "기간" }, ...USAGE_DAYS.map((n) => {
    const b = h("button", { type: "button", class: "ui-chip", "aria-pressed": String(n === u.days) }, `최근 ${n}일`);
    b.addEventListener("click", () => {
      if (u.days === n) return;
      u.days = n;
      u.data = null;
      renderUsage();
      loadUsage(S.ctx);
    });
    return b;
  }));
  let body;
  if (u.error) body = errorBox(u.error, () => { u.error = null; renderUsage(); loadUsage(S.ctx); });
  else if (!u.data) body = loadingNode();
  else if (!u.data.rows || u.data.rows.length === 0) {
    body = h("p", { class: "muted admin-empty" }, `최근 ${u.days}일 동안 AI를 쓴 기록이 없습니다.`);
  } else body = usageBody(u.data);
  fill(S.panel, h("p", { class: "muted small" }, "한국 시간 날짜별 합계입니다. 지운 사용자의 사용액은 ‘삭제된 사용자’로 남습니다."),
    picks, body);
}

function usageBody(data) {
  const totals = usageTotals(data.rows);
  const summary = h("ul", { class: "admin-totals" }, ...totals.map((t) => h("li", {},
    h("span", { class: "admin-name" }, t.email),
    h("span", {}, `${usd(t.cost)} · 호출 ${t.calls}번${t.failed ? `(실패 ${t.failed})` : ""}`))));
  const rows = data.rows.map((r) => h("tr", {},
    h("td", { "data-label": "날짜" }, r.day), h("td", { "data-label": "사용자" }, usageEmail(r.email)),
    h("td", { "data-label": "호출", class: "num" }, r.failed ? `${r.calls} (실패 ${r.failed})` : String(r.calls)),
    h("td", { "data-label": "토큰(입력/출력)", class: "num" },
      `${r.input_tokens.toLocaleString("ko-KR")} / ${r.output_tokens.toLocaleString("ko-KR")}`),
    h("td", { "data-label": "비용", class: "num" }, usd(r.cost_usd))));
  return h("div", {},
    h("p", { class: "admin-total" }, `합계 ${usd(data.total_usd)}`),
    summary,
    h("div", { class: "admin-table-wrap" }, h("table", { class: "admin-table admin-usage" },
      h("caption", { class: "sr-only" }, `최근 ${data.days}일 날짜·사용자별 AI 사용량`),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "날짜"), h("th", { scope: "col" }, "사용자"),
        h("th", { scope: "col", class: "num" }, "호출"), h("th", { scope: "col", class: "num" }, "토큰(입력/출력)"),
        h("th", { scope: "col", class: "num" }, "비용"))),
      h("tbody", {}, ...rows))));
}

// ---- audit tab -----------------------------------------------------------------------------------

async function loadAudit(ctx) {
  const a = S.audit;
  const query = auditQuery(a.filters, a.page);
  try {
    const data = await ctx.api("GET", `/api/admin/audit${query}`);
    if (!alive(ctx) || auditQuery(S.audit.filters, S.audit.page) !== query) return;
    a.data = data;
    a.error = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    a.error = adminErrorText(e);
  }
  if (S.tab === "audit") renderAudit();
}

function auditFilters() {
  const a = S.audit;
  const f = a.filters;
  const actor = h("input", { type: "search", id: "adm-a-actor", value: f.actor, autocomplete: "off", maxlength: "200",
    placeholder: "이메일 일부" });
  const target = h("input", { type: "search", id: "adm-a-target", value: f.target, autocomplete: "off", maxlength: "200",
    placeholder: "이메일 일부" });
  const action = select("adm-a-action", actionOptions(a.data && a.data.actions), f.action, null);
  const from = h("input", { type: "date", id: "adm-a-from", value: f.from });
  const to = h("input", { type: "date", id: "adm-a-to", value: f.to });
  const err = h("p", { class: "error field-error", id: "adm-a-err", role: "alert", hidden: !a.rangeError || null },
    a.rangeError || "");
  const submit = h("button", { type: "submit", class: "btn btn-s btn-primary" }, "찾기");
  const reset = hasAuditFilters(f) ? button("초기화", { size: "s", variant: "ghost", onClick: () => {
    a.filters = { ...DEFAULT_AUDIT_FILTERS };
    a.page = 1;
    a.rangeError = null;
    a.data = null;
    renderAudit();
    loadAudit(S.ctx);
  } }) : null;
  const form = h("form", { class: "admin-toolbar", role: "search", "aria-label": "감사 기록 찾기", novalidate: true },
    labelled("adm-a-actor", "누가", actor), labelled("adm-a-action", "무엇을", action),
    labelled("adm-a-target", "누구에게", target), labelled("adm-a-from", "시작일", from), labelled("adm-a-to", "끝 날짜", to),
    h("div", { class: "admin-toolbar-end" }, submit, reset), err);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const next = { actor: actor.value, action: action.value, target: target.value, from: from.value, to: to.value };
    a.rangeError = checkRange(next.from, next.to);
    if (a.rangeError) { renderAudit(); focusNode(document.getElementById("adm-a-from")); return; }
    a.filters = next;
    a.page = 1;
    a.data = null;
    renderAudit();
    loadAudit(S.ctx);
  });
  return form;
}

function renderAudit() {
  const a = S.audit;
  let body;
  if (a.error) body = errorBox(a.error, () => { a.error = null; renderAudit(); loadAudit(S.ctx); });
  else if (!a.data) body = loadingNode();
  else if (a.data.entries.length === 0) {
    body = h("p", { class: "muted admin-empty" },
      hasAuditFilters(a.filters) ? "조건에 맞는 기록이 없습니다." : "아직 관리 기록이 없습니다.");
  } else {
    body = h("ol", { class: "admin-audit" }, ...a.data.entries.map((entry) => {
      const line = auditLine(entry);
      return h("li", {},
        h("time", { class: "muted", datetime: entry.at }, shortTime(entry.at)),
        h("span", {}, h("strong", {}, line.who), ` → ${line.whom}: ${line.what}`));
    }));
  }
  fill(S.panel,
    h("p", { class: "muted small" }, "관리자와 운영자가 바꾼 것만 남습니다(보기만 한 것은 남지 않습니다). 최근 것이 위에 옵니다."),
    auditFilters(), body,
    a.data && a.data.total > 0 ? pager(a.data, "건", (page) => {
      a.page = page;
      loadAudit(S.ctx);
    }) : null);
}

// ---- server tab ----------------------------------------------------------------------------------

async function loadServer(ctx) {
  try {
    const data = await ctx.api("GET", "/api/admin/overview");
    if (!alive(ctx)) return;
    S.server.data = data;
    S.server.error = null;
  } catch (e) {
    if (ignorable(e) || !alive(ctx)) return;
    S.server.error = adminErrorText(e);
  }
  if (S.tab === "server") renderServer();
}

function stat(label, value, note, wide = false) {
  return h("div", { class: wide ? "admin-stat admin-stat-wide" : "admin-stat" }, h("dt", {}, label), h("dd", {}, value),
    note ? h("dd", { class: "muted small" }, note) : null);
}

function renderServer() {
  const s = S.server;
  if (s.error) { fill(S.panel, errorBox(s.error, () => { s.error = null; renderServer(); loadServer(S.ctx); })); return; }
  if (!s.data) { fill(S.panel, loadingNode()); return; }
  const { counts, spend: money, login, features_off: off } = s.data;
  const people = h("section", { class: "card admin-card", "aria-labelledby": "adm-sv-people" },
    h("h2", { id: "adm-sv-people" }, "사용자"),
    h("dl", { class: "admin-stats" },
      stat("전체", `${counts.users}명`),
      stat("활성 관리자", `${counts.admins}명`),
      stat("비활성", `${counts.disabled}명`),
      stat("로그인한 적 없음", `${counts.never_logged_in}명`),
      stat("비밀번호 있음", `${counts.with_password}명`),
      stat("Google 연결", `${counts.with_google}명`)));
  const costs = h("section", { class: "card admin-card", "aria-labelledby": "adm-sv-cost" },
    h("h2", { id: "adm-sv-cost" }, "AI 사용액"),
    h("dl", { class: "admin-stats" },
      stat("오늘", usd(money.today_usd), "한국 시간 기준"),
      stat("이번 달", usd(money.month_usd), money.month_deleted_usd ? `삭제된 사용자 ${usd(money.month_deleted_usd)} 포함` : null),
      stat("기본 하루 한도", usd(s.data.default_daily_usd), "서버 설정(YTC_DAILY_USD)으로 정합니다. 사람마다 다른 한도는 사용자 정보에서 바꿉니다.", true)));
  const methods = h("section", { class: "card admin-card", "aria-labelledby": "adm-sv-login" },
    h("h2", { id: "adm-sv-login" }, "로그인 방법"),
    h("ul", { class: "admin-lines" }, ...loginLines(login).map((l) => h("li", { class: l.warn ? "is-warn" : null },
      h("span", { class: `admin-chip ${l.on ? "admin-chip-on" : "admin-chip-off-quiet"}` }, l.on ? "켜짐" : "꺼짐"),
      h("span", {}, h("strong", {}, l.label), ` ${l.on ? l.note : ""}`)))),
    h("p", { class: "muted small" }, "로그인 방법은 서버 설정에서 바꿉니다. 이 화면에서는 볼 수만 있습니다."));
  const features = h("section", { class: "card admin-card", "aria-labelledby": "adm-sv-off" },
    h("h2", { id: "adm-sv-off" }, "서버판에서 꺼진 기능"),
    off && off.length
      ? h("ul", { class: "admin-lines" }, ...off.map((f) => h("li", {}, h("span", {}, f.text || f.name))))
      : h("p", { class: "muted" }, "꺼진 기능이 없습니다."));
  fill(S.panel, h("div", { class: "admin-server" }, people, costs, methods, features));
}
