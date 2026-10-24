// 모티브 화면
//   상태(단계·허용 카드 종류·저장된 카드) → "새 카드 뽑기"(작업: 자료 모으기 → Claude) → 미리보기(편집·선택) → 저장.
//   미리보기 단계에서는 index.md가 바뀌지 않는다. 저장은 사용자가 누른 뒤에만, 고른 카드만 보낸다.
import { h, fill } from "/static/dom.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.motif.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const MAX_TOPIC = 100;
const TYPE_KO = { flow: "흐름", combo: "조합", technique: "기법", angle: "각도" };
const FILTER_ORDER = ["flow", "combo", "technique", "angle"];
const STEP_TEXT = { context: "자료 모으는 중", claude: "Claude가 카드 만드는 중" };
const CATALOG_HINT = "기법 카탈로그 정리(같은 기법 합치기)는 터미널 /yt-coach:yt-inspire 에서 할 수 있어요";

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const refHref = (id) => `#/refs/${encodeURIComponent(id)}`;
const typeName = (t) => TYPE_KO[t] || String(t);

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved (or discarded) in this browser is not restored after a reload.
function markHandled(jobId) {
  if (!jobId) return;
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

function previewOf(job) {
  const item = job && job.items && job.items[0];
  return item && item.state === "awaiting" && item.preview && Array.isArray(item.preview.cards) && item.preview.cards.length
    ? item.preview : null;
}

// ---- page state ---------------------------------------------------------------

let S = null;

// Each category change builds a fresh state, so late async work of the old one sees S !== mine and stops.
function freshState(el, ctx, category) {
  return {
    el, ctx, category, data: null, cards: [], job: null, jobId: null, preview: null, edits: [],
    topic: "", filter: "all", starBusy: new Set(),
    timer: null, pollErrors: 0, running: false, saving: false,
    runError: null, saveError: null, listError: null, confirmRerun: false, confirmDiscard: false,
    runSig: null, alertSig: null, nodes: {},
  };
}

function alive(mine) {
  return S === mine && mine.ctx.isCurrent();
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

function ignorable(e) {
  return !e || e.error === "aborted";
}

function focusStatus() {
  const t = S && S.nodes.status;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  const cat = mine.category;
  if (!cat) {
    fill(mine.el, h("h1", {}, "모티브"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", {}, "모티브"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const [data, cur] = await Promise.all([
      mine.ctx.api("GET", `/api/motif?category=${encodeURIComponent(cat)}`),
      mine.ctx.api("GET", "/api/jobs/current?kind=motif"),
    ]);
    if (!alive(mine)) return;
    mine.data = data;
    mine.cards = data.cards || [];
    const job = cur.job;
    // The latest motif job may belong to another category.
    if (job && job.category === cat && !isHandled(job.id)) {
      if (isActive(job) || (job.status === "done" && previewOf(job))) {
        adoptJob(mine, job);
      } else if (job.status === "failed" || job.status === "stopped") {
        adoptJob(mine, job);          // shown once, then not again
        markHandled(job.id);
      }
    }
    render();
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e);
  }
}

function loadError(e) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) load(); });
  fill(S.el, h("h1", {}, "모티브"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

function adoptJob(mine, job) {
  mine.job = job;
  const preview = job.status === "done" ? previewOf(job) : null;
  if (preview && (!mine.preview || mine.jobId !== job.id)) {
    mine.preview = preview;
    mine.jobId = job.id;
    initEdits(mine, preview);
  }
}

function initEdits(mine, preview) {
  mine.edits = preview.cards.map((c) => {
    const how = (c.how || []).join("\n");
    return {
      keep: true, star: false, title: String(c.title || ""), idea: String(c.idea || ""), how,
      orig: { title: String(c.title || ""), idea: String(c.idea || ""), how },
    };
  });
  mine.confirmRerun = false;
  mine.confirmDiscard = false;
  mine.runError = null;
  mine.saveError = null;
}

const isDirty = (e) => !e.keep || e.star || e.title !== e.orig.title || e.idea !== e.orig.idea || e.how !== e.orig.how;
const hasEdits = () => !!S.preview && S.edits.some(isDirty);

// ---- run + polling ------------------------------------------------------------

function requestRerun() {
  if (S.preview && hasEdits() && !S.confirmRerun) {
    S.confirmRerun = true;
    renderRunBox();
    const yes = S.nodes.run.querySelector("[data-rerun-yes]");
    if (yes) yes.focus();
    return;
  }
  startRun();
}

async function startRun() {
  const mine = S;
  if (mine.running || mine.saving || isActive(mine.job)) return;
  mine.running = true;
  mine.runError = null;
  mine.confirmRerun = false;
  mine.alertSig = null;
  renderRunBox();
  try {
    const res = await mine.ctx.api("POST", "/api/motif/run", { category: mine.category, topic: mine.topic.trim() || null });
    if (!alive(mine)) return;
    if (mine.jobId) markHandled(mine.jobId);
    mine.job = res.job;
    mine.preview = null;
    mine.jobId = null;
    mine.edits = [];
    mine.pollErrors = 0;
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.runError = e.message || "카드를 뽑지 못했습니다";
  } finally {
    if (alive(mine)) { mine.running = false; render(); if (!mine.runError) focusStatus(); }
  }
}

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    if (!alive(mine) || !mine.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(mine.job.id)}`);
      if (!alive(mine)) return;
      if (mine.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.pollErrors = 0;
      const wasActive = isActive(mine.job);
      adoptJob(mine, data.job);
      if (wasActive && !isActive(mine.job)) {
        if (mine.job.status === "failed" || mine.job.status === "stopped") markHandled(mine.job.id);
        render();
        focusStatus();
      } else {
        renderRunBox();
      }
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 카드를 다시 뽑으세요", "error");
        mine.job = null;
        render();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

// ---- save / discard / star ----------------------------------------------------

const keptIndexes = () => S.edits.map((e, i) => (e.keep ? i : -1)).filter((i) => i >= 0);

// Why the save button is disabled (empty string when it can be used).
function saveBlocker() {
  const kept = keptIndexes();
  if (!kept.length) return "저장할 카드를 하나 이상 골라 주세요";
  for (const i of kept) {
    const e = S.edits[i];
    if (!e.title.trim()) return `${i + 1}번 카드의 제목이 비어 있습니다`;
    if (!e.idea.trim()) return `${i + 1}번 카드의 아이디어가 비어 있습니다`;
  }
  return "";
}

function cardPayload(i) {
  const src = S.preview.cards[i];
  const e = S.edits[i];
  return {
    type: src.type,
    title: e.title.trim(),
    idea: e.idea.trim(),
    how: e.how.split("\n").map((s) => s.trim()).filter(Boolean),
    basis: src.basis || null,
    evidence: src.evidence || [],
  };
}

async function save() {
  const mine = S;
  if (mine.saving || !mine.preview || saveBlocker()) return;
  const kept = keptIndexes();
  const cards = kept.map(cardPayload);
  const star = kept.map((i, pos) => (mine.edits[i].star ? pos : -1)).filter((pos) => pos >= 0);
  mine.saving = true;
  mine.saveError = null;
  updateActions();
  renderAlerts();
  try {
    const res = await mine.ctx.api("POST", "/api/motif/save", { category: mine.category, cards, star });
    if (!alive(mine)) return;
    markHandled(mine.jobId);
    mine.ctx.toast(`카드 ${cards.length}장을 저장했습니다`, "info");
    mine.cards = res.cards || mine.cards;
    mine.preview = null;
    mine.edits = [];
    mine.job = null;
    mine.jobId = null;
    mine.saving = false;
    render();
    focusStatus();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.saveError = e.message || "저장하지 못했습니다";
    mine.saving = false;
    updateActions();
    renderAlerts();
  }
}

function askDiscard() {
  if (S.saving) return;
  S.confirmDiscard = true;
  renderAlerts();
  const yes = S.nodes.alerts && S.nodes.alerts.querySelector("[data-discard-yes]");
  if (yes) yes.focus();
}

function discard() {
  if (S.saving) return;
  markHandled(S.jobId);
  S.preview = null;
  S.edits = [];
  S.job = null;
  S.jobId = null;
  S.saveError = null;
  S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  render();
  focusStatus();
}

async function toggleStar(card) {
  const mine = S;
  if (mine.starBusy.has(card.id)) return;      // same card is already in flight
  const target = !card.starred;
  mine.starBusy.add(card.id);
  mine.listError = null;
  card.starred = target;
  renderList(card.id);
  try {
    const res = await mine.ctx.api("POST", "/api/motif/star", { category: mine.category, id: card.id, on: target });
    if (!alive(mine)) return;
    mine.cards = res.cards || mine.cards;
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    card.starred = !target;
    mine.listError = `★ 표시를 바꾸지 못했습니다. ${e.message || ""}`.trim();
  } finally {
    mine.starBusy.delete(card.id);
    if (alive(mine)) renderList(card.id);
  }
}

async function copyCommand(card) {
  const mine = S;
  const hint = (mine.data && mine.data.draft_hint) || "/yt-coach:yt-draft";
  const text = `${hint} ${mine.category} ${card.id} "${String(card.title).replace(/"/g, "'")}"`;
  try {
    await navigator.clipboard.writeText(text);
    if (alive(mine)) mine.ctx.toast("기획안 명령을 복사했습니다", "info");
  } catch {
    if (alive(mine)) mine.ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
  }
}

