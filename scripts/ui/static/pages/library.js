// 공용 라이브러리 화면
//   연결 안 됨: 저장소 주소·표시 이름 → 연결.  연결됨: 받기, 탭(기법 / 기획안 틀 / 공유하기).
//   지인이 쓴 글(이름·메모·본문)은 신뢰하지 않는 데이터라 항상 텍스트 노드나 renderMarkdown으로만 그린다.
//   올리기는 "올리기" → 인라인 확인 → "올리기 확인"을 눌러야만 서버로 간다. 자동으로 올리지 않는다.
import { h, fill } from "/static/dom.js";
import { hookName, narrativeName } from "/static/labels.js";
import { renderMarkdown } from "/static/md.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.library.handled";
const MOTIF_IDS_KEY = "ytc.motif.library_ids";
const MAX_PICK = 10;
const MAX_NOTE = 200;
const MAX_TITLE = 100;
const MAX_SUMMARY = 120;
const MAX_AUTHOR = 40;
const ACTIVE = new Set(["running", "awaiting"]);
const TABS = [["tech", "기법"], ["tpl", "기획안 틀"], ["share", "공유하기"]];
const KIND_OF_TAB = { tech: "technique", tpl: "template" };
const PULL_TEXT = {
  updated: "새 항목을 받았습니다",
  up_to_date: "이미 최신입니다",
  empty: "저장소가 아직 비어 있습니다. 첫 항목을 공유해 보세요",
};

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const ignorable = (e) => !e || e.error === "aborted";
const noRef = () => "#/library";
const md = (text, opts = {}) => h("div", { class: "md" }, renderMarkdown(String(text || ""), { refHref: noRef, ...opts }));
const draftHref = (id) => `#/draft/new/${encodeURIComponent(`library:${id}`)}`;

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A summary job that finished (or failed) and was shown/used in this browser is not restored again.
function markHandled(jobId) {
  if (!jobId) return;
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString("ko-KR");
}

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

// ---- page state ---------------------------------------------------------------

let S = null;

function freshShare() {
  return {
    mode: "technique", mine: null, mineLoading: false, mineError: null, choice: null,
    phase: "pick", preview: null, previewBusy: false, title: "", sections: [], note: "", worked: false,
    askUpload: false, uploading: false, error: null, dup: null, result: null, lastBlock: "",
    job: null, timer: null, pollErrors: 0, mineSeq: 0, askSummarize: false, summarizing: false, summaryError: null,
  };
}

function freshState(el, ctx) {
  return {
    el, ctx, status: null, items: [], skipped: 0, tab: "tech",
    filter: { q: "", category: "", author: "", worked: false }, picked: new Set(), details: new Map(),
    pulling: false, pullNote: null, connecting: false, connectError: null, remote: "", author: "",
    sh: freshShare(), nodes: {},
  };
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function stopPolling() {
  if (S && S.sh.timer) { clearTimeout(S.sh.timer); S.sh.timer = null; }
}

function focusHeading() {
  const t = S && S.nodes.title;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  fill(mine.el, h("h1", {}, "공용 라이브러리"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    mine.status = await mine.ctx.api("GET", "/api/library/status");
    if (!alive(mine)) return;
    if (mine.status.connected) {
      await fetchItems(mine);
      if (!alive(mine)) return;
      await restoreSummaryJob(mine);
      if (!alive(mine)) return;
    }
    render();
    if (isActive(mine.sh.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e);
  }
}

function loadError(e) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) load(); });
  fill(S.el, h("h1", {}, "공용 라이브러리"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

async function fetchItems(mine) {
  const data = await mine.ctx.api("GET", "/api/library/items");
  if (!alive(mine)) return;
  mine.items = data.items || [];
  mine.skipped = data.skipped || 0;
  const ids = new Set(mine.items.map((it) => it.id));
  for (const id of [...mine.picked]) if (!ids.has(id)) mine.picked.delete(id);
}

// A section-summary job (or its saved preview) survives a reload: bring the editing step back.
async function restoreSummaryJob(mine) {
  let job = null;
  try {
    job = (await mine.ctx.api("GET", "/api/jobs/current?kind=library_summary")).job;
  } catch (e) {
    if (ignorable(e)) return;
  }
  if (!alive(mine) || !job || isHandled(job.id)) return;
  const item = job.items && job.items[0];
  if (!item) return;
  try {
    const pv = await mine.ctx.api("GET", `/api/library/preview/${encodeURIComponent(item.id)}`);
    if (!alive(mine)) return;
    adoptPreview(mine, pv);
  } catch (e) {
    if (ignorable(e)) return;
    markHandled(job.id);                 // the preview is gone (uploaded, discarded or expired)
    return;
  }
  mine.sh.job = job;
  mine.tab = "share";
  if (isActive(job)) {
    mine.sh.summarizing = true;
  } else if (job.status === "failed" || job.status === "stopped") {
    mine.sh.summaryError = item.message || job.message || "섹션 요약을 받지 못했습니다";
    markHandled(job.id);                 // shown once
  }
}

// ---- connect ------------------------------------------------------------------

function renderConnect() {
  S.nodes = {};
  S.nodes.title = h("h1", { id: "lib-title" }, "공용 라이브러리");
  const remote = h("input", {
    id: "lib-remote", type: "text", autocomplete: "off", spellcheck: "false",
    placeholder: "GitHub 저장소 주소 (git@… 또는 깃허브 주소)", "aria-describedby": "lib-remote-hint",
  });
  remote.value = S.remote;
  remote.addEventListener("input", () => { S.remote = remote.value; });
  const author = h("input", { id: "lib-author", type: "text", maxlength: String(MAX_AUTHOR), placeholder: "예: 민지", autocomplete: "off" });
  author.value = S.author;
  author.addEventListener("input", () => { S.author = author.value; });
  const btn = h("button", { type: "submit", class: "btn btn-primary" }, "연결");
  S.nodes.connectBtn = btn;
  S.nodes.connectMsg = h("div", { class: "lib-msg" });
  const form = h("form", { class: "lib-connect add-form", novalidate: "" },
    h("div", { class: "field" }, h("label", { for: "lib-remote" }, "저장소 주소"), remote,
      h("p", { class: "muted hint", id: "lib-remote-hint" },
        "지인들과 함께 쓰는 비공개 GitHub 저장소 주소입니다. 주소에 비밀번호·토큰을 넣지 마세요. 로그인은 git(SSH, gh auth login)이 처리합니다.")),
    h("div", { class: "field" }, h("label", { for: "lib-author" }, "표시 이름"), author,
      h("p", { class: "muted hint" }, "내가 올린 항목에 작성자로 보이는 이름입니다.")),
    h("div", { class: "btn-row" }, btn), S.nodes.connectMsg);
  form.addEventListener("submit", (ev) => { ev.preventDefault(); connect(); });
  fill(S.el, S.nodes.title,
    h("p", { class: "lead" }, "지인과 훅 기법·기획안 틀을 나누는 공용 저장소를 연결하세요. 연결 뒤에는 받기·보기·공유하기를 이 화면에서 합니다."),
    h("section", { class: "card", "aria-label": "저장소 연결" }, form));
  renderConnectMsg();
}

function renderConnectMsg() {
  const box = S.nodes.connectMsg;
  if (!box) return;
  if (S.nodes.connectBtn) S.nodes.connectBtn.disabled = S.connecting;
  const kids = [];
  if (S.connecting) {
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "저장소 받는 중" }),
      h("span", {}, "저장소를 받는 중… (최대 1분)")));
  }
  if (S.connectError) kids.push(h("p", { class: "error", role: "alert" }, S.connectError));
  fill(box, ...kids);
}

