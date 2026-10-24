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
const LIB_IDS_KEY = "ytc.motif.library_ids";      // set by the library screen ("모티브 재료로 쓰기"); chosen per run, not saved
const MAX_LIB = 10;
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

function readLibIds() {
  try {
    const v = JSON.parse(sessionStorage.getItem(LIB_IDS_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch { return []; }
}

function writeLibIds(ids) {
  try {
    if (ids.length) sessionStorage.setItem(LIB_IDS_KEY, JSON.stringify(ids));
    else sessionStorage.removeItem(LIB_IDS_KEY);
  } catch { /* storage unavailable: the choice only lives on this screen */ }
}

// Friend techniques for the chooser. null when the library is not connected (or cannot be read): no chooser then.
async function loadLibrary(mine) {
  try {
    const st = await mine.ctx.api("GET", "/api/library/status");
    if (!st.connected) return null;
    const res = await mine.ctx.api("GET", "/api/library/items?kind=technique");
    const items = res.items || [];
    const have = new Set(items.map((it) => it.id));
    return { items, selected: new Set(readLibIds().filter((id) => have.has(id)).slice(0, MAX_LIB)) };
  } catch {
    return null;
  }
}

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
    el, ctx, category, data: null, cards: [], lib: null, job: null, jobId: null, preview: null, edits: [],
    topic: "", filter: "all", starBusy: new Set(), pinned: new Set(), saveWarning: null, warnShown: false,
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
    const [data, cur, lib] = await Promise.all([
      mine.ctx.api("GET", `/api/motif?category=${encodeURIComponent(cat)}`),
      mine.ctx.api("GET", "/api/jobs/current?kind=motif"),
      loadLibrary(mine),
    ]);
    if (!alive(mine)) return;
    mine.lib = lib;
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

// `from` is where the confirm appears: next to the button that was pressed ("run" box or "preview" actions).
function requestRerun(from) {
  if (S.preview && hasEdits() && !S.confirmRerun) {
    S.confirmRerun = from;
    S.alertSig = null;
    renderRunBox();
    renderAlerts();
    const box = from === "preview" ? S.nodes.alerts : S.nodes.run;
    const yes = box && box.querySelector("[data-rerun-yes]");
    if (yes) yes.focus();
    return;
  }
  startRun();
}

function rerunConfirm(from) {
  const yes = h("button", { type: "button", class: "btn btn-danger", "data-rerun-yes": "" }, "다시 뽑기");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  yes.addEventListener("click", startRun);
  no.addEventListener("click", () => {
    S.confirmRerun = false;
    S.alertSig = null;
    renderRunBox();
    renderAlerts();
    const back = from === "preview" ? S.nodes.rerunBtn : S.nodes.run.querySelector("button");
    if (back) back.focus();
  });
  return h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, "지금 미리보기에서 고친 내용은 사라집니다. 저장한 카드는 그대로입니다."),
    h("div", { class: "btn-row" }, yes, no));
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
    const body = { category: mine.category };
    const topic = mine.topic.trim();
    if (topic) body.topic = topic;
    const libIds = mine.lib ? [...mine.lib.selected] : [];
    if (libIds.length) body.library_ids = libIds;
    const res = await mine.ctx.api("POST", "/api/motif/run", body);
    if (!alive(mine)) return;
    if (libIds.length) { mine.lib.selected.clear(); writeLibIds([]); }       // chosen per run, not kept
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
    const res = await mine.ctx.api("POST", "/api/motif/save", { category: mine.category, cards, star, job_id: mine.jobId });
    markHandled(mine.jobId);                    // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.ctx.toast(`카드 ${cards.length}장을 저장했습니다`, "info");
    mine.saveWarning = res.warning ? String(res.warning) : null;
    mine.warnShown = false;
    mine.cards = res.cards || mine.cards;
    mine.preview = null;
    mine.edits = [];
    mine.job = null;
    mine.jobId = null;
    mine.saving = false;
    render();
    focusStatus();
  } catch (e) {
    // An aborted request (page left mid-save) may still have been saved by the server: never restore that preview.
    if (e && e.error === "aborted") markHandled(mine.jobId);
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
  mine.pinned.add(card.id);                    // stays visible under the ★ filter until the request settles
  renderList(card.id);
  try {
    const res = await mine.ctx.api("POST", "/api/motif/star", { category: mine.category, id: card.id, on: target });
    if (!alive(mine)) return;
    // Apply only this card's value: responses of different cards can arrive out of order.
    const fresh = (res.cards || []).find((c) => c.id === card.id);
    if (fresh) card.starred = !!fresh.starred;
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    card.starred = !target;
    mine.listError = `★ 표시를 바꾸지 못했습니다. ${e.message || ""}`.trim();
  } finally {
    mine.starBusy.delete(card.id);
    mine.pinned.delete(card.id);
    if (alive(mine)) renderList(card.id);
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
  if (d.ref === 0) {
    notes.push(h("span", {}, "먼저 레퍼런스를 넣어 주세요 ", h("a", { href: "#/add" }, "레퍼런스 추가로 가기")));
  }
  if (!d.has_profile) notes.push("프로필이 없어 카드가 일반적으로 나옵니다 — /yt-coach:yt-init 으로 만들 수 있어요");
  if (d.stage === 3 && !d.has_typify) {
    notes.push(h("span", {}, "유형화를 먼저 하면 흐름·조합 카드가 나옵니다 ", h("a", { href: "#/typify" }, "유형화 하러 가기")));
  }
  for (const n of notes) kids.push(h("p", { class: "muted hint" }, n));
  kids.push(h("p", { class: "muted small" }, CATALOG_HINT));

  S.nodes.lib = h("div", { class: "lib-choose" });
  S.nodes.body = h("div", { class: "motif-body" });
  S.nodes.list = h("section", { class: "motif-sec", "aria-labelledby": "motif-list-title" });
  if (d.ref > 0) {            // without ref there is nothing to run: no empty section
    S.nodes.run = h("section", { class: "job-box motif-run", "aria-label": "새 카드 뽑기" });
    kids.push(S.nodes.run);
  }
  kids.push(S.nodes.body, S.nodes.list);
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
  const sig = JSON.stringify([active, failed, S.running, S.saving, S.runError, S.confirmRerun, d.ref,
    item0 && item0.step, item0 && item0.message, job && job.message, !!S.preview]);
  if (sig === S.runSig && box.childNodes.length) return;
  S.runSig = sig;
  if (d.ref === 0) { fill(box); return; }
  const busy = S.running || active || S.saving;
  const topic = h("input", {
    id: "motif-topic", type: "text", maxlength: String(MAX_TOPIC), placeholder: "예: 편의점 라면", disabled: busy,
  });
  topic.value = S.topic;
  topic.addEventListener("input", () => { S.topic = topic.value; });
  const btn = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "새 카드 뽑기");
  btn.addEventListener("click", () => requestRerun("run"));
  const kids = [
    h("div", { class: "field" }, h("label", { for: "motif-topic" }, "주제(선택)"), topic),
    S.nodes.lib,
    h("div", { class: "btn-row" }, btn, h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (S.confirmRerun === "run") kids.push(rerunConfirm("run"));
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
  renderLibChoose();
  if (hadFocus && !busy) topic.focus();
}

// "지인 기법" chooser: only when the shared library is connected. The choice is sent as library_ids with the next run.
function renderLibChoose(focusId) {
  const box = S.nodes.lib;
  if (!box) return;
  const lib = S.lib;
  if (!lib) { fill(box); return; }
  const busy = S.running || isActive(S.job) || S.saving;
  if (!lib.items.length) {
    fill(box, h("p", { class: "muted small" }, "지인 기법: 받은 기법이 아직 없습니다. ", h("a", { href: "#/library" }, "공용 라이브러리에서 받기")));
    return;
  }
  const n = lib.selected.size;
  const rows = lib.items.map((it) => {
    const checked = lib.selected.has(it.id);
    const cb = h("input", {
      type: "checkbox", checked, disabled: busy || (!checked && n >= MAX_LIB), "data-lib-id": it.id,
      "aria-label": `${it.name} · ${it.author}`,
    });
    cb.addEventListener("change", () => {
      if (cb.checked) lib.selected.add(it.id); else lib.selected.delete(it.id);
      writeLibIds([...lib.selected]);
      renderLibChoose(it.id);
    });
    const kind = it.technique_kind ? `[${it.technique_kind}] ` : "";
    return h("label", { class: "check lib-choice" }, cb,
      h("span", {}, `${kind}${it.name}`, " ", h("span", { class: "muted small" }, `${it.author}${it.category ? ` · ${it.category}` : ""}`)));
  });
  fill(box, h("fieldset", { class: "field lib-choose-set" },
    h("legend", {}, `지인 기법(선택) ${n ? `· ${n}개 고름` : ""}`.trim()),
    h("p", { class: "muted hint" }, `공용 라이브러리에서 받은 지인 기법을 재료로 더합니다(최대 ${MAX_LIB}개). 카드의 근거는 여전히 내 레퍼런스 영상입니다.`),
    h("div", { class: "lib-choose-list" }, ...rows)));
  if (focusId) {
    const el = box.querySelector(`[data-lib-id="${CSS.escape(focusId)}"]`);
    if (el) el.focus({ preventScroll: true });
  }
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
  const star = h("input", { type: "checkbox", checked: e.star, "aria-label": `${n}번 카드 저장하면서 ★` });
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
  rerunBtn.addEventListener("click", () => requestRerun("preview"));
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
  const sig = JSON.stringify([S.saveError, S.confirmDiscard, S.saving, S.confirmRerun === "preview"]);
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
  if (S.confirmRerun === "preview") kids.push(rerunConfirm("preview"));
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
  const make = h("a", { class: "btn", href: `#/draft/new/${encodeURIComponent(card.id)}` }, "기획안 만들기");
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
    h("div", { class: "btn-row" }, make));
}

function filterButtons(types) {
  const defs = [["all", "전체"]];
  if (S.cards.some((c) => c.starred || S.pinned.has(c.id))) defs.push(["star", "★"]);
  for (const t of FILTER_ORDER) if (types.has(t)) defs.push([t, typeName(t)]);
  if (!defs.some(([k]) => k === S.filter)) S.filter = "all";
  const row = h("div", { class: "filter-row", role: "group", "aria-label": "카드 종류 거르기" });
  for (const [key, label] of defs) {
    const b = h("button", { type: "button", class: "btn filter-btn", "aria-pressed": String(S.filter === key) }, label);
    if (key === "star") b.setAttribute("aria-label", "즐겨찾기만");
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
  S.nodes.listAlert = h("div", { class: "list-alert" });
  kids.push(S.nodes.listAlert);
  if (!S.cards.length) {
    kids.push(h("p", { class: "muted" }, "아직 저장한 카드가 없습니다. 새 카드를 뽑아 보세요"));
  } else {
    kids.push(filterButtons(types));
    const shown = S.cards.filter((c) => S.filter === "all" || S.pinned.has(c.id)
      || (S.filter === "star" ? c.starred : c.type === S.filter));
    kids.push(h("div", { class: "motif-grid" }, ...shown.map(savedCard)));
  }
  fill(box, ...kids);
  const notes = [];
  if (S.saveWarning) {
    // Announced as an alert once; later re-renders (star / filter clicks) stay quiet.
    notes.push(h("p", { class: "warn-text", role: S.warnShown ? "status" : "alert" }, S.saveWarning));
    S.warnShown = true;
  }
  if (S.listError) notes.push(h("p", { class: "error", role: "alert" }, S.listError));
  if (notes.length) fill(S.nodes.listAlert, ...notes);
  if (focusId) {
    const btn = box.querySelector(`[data-star-id="${CSS.escape(focusId)}"]`);
    if (btn) {
      btn.focus({ preventScroll: true });
    } else {
      // The card left the current filter (un-starred under ★): keep focus inside the list.
      const heading = box.querySelector("#motif-list-title");
      if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
    }
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