// ---- render -------------------------------------------------------------------

function render() {
  const d = S.data;
  S.nodes = {};
  S.runSig = null;
  S.alertSig = null;
  S.nodes.status = h("h1", { id: "motif-title" }, "모티브");
  const allowed = (d.allowed_ko || []).join("·") || "아직 없음";
  const kids = [
    S.nodes.status,
    h("p", { class: "lead" }, `${d.stage}단계 · 만들 수 있는 카드: ${allowed}`),
  ];
  const notes = [];
  if (!d.has_profile) notes.push("profile.md가 없어 내 취향을 반영하지 못합니다. 터미널에서 /yt-coach:yt-init 으로 만들 수 있어요");
  if (!d.has_typify) {
    notes.push(h("span", {}, "유형화 결과가 없어 흐름·조합 카드는 근거가 약할 수 있어요. ",
      h("a", { href: "#/typify" }, "유형화 하러 가기")));
  }
  if ((d.allowed_types || []).length < FILTER_ORDER.length) {
    notes.push(`레퍼런스가 더 모이면 만들 수 있는 카드 종류가 늘어납니다 (지금 ref ${d.ref}개)`);
  }
  for (const n of notes) kids.push(h("p", { class: "muted hint" }, n));
  kids.push(h("p", { class: "muted small" }, CATALOG_HINT));

  S.nodes.run = h("section", { class: "job-box motif-run", "aria-label": "새 카드 뽑기" });
  S.nodes.body = h("div", { class: "motif-body" });
  S.nodes.list = h("section", { class: "motif-sec", "aria-labelledby": "motif-list-title" });
  kids.push(S.nodes.run, S.nodes.body, S.nodes.list);
  fill(S.el, ...kids);
  renderRunBox();
  renderBody();
  renderList();
}