async function connect() {
  const mine = S;
  if (mine.connecting) return;
  const remote = mine.remote.trim();
  const author = mine.author.trim();
  if (!remote) { mine.connectError = "저장소 주소를 적어 주세요"; renderConnectMsg(); return; }
  if (!author) { mine.connectError = "표시 이름을 적어 주세요"; renderConnectMsg(); return; }
  mine.connecting = true;
  mine.connectError = null;
  renderConnectMsg();
  try {
    await mine.ctx.api("POST", "/api/library/connect", { remote, author });
    if (!alive(mine)) return;
    mine.ctx.toast("저장소를 연결했습니다", "info");
    mine.connecting = false;
    await load();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.connecting = false;
    mine.connectError = e.message || "연결하지 못했습니다";
    renderConnectMsg();
  }
}

// ---- connected page -----------------------------------------------------------

function render() {
  if (!S.status || !S.status.connected) { renderConnect(); return; }
  S.nodes = {};
  const st = S.status;
  S.nodes.title = h("h1", { id: "lib-title" }, "공용 라이브러리");
  S.nodes.pullBtn = h("button", { type: "button", class: "btn btn-primary" }, "받기");
  S.nodes.pullBtn.addEventListener("click", pull);
  S.nodes.pullNote = h("div", { class: "lib-pull-note" });
  S.nodes.tabs = h("div", { class: "lib-tabs", role: "tablist", "aria-label": "라이브러리 보기" });
  S.nodes.panel = h("div", { class: "lib-panel", role: "tabpanel", id: "lib-panel" });
  const info = h("p", { class: "muted small" },
    `저장소 ${st.remote || ""} · 표시 이름 ${st.author || ""} · 마지막 받은 시각 ${st.last_pull ? fmtTime(st.last_pull) : "아직 받지 않음"}`);
  S.nodes.info = info;
  const kids = [
    S.nodes.title,
    h("div", { class: "btn-row lib-top" }, S.nodes.pullBtn, S.nodes.pullNote),
    info,
  ];
  if (S.skipped > 0) kids.push(h("p", { class: "muted small", role: "status" }, `읽지 못한 파일 ${S.skipped}개는 목록에서 뺐습니다`));
  kids.push(S.nodes.tabs, S.nodes.panel);
  fill(S.el, ...kids);
  renderPullNote();
  renderTabs();
  renderPanel();
}

function renderInfo() {
  const st = S.status;
  if (S.nodes.info && st) {
    S.nodes.info.textContent =
      `저장소 ${st.remote || ""} · 표시 이름 ${st.author || ""} · 마지막 받은 시각 ${st.last_pull ? fmtTime(st.last_pull) : "아직 받지 않음"}`;
  }
}

function renderPullNote() {
  const box = S.nodes.pullNote;
  if (!box) return;
  S.nodes.pullBtn.disabled = S.pulling;
  S.nodes.pullBtn.textContent = S.pulling ? "받는 중…" : "받기";
  const n = S.pullNote;
  const kids = [];
  if (S.pulling) kids.push(h("span", { class: "muted small", role: "status" }, "저장소에서 받는 중… (최대 1분)"));
  else if (n) {
    kids.push(h("span", { class: n.error ? "error small" : "muted small", role: n.error ? "alert" : "status" }, n.text));
    if (n.warning) kids.push(h("span", { class: "warn-text small", role: "status" }, n.warning));
  }
  fill(box, ...kids);
}

async function pull() {
  const mine = S;
  if (mine.pulling) return;
  mine.pulling = true;
  mine.pullNote = null;
  renderPullNote();
  try {
    const res = await mine.ctx.api("POST", "/api/library/pull", {});
    if (!alive(mine)) return;
    mine.pullNote = { text: PULL_TEXT[res.status] || "받았습니다", warning: res.warning || null };
    mine.status = { ...mine.status, last_pull: res.last_pull };
    await fetchItems(mine);
    if (!alive(mine)) return;
    renderInfo();
    renderTabs();
    if (mine.tab !== "share") renderPanel();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.pullNote = { text: e.message || "받지 못했습니다", error: true };
  } finally {
    if (alive(mine)) { mine.pulling = false; renderPullNote(); }
  }
}

// ---- tabs ---------------------------------------------------------------------

function countOf(kind) {
  return S.items.filter((it) => it.kind === kind).length;
}

function renderTabs() {
  const box = S.nodes.tabs;
  if (!box) return;
  const btns = TABS.map(([key, label]) => {
    const n = KIND_OF_TAB[key] ? ` (${countOf(KIND_OF_TAB[key])})` : "";
    const selected = S.tab === key;
    const b = h("button", {
      type: "button", class: "btn lib-tab", role: "tab", id: `lib-tab-${key}`, "aria-selected": String(selected),
      "aria-controls": "lib-panel", tabindex: selected ? "0" : "-1", "data-tab": key,
    }, `${label}${n}`);
    b.addEventListener("click", () => selectTab(key));
    b.addEventListener("keydown", (ev) => {
      const i = TABS.findIndex(([k]) => k === key);
      let next = null;
      if (ev.key === "ArrowRight") next = TABS[(i + 1) % TABS.length][0];
      else if (ev.key === "ArrowLeft") next = TABS[(i + TABS.length - 1) % TABS.length][0];
      else if (ev.key === "Home") next = TABS[0][0];
      else if (ev.key === "End") next = TABS[TABS.length - 1][0];
      if (next) { ev.preventDefault(); selectTab(next); }
    });
    return b;
  });
  fill(box, ...btns);
  S.nodes.panel.setAttribute("aria-labelledby", `lib-tab-${S.tab}`);
}

function selectTab(key) {
  if (S.tab === key) return;
  S.tab = key;
  renderTabs();
  renderPanel();
  // renderTabs rebuilt the buttons: keep keyboard focus on the tab that is now selected
  const btn = S.nodes.tabs && S.nodes.tabs.querySelector(`[data-tab="${key}"]`);
  if (btn) btn.focus();
}

function renderPanel() {
  if (S.tab === "share") renderShare();
  else renderBrowse(S.tab);
}

// ---- browse (technique / template tabs) ------------------------------------------

function optionsOf(kind, field) {
  return [...new Set(S.items.filter((it) => it.kind === kind).map((it) => it[field]).filter(Boolean))].sort();
}

function matches(it, kind) {
  const f = S.filter;
  if (it.kind !== kind) return false;
  if (f.category && it.category !== f.category) return false;
  if (f.author && it.author !== f.author) return false;
  if (f.worked && !it.worked) return false;
  const q = f.q.trim().toLowerCase();
  if (!q) return true;
  const hay = [it.name, it.title, it.note, it.author, it.summary].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(q);
}

function selectField(id, label, values, current, onChange) {
  const sel = h("select", { id }, h("option", { value: "" }, "전체"), ...values.map((v) => h("option", { value: v }, v)));
  sel.value = values.includes(current) ? current : "";
  sel.addEventListener("change", () => onChange(sel.value));
  return h("div", { class: "field lib-filter" }, h("label", { for: id }, label), sel);
}

function renderBrowse(tab) {
  const kind = KIND_OF_TAB[tab];
  const cats = optionsOf(kind, "category");
  const authors = optionsOf(kind, "author");
  if (S.filter.category && !cats.includes(S.filter.category)) S.filter.category = "";
  if (S.filter.author && !authors.includes(S.filter.author)) S.filter.author = "";
  const q = h("input", { id: "lib-q", type: "search", placeholder: "이름·메모 검색", autocomplete: "off" });
  q.value = S.filter.q;
  q.addEventListener("input", () => { S.filter.q = q.value; renderList(tab); });
  const worked = h("input", { type: "checkbox", id: "lib-worked", checked: S.filter.worked });
  worked.addEventListener("change", () => { S.filter.worked = worked.checked; renderList(tab); });
  const bar = h("div", { class: "lib-filters", role: "group", "aria-label": "목록 거르기" },
    h("div", { class: "field lib-filter" }, h("label", { for: "lib-q" }, "검색"), q),
    selectField("lib-cat", "카테고리", cats, S.filter.category, (v) => { S.filter.category = v; renderList(tab); }),
    selectField("lib-author-f", "작성자", authors, S.filter.author, (v) => { S.filter.author = v; renderList(tab); }),
    h("label", { class: "check lib-worked", for: "lib-worked" }, worked, "잘 먹힘만"));
  S.nodes.pickBar = h("div", { class: "lib-pickbar" });
  S.nodes.count = h("p", { class: "muted small lib-count", "aria-live": "polite" });
  S.nodes.list = h("div", { class: "lib-list" });
  fill(S.nodes.panel, bar, S.nodes.pickBar, S.nodes.count, S.nodes.list);
  renderList(tab);
}

function renderPickBar() {
  const box = S.nodes.pickBar;
  if (!box) return;
  const n = S.picked.size;
  if (!n) { fill(box); return; }
  const go = h("button", { type: "button", class: "btn btn-primary" }, "모티브 재료로 쓰기");
  go.addEventListener("click", sendToMotif);
  const clear = h("button", { type: "button", class: "btn" }, "선택 해제");
  clear.addEventListener("click", () => { S.picked.clear(); renderPickBar(); renderList("tech"); });
  fill(box, h("div", { class: "lib-pick-inner", role: "status" },
    h("span", {}, `${n}개 골랐습니다 (최대 ${MAX_PICK}개)`), go, clear));
}

function sendToMotif() {
  const ids = [...S.picked].slice(0, MAX_PICK);
  try {
    sessionStorage.setItem(MOTIF_IDS_KEY, JSON.stringify(ids));
  } catch {
    S.ctx.toast("이 브라우저에서는 선택을 모티브 화면으로 넘길 수 없습니다. 모티브 화면에서 직접 골라 주세요", "error");
    return;
  }
  location.hash = "#/motif";
}