// Topic input, run button, progress and errors. Re-rendered by polling without touching the (editable) preview below.
function renderRunBox() {
  const box = S.nodes.run;
  if (!box) return;
  const d = S.data;
  const job = S.job;
  const active = isActive(job);
  const failed = !!job && (job.status === "failed" || job.status === "stopped");
  const item0 = job && job.items && job.items[0];
  const sig = JSON.stringify([active, failed, S.running, S.runError, S.confirmRerun, d.ref,
    item0 && item0.step, item0 && item0.message, job && job.message, !!S.preview]);
  if (sig === S.runSig && box.childNodes.length) return;
  S.runSig = sig;
  if (d.ref === 0) {
    fill(box, h("p", { class: "muted" }, "ref 영상이 없어 카드를 뽑을 수 없습니다. 레퍼런스를 먼저 넣어 주세요."),
      h("a", { class: "btn btn-primary", href: "#/add" }, "레퍼런스 추가로 가기"));
    return;
  }
  const busy = S.running || active || S.saving;
  const topic = h("input", {
    id: "motif-topic", type: "text", maxlength: String(MAX_TOPIC), placeholder: "예: 편의점 라면", disabled: busy,
  });
  topic.value = S.topic;
  topic.addEventListener("input", () => { S.topic = topic.value; });
  const btn = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "새 카드 뽑기");
  btn.addEventListener("click", requestRerun);
  const kids = [
    h("div", { class: "field" }, h("label", { for: "motif-topic" }, "주제(선택)"), topic),
    h("div", { class: "btn-row" }, btn, h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (S.confirmRerun) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-rerun-yes": "" }, "다시 뽑기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", startRun);
    no.addEventListener("click", () => { S.confirmRerun = false; renderRunBox(); S.nodes.run.querySelector("button").focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "지금 미리보기에서 고친 내용은 사라집니다. 저장한 카드는 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (active) {
    const step = (item0 && STEP_TEXT[item0.step]) || "준비 중";
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "카드 뽑기 진행" }),
      h("span", {}, `${step}…`)));
  }
  if (failed) {
    kids.push(h("p", { class: "error", role: "alert" },
      `${(item0 && item0.message) || job.message || "카드를 뽑지 못했습니다"} 저장된 카드는 그대로입니다.`));
  }
  if (S.runError) kids.push(h("p", { class: "error", role: "alert" }, S.runError));
  // Keep a half-typed topic and focus when only the status changed.
  const hadFocus = document.activeElement && document.activeElement.id === "motif-topic";
  fill(box, ...kids);
  if (hadFocus && !busy) topic.focus();
}