function renderList(tab, focusId) {
  const box = S.nodes.list;
  if (!box) return;
  const kind = KIND_OF_TAB[tab];
  const total = countOf(kind);
  const shown = S.items.filter((it) => matches(it, kind));
  const kids = [];
  if (S.nodes.count) S.nodes.count.textContent = total && shown.length ? `${shown.length}개` : "";
  if (!total) {
    kids.push(h("p", { class: "muted" }, kind === "technique"
      ? "받은 기법이 없습니다. 위의 받기를 눌러 보세요. 아직 아무도 올리지 않았다면 공유하기 탭에서 내 기법을 올릴 수 있습니다."
      : "받은 기획안 틀이 없습니다. 위의 받기를 눌러 보세요. 공유하기 탭에서 내 기획안을 틀로 올릴 수 있습니다."));
  } else if (!shown.length) {
    kids.push(h("p", { class: "muted" }, "조건에 맞는 항목이 없습니다"));
  } else {
    kids.push(h("div", { class: "motif-grid" }, ...shown.map(itemCard)));
  }
  fill(box, ...kids);
  if (kind === "technique") renderPickBar();
  else fill(S.nodes.pickBar);
  if (focusId) {
    const el = box.querySelector(`[data-pick="${CSS.escape(focusId)}"]`);
    if (el) el.focus({ preventScroll: true });
  }
}

function itemCard(it) {
  const isTech = it.kind === "technique";
  const titleId = `lib-item-${it.id}`;
  const name = isTech ? it.name : it.title;
  const badges = [];
  if (isTech && it.technique_kind) badges.push(h("span", { class: "badge" }, it.technique_kind));
  if (it.worked) badges.push(h("span", { class: "badge lib-worked-badge" }, "잘 먹힘"));
  if (it.mine) badges.push(h("span", { class: "badge lib-mine-badge" }, "내가 올림"));
  const meta = [`작성자 ${it.author}`, it.category, it.shared_at].filter(Boolean).join(" · ");
  const kids = [
    h("div", { class: "card-head" }, h("h3", { id: titleId }, name), h("div", { class: "lib-badges" }, ...badges)),
    h("p", { class: "muted small" }, meta),
  ];
  if (isTech) {
    if (it.summary) kids.push(md(it.summary));
  } else {
    const facts = [];
    if (it.target_sec) facts.push(`목표 ${Math.round(it.target_sec)}초`);
    if (it.hook_type) facts.push(`훅 ${hookName(it.hook_type)}`);
    if (it.narrative_type) facts.push(`서사 ${narrativeName(it.narrative_type)}`);
    if (facts.length) kids.push(h("p", { class: "small" }, facts.join(" · ")));
  }
  if (it.note) kids.push(h("div", { class: "lib-note" }, h("span", { class: "muted small" }, "메모"), md(it.note)));
  const region = h("div", { class: "lib-detail", id: `lib-detail-${it.id}` });
  const toggle = h("button", { type: "button", class: "btn", "aria-expanded": "false", "aria-controls": region.id }, "자세히");
  const actions = [toggle];
  if (isTech) {
    const picked = S.picked.has(it.id);
    const box = h("input", {
      type: "checkbox", checked: picked, disabled: !picked && S.picked.size >= MAX_PICK, "data-pick": it.id,
      "aria-label": `${name} 모티브 재료로 고르기`,
    });
    box.addEventListener("change", () => {
      if (box.checked) S.picked.add(it.id); else S.picked.delete(it.id);
      renderList("tech", it.id);
    });
    actions.unshift(h("label", { class: "check" }, box, "모티브 재료로 고르기"));
  } else {
    actions.push(h("a", { class: "btn btn-primary", href: draftHref(it.id) }, "이 틀로 기획안 만들기"));
  }
  kids.push(h("div", { class: "btn-row" }, ...actions), region);
  toggle.addEventListener("click", () => toggleDetail(it, toggle, region));
  const state = S.details.get(it.id);
  if (state && state.open) { toggle.setAttribute("aria-expanded", "true"); toggle.textContent = "접기"; fillDetail(region, it.id); }
  return h("article", { class: "card motif-card lib-card", "aria-labelledby": titleId }, ...kids);
}