function renderBody() {
  const body = S.nodes.body;
  if (!body) return;
  if (!S.preview) { fill(body); return; }
  fill(body, previewView());
  updateActions();
  renderAlerts();
}

function evidenceLinks(ids, titles, label) {
  if (!ids.length) return null;
  const list = h("ul", { class: "evidence-list", "aria-label": label });
  for (const id of ids) list.append(h("li", {}, h("a", { href: refHref(id) }, titles.get(id) || id)));
  return list;
}

function previewCard(card, i, titles) {
  const e = S.edits[i];
  const n = i + 1;
  const keep = h("input", { type: "checkbox", checked: e.keep, "aria-label": `${n}번 카드 저장` });
  keep.addEventListener("change", () => { e.keep = keep.checked; updateActions(); });
  const title = h("input", { type: "text", "aria-label": `${n}번 카드 제목`, maxlength: "120" });
  title.value = e.title;
  title.addEventListener("input", () => { e.title = title.value; updateActions(); });
  const idea = h("textarea", { rows: "3", "aria-label": `${n}번 카드 아이디어` });
  idea.value = e.idea;
  idea.addEventListener("input", () => { e.idea = idea.value; updateActions(); });
  const how = h("textarea", { rows: "4", "aria-label": `${n}번 카드 찍는 법 (한 줄에 한 단계)` });
  how.value = e.how;
  how.addEventListener("input", () => { e.how = how.value; });
  const star = h("input", { type: "checkbox", checked: e.star });
  star.addEventListener("change", () => { e.star = star.checked; });
  return h("article", { class: "card motif-card" },
    h("div", { class: "card-head" },
      h("label", { class: "check" }, keep, "저장"),
      h("span", { class: `badge motif-badge motif-${card.type}` }, typeName(card.type))),
    h("div", { class: "field" }, h("label", {}, "제목", title)),
    h("div", { class: "field" }, h("label", {}, "아이디어", idea)),
    h("div", { class: "field" }, h("label", {}, "찍는 법 (한 줄에 한 단계)", how)),
    card.basis ? h("p", { class: "small" }, h("span", { class: "muted" }, "바탕 "), card.basis) : null,
    evidenceLinks(card.evidence || [], titles, `${n}번 카드 근거 영상`),
    h("label", { class: "check" }, star, "저장하면서 ★"));
}

function previewView() {
  const p = S.preview;
  const titles = new Map(Object.entries(p.titles || {}));
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "motif-save-hint" }, "선택한 카드 저장");
  const rerunBtn = h("button", { type: "button", class: "btn" }, "다시 뽑기");
  const discardBtn = h("button", { type: "button", class: "btn" }, "버리기");
  saveBtn.addEventListener("click", save);
  rerunBtn.addEventListener("click", requestRerun);
  discardBtn.addEventListener("click", askDiscard);
  S.nodes.saveBtn = saveBtn;
  S.nodes.rerunBtn = rerunBtn;
  S.nodes.discardBtn = discardBtn;
  S.nodes.hint = h("p", { class: "muted hint", id: "motif-save-hint" });
  S.nodes.alerts = h("div", { class: "preview-alerts" });
  S.alertSig = null;
  return h("section", { class: "motif-sec", "aria-labelledby": "motif-preview-title" },
    h("h2", { id: "motif-preview-title" }, p.topic ? `미리보기 · 주제: ${p.topic}` : "미리보기"),
    h("p", { class: "muted small" }, "저장하기 전에는 파일이 바뀌지 않습니다. 제목·아이디어·찍는 법은 고칠 수 있고, 다른 화면으로 가면 고친 내용은 사라집니다."),
    h("div", { class: "motif-grid" }, ...p.cards.map((c, i) => previewCard(c, i, titles))),
    h("div", { class: "preview-actions" },
      h("div", { class: "btn-row" }, saveBtn, rerunBtn, discardBtn), S.nodes.hint, S.nodes.alerts));
}

function updateActions() {
  if (!S.preview || !S.nodes.saveBtn) return;
  const reason = saveBlocker();
  const n = keptIndexes().length;
  S.nodes.saveBtn.textContent = `선택한 카드 저장 (${n}장)`;
  S.nodes.saveBtn.disabled = S.saving || !!reason;
  S.nodes.rerunBtn.disabled = S.saving || S.running || isActive(S.job);
  S.nodes.discardBtn.disabled = S.saving;
  S.nodes.hint.textContent = reason;
}

// Alerts live in their own node and are rebuilt only when their content changes (no re-announcing on keystrokes).
function renderAlerts() {
  const box = S.nodes.alerts;
  if (!box || !S.preview) return;
  const sig = JSON.stringify([S.saveError, S.confirmDiscard, S.saving]);
  if (sig === S.alertSig) return;
  S.alertSig = sig;
  const kids = [];
  if (S.confirmDiscard) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-discard-yes": "", disabled: S.saving }, "버리기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", discard);
    no.addEventListener("click", () => { S.confirmDiscard = false; renderAlerts(); S.nodes.discardBtn.focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "미리보기를 버립니다. 고친 내용은 사라집니다. 저장한 카드는 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (S.saveError) kids.push(h("p", { class: "error", role: "alert" }, S.saveError));
  fill(box, ...kids);
}

// ---- saved cards --------------------------------------------------------------

function savedCard(card) {
  const titleId = `motif-card-${card.id}`;
  const star = h("button", {
    type: "button", class: "btn btn-icon star-btn", "aria-pressed": String(!!card.starred),
    "aria-label": `${card.title} 즐겨찾기`, "data-star-id": card.id,
  }, card.starred ? "★" : "☆");
  star.addEventListener("click", () => toggleStar(card));
  const copy = h("button", { type: "button", class: "btn" }, "기획안 명령 복사");
  copy.addEventListener("click", () => copyCommand(card));
  const titles = new Map();
  const ids = [];
  for (const ev of card.evidence || []) {
    if (ev && ev.id) { ids.push(ev.id); if (ev.title) titles.set(ev.id, ev.title); }
  }
  return h("article", { class: "card motif-card", "aria-labelledby": titleId },
    h("div", { class: "card-head" },
      h("div", { class: "motif-head-left" },
        h("span", { class: `badge motif-badge motif-${card.type}` }, card.type_ko || typeName(card.type)),
        h("span", { class: "muted small mono" }, card.id)),
      star),
    h("h3", { id: titleId }, card.title),
    card.idea ? h("p", { class: "md-text" }, card.idea) : null,
    card.how && card.how.length
      ? h("ol", { class: "motif-how", "aria-label": "찍는 법" }, ...card.how.map((s) => h("li", {}, s))) : null,
    card.basis ? h("p", { class: "small" }, h("span", { class: "muted" }, "바탕 "), card.basis) : null,
    evidenceLinks(ids, titles, "근거 영상"),
    card.made ? h("p", { class: "muted small" }, `만든 날 ${card.made}`) : null,
    h("div", { class: "btn-row" }, copy));
}

function filterButtons(types) {
  const defs = [["all", "전체"]];
  if (S.cards.some((c) => c.starred)) defs.push(["star", "★"]);
  for (const t of FILTER_ORDER) if (types.has(t)) defs.push([t, typeName(t)]);
  if (!defs.some(([k]) => k === S.filter)) S.filter = "all";
  const row = h("div", { class: "filter-row", role: "group", "aria-label": "카드 종류 거르기" });
  for (const [key, label] of defs) {
    const b = h("button", { type: "button", class: "btn filter-btn", "aria-pressed": String(S.filter === key) }, label);
    b.addEventListener("click", () => { S.filter = key; renderList(); });
    row.append(b);
  }
  return row;
}

// Rebuilds the saved-card list. `focusId` keeps keyboard focus on that card's star button.
function renderList(focusId) {
  const box = S.nodes.list;
  if (!box) return;
  const types = new Set(S.cards.map((c) => c.type));
  const kids = [h("h2", { id: "motif-list-title" }, `저장한 카드 (${S.cards.length}장)`)];
  if (!S.cards.length) {
    kids.push(h("p", { class: "muted" }, "아직 저장한 카드가 없습니다. 새 카드를 뽑아 보세요"));
  } else {
    kids.push(filterButtons(types));
    const shown = S.cards.filter((c) => S.filter === "all" || (S.filter === "star" ? c.starred : c.type === S.filter));
    S.nodes.listAlert = h("div", { class: "list-alert" });
    kids.push(S.nodes.listAlert);
    kids.push(h("div", { class: "motif-grid" }, ...shown.map(savedCard)));
  }
  fill(box, ...kids);
  if (S.listError && S.nodes.listAlert) fill(S.nodes.listAlert, h("p", { class: "error", role: "alert" }, S.listError));
  if (focusId) {
    const btn = box.querySelector(`[data-star-id="${CSS.escape(focusId)}"]`);
    if (btn) btn.focus({ preventScroll: true });
  }
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  S = freshState(el, ctx, ctx.category);
  await load();
}

export function onCategory(category) {
  if (!S || S.category === category) return;
  stopPolling();
  S = freshState(S.el, S.ctx, category);
  load();
}

export function unmount() {
  stopPolling();
  S = null;
}