function fillDetail(region, id) {
  const st = S.details.get(id);
  if (!st || !st.open) { fill(region); return; }
  if (st.loading) fill(region, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  else if (st.error) fill(region, h("p", { class: "error", role: "alert" }, st.error));
  else if (st.data) fill(region, md(st.data.body, { dropTitle: String(st.data.name || st.data.title || "") }));
}

async function toggleDetail(it, toggle, region) {
  const mine = S;
  const cur = mine.details.get(it.id) || { open: false, loading: false, data: null, error: null };
  cur.open = !cur.open;
  mine.details.set(it.id, cur);
  toggle.setAttribute("aria-expanded", String(cur.open));
  toggle.textContent = cur.open ? "접기" : "자세히";
  const refill = () => { const r = document.getElementById(`lib-detail-${it.id}`); if (r) fillDetail(r, it.id); };
  if (!cur.open || cur.data) { refill(); return; }
  cur.loading = true;
  cur.error = null;
  refill();
  try {
    cur.data = await mine.ctx.api("GET", `/api/library/items/${encodeURIComponent(it.id)}`);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    cur.error = e.message || "불러오지 못했습니다";
  } finally {
    cur.loading = false;
  }
  if (alive(mine)) refill();          // the card may have been re-rendered meanwhile: look the region up again
}

// ---- share: pick --------------------------------------------------------------

function renderShare() {
  const sh = S.sh;
  S.nodes.shareHead = null;
  if (sh.phase === "pick") renderSharePick();
  else if (sh.phase === "edit") renderShareEdit();
  else renderShareDone();
}

function shareHeading(text) {
  S.nodes.shareHead = h("h2", { id: "lib-share-title", tabindex: "-1" }, text);
  return S.nodes.shareHead;
}

function renderSharePick() {
  const sh = S.sh;
  const mode = h("fieldset", { class: "field seg" }, h("legend", {}, "무엇을 올릴까요"),
    ...[["technique", "내 기법"], ["template", "내 기획안(틀)"]].map(([key, label]) => {
      const r = h("input", { type: "radio", name: "lib-mode", value: key, checked: sh.mode === key });
      r.addEventListener("change", () => { sh.mode = key; sh.choice = null; sh.mine = null; renderShare(); });
      return h("label", { class: "seg-opt" }, r, h("span", {}, label));
    }));
  S.nodes.pickBody = h("div", { class: "lib-pick" });
  S.nodes.shareMsg = h("div", { class: "lib-msg" });
  fill(S.nodes.panel,
    shareHeading("공유하기"),
    h("p", { class: "muted small" },
      "내 기법이나 기획안의 구조를 지인에게 나눕니다. 대본 원문·내 메모·준비물·영상 정보는 올라가지 않습니다. 올리기 전에 미리보기와 확인 단계가 있습니다."),
    mode, S.nodes.pickBody, S.nodes.shareMsg);
  loadMine();
}

async function loadMine() {
  const mine = S;
  const sh = mine.sh;
  const cat = mine.ctx.category;
  if (!cat) {
    fill(mine.nodes.pickBody, h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  const seq = ++sh.mineSeq;                    // a newer load (mode or category change) makes this one stale
  sh.mineLoading = true;
  sh.mineError = null;
  renderPickBody();
  const mode = sh.mode;
  try {
    const url = mode === "technique"
      ? `/api/library/my/techniques?category=${encodeURIComponent(cat)}`
      : `/api/library/my/drafts?category=${encodeURIComponent(cat)}`;
    const res = await mine.ctx.api("GET", url);
    if (!alive(mine) || seq !== sh.mineSeq || sh.phase !== "pick") return;
    sh.mine = mode === "technique" ? res.techniques || [] : res.drafts || [];
  } catch (e) {
    if (!alive(mine) || ignorable(e) || seq !== sh.mineSeq) return;
    sh.mineError = e.message || "목록을 불러오지 못했습니다";
  } finally {
    if (alive(mine) && seq === sh.mineSeq) { sh.mineLoading = false; if (sh.phase === "pick") renderPickBody(); }
  }
}

function techLabel(t) {
  const more = t.ambiguous ? ` — ${String(t.desc || "").slice(0, 60)}` : "";
  return `[${t.kind}] ${t.name}${more}`;
}

function renderPickBody() {
  const sh = S.sh;
  const box = S.nodes.pickBody;
  if (!box) return;
  if (sh.mineLoading) { fill(box, h("p", { class: "muted", role: "status" }, "불러오는 중…")); return; }
  if (sh.mineError) {
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", loadMine);
    fill(box, h("p", { class: "error", role: "alert" }, sh.mineError), h("div", { class: "btn-row" }, retry));
    return;
  }
  const list = sh.mine || [];
  if (!list.length) {
    fill(box, h("p", { class: "muted" }, sh.mode === "technique"
      ? `${S.ctx.category} 카테고리에 올릴 기법이 없습니다. 레퍼런스를 분석하면 기법 카탈로그가 쌓입니다.`
      : `${S.ctx.category} 카테고리에 저장한 기획안이 없습니다. 기획안 화면에서 먼저 만들어 저장하세요.`));
    return;
  }
  const btn = h("button", { type: "button", class: "btn btn-primary", disabled: sh.choice === null || sh.previewBusy }, "미리보기 만들기");
  btn.addEventListener("click", makePreview);
  S.nodes.previewBtn = btn;
  const group = h("fieldset", { class: "field lib-choices" }, h("legend", {}, sh.mode === "technique" ? "올릴 기법 하나 고르기" : "올릴 기획안 하나 고르기"));
  list.forEach((entry, i) => {
    const key = String(i);
    const r = h("input", { type: "radio", name: "lib-choice", value: key, checked: sh.choice === key });
    r.addEventListener("change", () => { sh.choice = key; btn.disabled = sh.previewBusy; });
    const label = sh.mode === "technique" ? techLabel(entry) : `${entry.title} · ${String(entry.date || "").slice(0, 10)}`;
    const extra = [];
    if (sh.mode === "technique" && entry.shared_id) extra.push(h("span", { class: "badge lib-mine-badge" }, "이미 올림"));
    if (sh.mode === "technique" && entry.source_count) extra.push(h("span", { class: "muted small" }, `출처 ${entry.source_count}개`));
    group.append(h("label", { class: "check lib-choice" }, r, h("span", {}, label, " ", ...extra)));
  });
  fill(box, group, h("div", { class: "btn-row" }, btn));
}

async function makePreview() {
  const mine = S;
  const sh = mine.sh;
  const entry = sh.mine && sh.choice !== null ? sh.mine[Number(sh.choice)] : null;
  if (!entry || sh.previewBusy) return;
  sh.previewBusy = true;
  if (S.nodes.previewBtn) S.nodes.previewBtn.disabled = true;
  fill(S.nodes.shareMsg, h("p", { class: "muted", role: "status" }, "미리보기를 만드는 중…"));
  const body = sh.mode === "technique"
    ? { kind: "technique", category: mine.ctx.category, technique_kind: entry.kind, name: entry.name, desc: entry.desc }
    : { kind: "template", draft: entry.path };
  try {
    const pv = await mine.ctx.api("POST", "/api/library/preview", body);
    if (!alive(mine)) return;
    adoptPreview(mine, pv);
    sh.job = null;
    sh.error = null;
    sh.dup = null;
    renderShare();
    focusShareHead();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    fill(S.nodes.shareMsg, h("p", { class: "error", role: "alert" }, e.message || "미리보기를 만들지 못했습니다"));
  } finally {
    if (alive(mine)) { sh.previewBusy = false; if (S.nodes.previewBtn) S.nodes.previewBtn.disabled = sh.choice === null; }
  }
}

function focusShareHead() {
  const el = S && S.nodes.shareHead;
  if (el) el.focus({ preventScroll: true });
}

// Preview -> editable state. Typed note/worked/title survive a summary refresh (keepTyped).
function adoptPreview(mine, pv, keepTyped = false) {
  const sh = mine.sh;
  const fm = pv.frontmatter || {};
  sh.preview = pv;
  sh.mode = pv.kind;
  sh.phase = "edit";
  if (pv.kind === "template") {
    const summaries = pv.summaries || [];
    const rhythms = pv.rhythms || [];
    sh.sections = (fm.sections || []).map((s, i) => ({
      label: String(s.label), sub: s.sub ? String(s.sub) : "", ratio: s.ratio, rhythm: String(rhythms[i] || ""), summary: String(summaries[i] || ""),
    }));
    if (!keepTyped) sh.title = String(fm.title || "");
  } else {
    sh.sections = [];
  }
  if (!keepTyped) {
    sh.note = pv.note ? String(pv.note) : "";
    sh.worked = pv.worked === true;
    sh.askUpload = false;
    sh.askSummarize = false;
    sh.summaryError = null;
  }
}

// ---- share: edit / preview ----------------------------------------------------

const sectionPct = (r) => (typeof r === "number" ? `${Math.round(r * 100)}%` : "");

function previewFacts(pv) {
  const fm = pv.frontmatter || {};
  if (pv.kind === "technique") return [fm.technique_kind, pv.category].filter(Boolean).join(" · ");
  const facts = [pv.category];
  if (fm.target_sec) facts.push(`목표 ${Math.round(fm.target_sec)}초`);
  if (fm.hook_type) facts.push(`훅 ${hookName(fm.hook_type)}`);
  if (fm.narrative_type) facts.push(`서사 ${narrativeName(fm.narrative_type)}`);
  if (fm.pacing && typeof fm.pacing.avg_cut_sec === "number") facts.push(`평균 컷 ${fm.pacing.avg_cut_sec}초`);
  return facts.filter(Boolean).join(" · ");
}

function renderShareEdit() {
  const sh = S.sh;
  const pv = sh.preview;
  const author = (S.status && S.status.author) || "";
  S.nodes.summaryBox = null;                  // set again by templateEditor() for templates only
  const kids = [
    shareHeading(pv.kind === "technique" ? "미리보기 · 기법" : "미리보기 · 기획안 틀"),
    h("p", { class: "muted small" },
      `${previewFacts(pv)} · 작성자 ${author} · 올리기 전까지 저장소는 바뀌지 않습니다. 미리보기는 약 30분 뒤 만료됩니다.`),
  ];
  if (pv.duplicate) {
    kids.push(h("p", { class: "muted small", role: "status" },
      `이미 올린 적 있는 항목입니다(${pv.duplicate.shared_at || ""}${pv.duplicate.changed ? ", 내용이 바뀜" : ""}). 올리면 새 항목이 하나 더 생깁니다.`));
  }
  if (pv.kind === "template") kids.push(templateEditor());
  kids.push(
    h("details", { class: "warn-box lib-markdown" }, h("summary", {}, "올라갈 내용 보기"), md(pv.markdown, { dropTitle: "" }),
      h("p", { class: "muted small" }, "여기에는 고치는 중인 제목·요약·메모가 반영되지 않습니다. 올릴 때 함께 반영됩니다.")));
  const note = h("textarea", { id: "lib-note", rows: "2", maxlength: String(MAX_NOTE) });
  note.value = sh.note;
  note.addEventListener("input", () => { sh.note = note.value; });
  const worked = h("input", { type: "checkbox", id: "lib-worked-share", checked: sh.worked });
  worked.addEventListener("change", () => { sh.worked = worked.checked; });
  kids.push(
    h("div", { class: "field" }, h("label", { for: "lib-note" }, `메모(선택, ${MAX_NOTE}자까지)`), note,
      h("p", { class: "muted hint" }, "지인이 볼 한 줄 메모입니다. 언제 잘 먹혔는지 같은 것을 적어 보세요.")),
    h("label", { class: "check", for: "lib-worked-share" }, worked, "써 보니 잘 먹혔어요"));
  S.nodes.actions = h("div", { class: "btn-row lib-actions" });
  S.nodes.shareMsg = h("div", { class: "lib-msg" });
  kids.push(S.nodes.actions, S.nodes.shareMsg);
  fill(S.nodes.panel, ...kids);
  renderEditActions();
  renderEditMsg();
}

function templateEditor() {
  const sh = S.sh;
  const title = h("input", { id: "lib-title-input", type: "text", maxlength: String(MAX_TITLE) });
  title.value = sh.title;
  title.addEventListener("input", () => {
    sh.title = title.value;
    if (editBlocker() !== sh.lastBlock) renderEditActions();      // rebuild only when the blocker text changes
  });
  S.nodes.summaryBox = h("div", { class: "lib-summary-box" });
  const rows = sh.sections.map((s, i) => {
    const input = h("input", {
      type: "text", maxlength: String(MAX_SUMMARY), "aria-label": `${s.label} 구간 요약`, "data-summary": String(i),
      placeholder: "이 구간이 하는 일 한 줄",
    });
    input.value = s.summary;
    input.addEventListener("input", () => { s.summary = input.value; });
    return h("li", { class: "lib-sec" },
      h("div", { class: "lib-sec-head" },
        h("span", { class: "badge" }, s.sub ? `${s.label} · ${s.sub}` : s.label),
        h("span", { class: "muted small" }, sectionPct(s.ratio)),
        s.rhythm ? h("span", { class: "muted small" }, `샷 리듬 ${s.rhythm}`) : null),
      input);
  });
  return h("section", { class: "lib-template", "aria-labelledby": "lib-sections-title" },
    h("div", { class: "field" }, h("label", { for: "lib-title-input" }, "제목"), title),
    h("h3", { id: "lib-sections-title" }, "섹션별 한 줄 요약"),
    h("p", { class: "muted small" }, "대본 원문이 아니라 '이 구간이 무엇을 하는지'만 적습니다. 비워 두면 그 줄은 요약 없이 올라갑니다. 섹션 요약 받기는 Claude를 한 번 부릅니다(수십 초)."),
    S.nodes.summaryBox,
    h("ol", { class: "lib-sections" }, ...rows));
}

function summaryTyped() {
  return S.sh.sections.some((s) => s.summary.trim());
}

function editBlocker() {
  const sh = S.sh;
  if (sh.preview.kind === "template" && !sh.title.trim()) return "제목이 비어 있습니다";
  return "";
}

function renderEditActions() {
  const box = S.nodes.actions;
  if (!box) return;
  const sh = S.sh;
  const busy = sh.uploading || sh.summarizing;
  const kids = [];
  const upload = h("button", { type: "button", class: "btn btn-primary", disabled: busy || !!editBlocker() || sh.askUpload }, "올리기");
  upload.addEventListener("click", askUpload);
  const discardBtn = h("button", { type: "button", class: "btn", disabled: busy }, "버리기");
  discardBtn.addEventListener("click", discardPreview);
  kids.push(upload, discardBtn);
  const reason = editBlocker();
  sh.lastBlock = reason;
  if (reason) kids.push(h("span", { class: "muted hint" }, reason));
  fill(box, ...kids);
  renderSummaryBox();
}

function renderSummaryBox() {
  const box = S.nodes.summaryBox;
  if (!box) return;
  const sh = S.sh;
  const kids = [];
  const btn = h("button", { type: "button", class: "btn", "data-summarize-btn": "", disabled: sh.summarizing || sh.uploading || sh.askSummarize }, "섹션 요약 받기");
  btn.addEventListener("click", requestSummarize);
  kids.push(h("div", { class: "btn-row" }, btn));
  if (sh.askSummarize) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-summarize-yes": "" }, "요약 받기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", startSummarize);
    no.addEventListener("click", () => {
      sh.askSummarize = false;
      renderEditActions();                                            // rebuilds the button: focus the current one
      const cur = S.nodes.summaryBox && S.nodes.summaryBox.querySelector("[data-summarize-btn]");
      if (cur) cur.focus();
    });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "지금 쓴 섹션 요약이 Claude의 요약으로 바뀝니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (sh.summarizing) {
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "섹션 요약 받는 중" }), h("span", {}, "Claude가 섹션 요약을 만드는 중…")));
  }
  if (sh.summaryError) kids.push(h("p", { class: "error", role: "alert" }, sh.summaryError));
  fill(box, ...kids);
  for (const input of S.nodes.panel.querySelectorAll("[data-summary]")) input.disabled = sh.summarizing;   // Claude is about to overwrite them
  const yes = box.querySelector("[data-summarize-yes]");
  if (yes && sh.askSummarize && document.activeElement && document.activeElement.tagName === "BODY") yes.focus();
}

// Errors and the upload confirm live in their own node so typing never re-announces them.
function renderEditMsg() {
  const box = S.nodes.shareMsg;
  if (!box || S.sh.phase !== "edit") return;
  const sh = S.sh;
  const kids = [];
  const author = (S.status && S.status.author) || "";
  if (sh.askUpload && !sh.uploading && !sh.dup) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-upload-yes": "" }, "올리기 확인");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", () => upload(false));
    no.addEventListener("click", () => { sh.askUpload = false; renderEditActions(); renderEditMsg(); focusUploadBtn(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" },
        `'${author}' 이름으로 공용 라이브러리에 올립니다. 올린 뒤에는 지인이 볼 수 있고 되돌릴 수 없습니다.`),
      h("p", { class: "small" }, "대본 원문·내 메모·준비물은 올라가지 않습니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (sh.uploading) {
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "올리는 중" }), h("span", {}, "올리는 중… (최대 1분)")));
  }
  if (sh.dup && !sh.uploading) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-dup-yes": "" }, "그래도 새로 올리기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", () => upload(true));
    no.addEventListener("click", () => { sh.dup = null; sh.askUpload = false; renderEditActions(); renderEditMsg(); focusUploadBtn(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "이미 올린 항목입니다"),
      h("p", { class: "small" }, sh.dup.message),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (sh.error && !sh.uploading) {
    const e = sh.error;
    const row = [];
    if (e.pushUnknown) {
      const pullBtn = h("button", { type: "button", class: "btn", "data-pull-after": "" }, "받기");
      pullBtn.addEventListener("click", async () => { await pull(); if (alive(S)) renderEditMsg(); });
      row.push(pullBtn);
    }
    if (e.restart) {
      const again = h("button", { type: "button", class: "btn" }, "처음부터");
      again.addEventListener("click", resetShare);
      row.push(again);
    }
    kids.push(h("div", { class: "lib-error", role: "alert" },
      h("p", { class: "error" }, e.message),
      e.pushUnknown ? h("p", { class: "small" }, "먼저 '받기'로 목록을 확인하세요. 이미 올라갔다면 '내가 올림'으로 보입니다. 올라가지 않았다면 다시 올리세요.") : null,
      row.length ? h("div", { class: "btn-row" }, ...row) : null));
  }
  fill(box, ...kids);
  const yes = box.querySelector("[data-upload-yes],[data-dup-yes]");
  if (yes) yes.focus();
}

function focusUploadBtn() {
  const b = S.nodes.actions && S.nodes.actions.querySelector("button");
  if (b) b.focus();
}

function askUpload() {
  const sh = S.sh;
  if (sh.uploading || sh.summarizing || editBlocker()) return;
  sh.askUpload = true;
  sh.error = null;
  sh.dup = null;
  renderEditActions();
  renderEditMsg();
}

function editsPayload() {
  const sh = S.sh;
  if (sh.preview.kind !== "template") return undefined;
  return {
    title: sh.title.trim(),
    sections: sh.sections.map((s) => ({ label: s.label, summary: s.summary.trim() })),
  };
}

async function upload(forceNew) {
  const mine = S;
  const sh = mine.sh;
  if (sh.uploading || !sh.preview) return;
  const blocked = editBlocker();
  if (blocked) { sh.error = { message: blocked }; sh.askUpload = false; renderEditActions(); renderEditMsg(); return; }
  sh.uploading = true;
  sh.askUpload = false;
  sh.dup = null;
  sh.error = null;
  renderEditActions();
  renderEditMsg();
  const body = { preview_id: sh.preview.preview_id, note: sh.note.trim(), worked: sh.worked };
  const edits = editsPayload();
  if (edits) body.edits = edits;
  if (forceNew) body.force_new = true;
  try {
    const res = await mine.ctx.api("POST", "/api/library/confirm", body);
    if (sh.job) markHandled(sh.job.id);
    if (!alive(mine)) return;
    sh.result = res;
    sh.phase = "done";
    sh.uploading = false;
    mine.ctx.toast("공용 라이브러리에 올렸습니다", "info");
    renderShare();
    focusShareHead();
    refreshAfterUpload(mine);
  } catch (e) {
    if (!alive(mine) || (e && e.error === "aborted")) return;
    sh.uploading = false;
    const data = e.data || {};
    if (e.error === "duplicate") {
      sh.dup = { message: (data.changed ? `${e.message} (내용이 바뀜)` : e.message) };
    } else if (data.push_unknown) {
      sh.error = { message: e.message, pushUnknown: true };
    } else if (e.error === "preview_expired" || e.error === "preview_not_found") {
      sh.error = { message: e.message, restart: true };
    } else if (e.status === 409 || e.error === "library_busy") {
      sh.error = { message: e.message || "다른 작업이 라이브러리를 쓰는 중입니다. 잠시 뒤 다시 올려 주세요" };
    } else {
      sh.error = { message: e.message || "올리지 못했습니다" };
    }
    renderEditActions();
    renderEditMsg();
  }
}

// After a successful upload: refresh the list and the "shared" counts quietly.
async function refreshAfterUpload(mine) {
  try {
    mine.status = await mine.ctx.api("GET", "/api/library/status");
    await fetchItems(mine);
    if (!alive(mine)) return;
    renderInfo();
    renderTabs();
  } catch { /* the list refreshes on the next pull */ }
}

async function discardPreview() {
  const mine = S;
  const sh = mine.sh;
  if (sh.uploading || sh.summarizing || !sh.preview) return;
  const id = sh.preview.preview_id;
  if (sh.job) markHandled(sh.job.id);
  try {
    await mine.ctx.api("POST", "/api/library/discard", { preview_id: id });
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
  }
  if (!alive(mine)) return;
  mine.ctx.toast("미리보기를 버렸습니다. 올리지 않았습니다", "info");
  resetShare();
}

function resetShare() {
  stopPolling();
  const mode = S.sh.mode;
  S.sh = freshShare();
  S.sh.mode = mode;
  renderShare();
  focusShareHead();
}

// ---- share: done --------------------------------------------------------------

function renderShareDone() {
  const sh = S.sh;
  const r = sh.result || {};
  const again = h("button", { type: "button", class: "btn btn-primary" }, "또 올리기");
  again.addEventListener("click", resetShare);
  const mineTab = h("button", { type: "button", class: "btn" }, "올린 것 보기");
  mineTab.addEventListener("click", () => selectTab(sh.mode === "template" ? "tpl" : "tech"));
  fill(S.nodes.panel,
    shareHeading("올렸습니다"),
    h("p", { role: "status" }, "공용 라이브러리에 올렸습니다. 지인이 받기를 하면 볼 수 있습니다."),
    r.unsummarized ? h("p", { class: "muted small" }, `요약이 비어 있는 섹션 ${r.unsummarized}개는 요약 없이 올라갔습니다.`) : null,
    h("p", { class: "muted small mono" }, r.id || ""),
    h("div", { class: "btn-row" }, again, mineTab));
}

// ---- share: summary job -------------------------------------------------------

function requestSummarize() {
  const sh = S.sh;
  if (sh.summarizing || sh.uploading) return;
  if (summaryTyped() && !sh.askSummarize) {
    sh.askSummarize = true;
    renderEditActions();
    return;
  }
  startSummarize();
}

async function startSummarize() {
  const mine = S;
  const sh = mine.sh;
  if (sh.summarizing || sh.uploading || !sh.preview) return;
  sh.askSummarize = false;
  sh.summaryError = null;
  sh.summarizing = true;
  sh.pollErrors = 0;
  renderEditActions();
  try {
    const res = await mine.ctx.api("POST", "/api/library/summarize", { preview_id: sh.preview.preview_id });
    if (!alive(mine)) return;
    sh.job = res.job;
    schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    sh.summarizing = false;
    sh.summaryError = e.message || "섹션 요약을 받지 못했습니다";
    renderEditActions();
  }
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.sh.timer = setTimeout(async () => {
    mine.sh.timer = null;
    if (!alive(mine) || !mine.sh.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(mine.sh.job.id)}`);
      if (!alive(mine)) return;
      if (mine.sh.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.sh.pollErrors = 0;
      mine.sh.job = data.job;
      if (!isActive(data.job)) { finishSummary(mine, data.job); return; }
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 섹션 요약을 다시 받으세요", "error");
        mine.sh.summarizing = false;
        mine.sh.job = null;
        if (mine.sh.phase === "edit") renderEditActions();
        return;
      }
      mine.sh.pollErrors += 1;
      if (mine.sh.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.sh.job)) schedulePoll();
  }, pollDelay(mine.sh.pollErrors));
}

function finishSummary(mine, job) {
  const sh = mine.sh;
  sh.summarizing = false;
  const item = job.items && job.items[0];
  const pv = job.status === "done" && item && item.preview && item.preview.preview;
  if (pv && sh.preview && pv.preview_id === sh.preview.preview_id) {
    adoptPreview(mine, pv, true);                 // keeps the typed title / note / worked
    markHandled(job.id);
    mine.ctx.toast("섹션 요약을 받았습니다. 고쳐서 올리세요", "info");
    renderShare();
    focusShareHead();
    return;
  }
  sh.summaryError = (item && item.message) || job.message || "섹션 요약을 받지 못했습니다";
  markHandled(job.id);
  if (sh.phase === "edit") renderEditActions();
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  S = freshState(el, ctx);
  await load();
}

// The "내 기법/내 기획안" lists depend on the category; an open preview keeps its own category.
export function onCategory() {
  if (!S || !S.status || !S.status.connected) return;
  if (S.tab === "share" && S.sh.phase === "pick") {
    S.sh.mine = null;
    S.sh.choice = null;
    S.sh.mineSeq += 1;                  // drop a response still in flight for the old category
    renderShare();
  }
}

export function unmount() {
  stopPolling();
  S = null;
}
