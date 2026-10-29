// 모티브 화면
//   위: 제목·한 줄, 주제 입력 + "새 카드 뽑기"(작업: 자료 모으기 → Claude) → 미리보기(편집·선택) → 저장.
//   아래: 필터 칩 + 카드 그리드(훑기용: 종류·★·제목·한 줄·근거 썸네일). 카드를 누르면 상세가
//   데스크톱은 오른쪽 패널, 폰은 바텀시트(모달)로 열리고, "이 카드로 새 영상 시작"이 기획안 만들기로 이어진다.
//   미리보기 단계에서는 index.md가 바뀌지 않는다. 저장은 사용자가 누른 뒤에만, 고른 카드만 보낸다.
import { h, fill, filesUrl } from "/static/dom.js";
import { chip, tag } from "/static/ui.js";
import {
  FILTERS, evidenceItems, filterCards, kindCounts, lockedReason, madeFrom, oneLine, tagKind,
} from "/static/motif-utils.js";
import { videoHref, videoId } from "/static/video-utils.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.motif.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const MAX_TOPIC = 100;
const TYPE_KO = { flow: "흐름", combo: "조합", technique: "기법", angle: "각도" };
const STEP_TEXT = { context: "자료 모으는 중", claude: "Claude가 카드 만드는 중" };
const LIB_IDS_KEY = "ytc.motif.library_ids";      // set by the library screen ("모티브 재료로 쓰기"); chosen per run, not saved
const MAX_LIB = 10;
const CATALOG_HINT = "기법 모음 정리(같은 기법 합치기)는 Claude에게 “기법 모음 정리해줘”라고 말하면 돼요";
const THUMBS = 3;                                  // evidence thumbnails on a card
const DETAIL_ID = "motif-detail";
const SHEET = "(max-width: 900px)";               // below this the 380px panel no longer fits beside the grid

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
    refs: null, drafts: null, selected: null, returnFocus: null,
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

// Moves focus to the page title after a run/save/discard; never behind an open bottom sheet.
function focusStatus() {
  if (S && (sheetOpen() || inDetail())) return;
  const t = S && S.nodes.status;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  const cat = mine.category;
  if (!cat) {
    fill(mine.el, h("h1", { class: "motif-h1" }, "모티브"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", { class: "motif-h1" }, "모티브"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
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
    loadExtras(mine);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e);
  }
}

// Reference frames for the evidence thumbnails (the motif API has none) and the videos made from each card.
// Both are extras: when they fail, cards show neutral placeholders and no "이 카드로 만든 영상" line.
async function loadExtras(mine) {
  const cat = encodeURIComponent(mine.category);
  const [refs, drafts] = await Promise.all([
    mine.ctx.api("GET", `/api/refs?category=${cat}`).then((r) => r.videos || [], () => null),
    mine.ctx.api("GET", `/api/draft/list?category=${cat}`).then((r) => r.drafts || [], () => null),
  ]);
  if (!alive(mine)) return;
  mine.refs = refs;
  mine.drafts = drafts;
  const inDetail = !!mine.nodes.detail && mine.nodes.detail.contains(document.activeElement);
  renderList();
  renderDetail();
  if (inDetail) {
    const title = mine.nodes.detail.querySelector("#motif-detail-title");
    if (title) title.focus({ preventScroll: true });
  }
}

function loadError(e) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (S && S.ctx.isCurrent()) load(); });
  fill(S.el, h("h1", { class: "motif-h1" }, "모티브"),
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
    const box = from === "preview" ? S.nodes.alerts : S.nodes.progress;
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
    const back = from === "preview" ? S.nodes.rerunBtn : S.nodes.run.querySelector(".motif-run-btn");
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
  renderDetail();
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
    if (alive(mine)) { renderList(card.id); renderDetail(); }
  }
}

// ---- render -------------------------------------------------------------------

function leadText(d) {
  const n = S.cards.length;
  if (!d.ref) return "레퍼런스를 넣으면 거기서 영상 아이디어 카드를 뽑아 드려요";
  if (!n) return `레퍼런스 ${d.ref}개에서 영상 아이디어 카드를 뽑아 보세요`;
  return `레퍼런스 ${d.ref}개에서 뽑은 아이디어 ${n}장 · 마음에 들면 ★, 바로 영상으로 시작`;
}

function render() {
  const d = S.data;
  const hadDetailFocus = inDetail();
  // The detail panel / sheet survives a full render (a run or save finishing): same node, same key handler.
  S.nodes = { detail: S.nodes.detail, scrim: S.nodes.scrim };
  S.runSig = null;
  S.alertSig = null;
  S.nodes.status = h("h1", { id: "motif-title", class: "motif-h1" }, "모티브");
  S.nodes.lead = h("p", { class: "motif-lead" }, leadText(d));
  const head = h("div", { class: "motif-head" }, h("div", { class: "motif-head-text" }, S.nodes.status, S.nodes.lead));
  // Phase 6: the header status chip takes over this line (same job, kind "motif"); until then it sits under the title.
  S.nodes.progress = h("div", { class: "motif-progress", "data-job-kind": "motif" });
  S.nodes.lib = h("div", { class: "lib-choose" });
  if (d.ref > 0) {            // without ref there is nothing to run: no empty form
    S.nodes.run = h("section", { class: "motif-runbar", "aria-label": "새 카드 뽑기" });
    head.append(S.nodes.run);
  }
  const kids = [head, S.nodes.progress];
  if (d.ref > 0) kids.push(S.nodes.lib);

  const notes = [];
  if (d.ref === 0) {
    notes.push(h("span", {}, "먼저 레퍼런스를 넣어 주세요 ", h("a", { href: "#/add" }, "레퍼런스 넣기")));
  }
  if (!d.has_profile) notes.push("프로필이 없어 카드가 일반적으로 나옵니다 — Claude에게 “처음 설정해줘”라고 말하면 만들 수 있어요");
  if (d.stage === 3 && !d.has_typify) {
    notes.push(h("span", {}, "유형화를 먼저 하면 흐름·조합 카드가 나옵니다 ", h("a", { href: "#/typify" }, "유형화 하러 가기")));
  }
  if (d.ref > 0 && (d.allowed_ko || []).length) notes.push(`지금 뽑을 수 있는 카드: ${d.allowed_ko.join("·")}`);
  for (const n of notes) kids.push(h("p", { class: "muted hint" }, n));

  S.nodes.body = h("div", { class: "motif-body" });
  S.nodes.list = h("section", { class: "motif-sec", "aria-labelledby": "motif-list-title" });
  kids.push(S.nodes.body, S.nodes.list, h("p", { class: "muted hint motif-foot" }, CATALOG_HINT));
  fill(S.el, ...kids);
  renderRunBox();
  renderBody();
  renderList();
  renderDetail();
  if (hadDetailFocus) focusDetailTitle();
}

// Compact progress line (no banner): the page stays usable while Claude works.
function renderProgress(active, failed, item0) {
  const box = S.nodes.progress;
  if (!box) return;
  const job = S.job;
  const kids = [];
  if (active) {
    const step = (item0 && STEP_TEXT[item0.step]) || "준비 중";
    kids.push(h("div", { class: "motif-progress-line", role: "status" },
      h("progress", { class: "motif-progress-bar", "aria-label": "카드 뽑기 진행" }),
      h("span", {}, `${step}… 다른 카드를 보며 기다려도 돼요`)));
  }
  if (failed) {
    kids.push(h("p", { class: "error", role: "alert" },
      `${(item0 && item0.message) || job.message || "카드를 뽑지 못했습니다"} 저장된 카드는 그대로입니다.`));
  }
  if (S.runError) kids.push(h("p", { class: "error", role: "alert" }, S.runError));
  if (S.confirmRerun === "run") kids.push(rerunConfirm("run"));
  fill(box, ...kids);
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
    id: "motif-topic", type: "text", maxlength: String(MAX_TOPIC), placeholder: "예: 비 오는 날 실내 여행", disabled: busy,
  });
  topic.value = S.topic;
  topic.addEventListener("input", () => { S.topic = topic.value; });
  topic.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) requestRerun("run"); });
  const btn = h("button", { type: "button", class: "btn btn-primary motif-run-btn", disabled: busy }, "새 카드 뽑기 · 약 30초");
  btn.addEventListener("click", () => requestRerun("run"));
  // Keep a half-typed topic and focus when only the status changed.
  const hadFocus = document.activeElement && document.activeElement.id === "motif-topic";
  fill(box, h("div", { class: "field motif-topic" }, h("label", { for: "motif-topic" }, "새 카드 주제 (선택)"), topic), btn);
  renderProgress(active, failed, item0);
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
    fill(box, h("p", { class: "muted small" }, "지인 기법: 받은 기법이 아직 없습니다. ", h("a", { href: "#/refs?tab=library" }, "공용 라이브러리에서 받기")));
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
  // Folded by default (keeps the header short); open when friend techniques are already chosen.
  if (S.libOpen === undefined) S.libOpen = n > 0;
  const fold = h("details", { class: "motif-lib", open: S.libOpen },
    h("summary", {}, `지인 기법 재료 더하기 ${n ? `· ${n}개 고름` : "(선택)"}`),
    h("fieldset", { class: "field lib-choose-set" },
      h("legend", { class: "sr-only" }, `지인 기법(선택) ${n ? `· ${n}개 고름` : ""}`.trim()),
      h("p", { class: "muted hint" }, `공용 라이브러리에서 받은 지인 기법을 재료로 더합니다(최대 ${MAX_LIB}개). 카드의 근거는 여전히 내 레퍼런스 영상입니다.`),
      h("div", { class: "lib-choose-list" }, ...rows)));
  fold.addEventListener("toggle", () => { S.libOpen = fold.open; });
  fill(box, fold);
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

const cardById = (id) => S.cards.find((c) => c.id === id) || null;
const kindLabel = (card) => card.type_ko || typeName(card.type);

function thumbBox(item, cls) {
  return item && item.thumb
    ? h("img", { class: cls, src: filesUrl(item.thumb), alt: "", loading: "lazy" })
    : h("span", { class: `${cls} is-empty`, "aria-hidden": "true" });
}

// Scan-friendly card: kind tag, star toggle, title (opens the detail), one line, evidence thumbnails.
function savedCard(card) {
  const titleId = `motif-card-${card.id}`;
  const open = S.selected === card.id;
  const star = h("button", {
    type: "button", class: "btn btn-ghost star-btn", "aria-pressed": String(!!card.starred),
    "aria-label": `${card.title} 즐겨찾기`, "data-star-id": card.id,
  }, card.starred ? "★" : "☆");
  star.addEventListener("click", () => toggleStar(card));
  const opener = h("button", {
    type: "button", class: "motif-open", "data-open-id": card.id,
    "aria-expanded": String(open), "aria-controls": DETAIL_ID,
  }, card.title);
  opener.addEventListener("click", () => openDetail(card.id));
  const ev = evidenceItems(card, S.refs);
  const one = oneLine(card.idea);
  return h("article", { class: `motif-tile${open ? " is-selected" : ""}`, "aria-labelledby": titleId },
    h("div", { class: "motif-tile-head" }, tag(kindLabel(card), tagKind(card.type)), star),
    h("h3", { id: titleId, class: "motif-tile-title" }, opener),
    one ? h("p", { class: "motif-one" }, one) : null,
    ev.length ? h("div", { class: "motif-ev" },
      ...ev.slice(0, THUMBS).map((it) => thumbBox(it, "motif-thumb")),
      h("span", { class: "motif-ev-n" }, `근거 영상 ${ev.length}`)) : null);
}

// Empty filter: say why when this category cannot draw the kind yet (stage / typify), else just "none yet".
function emptyFilter() {
  const why = lockedReason(S.filter, S.data);
  if (!why) return h("p", { class: "muted motif-empty" }, "이 종류의 카드가 아직 없어요");
  const typify = why.includes("유형화");
  return h("p", { class: "muted motif-empty", role: "note" }, why,
    typify ? h("span", {}, " · ", h("a", { href: "#/typify" }, "유형화 하러 가기")) : null);
}

function filterChips() {
  const counts = kindCounts(S.cards);
  if (!FILTERS.some(([k]) => k === S.filter)) S.filter = "all";
  const row = h("div", { class: "motif-filters", role: "group", "aria-label": "카드 종류 거르기" });
  for (const [key, label] of FILTERS) {
    const b = chip(label, {
      pressed: S.filter === key, count: counts[key],
      onClick: () => { S.filter = key; renderList(null, key); },
    });
    b.setAttribute("data-filter", key);
    if (key === "star") b.setAttribute("aria-label", `즐겨찾기만 ${counts.star}`);
    const why = counts[key] ? "" : lockedReason(key, S.data);
    if (why) { b.setAttribute("title", why); b.classList.add("is-locked"); }
    row.append(b);
  }
  return row;
}

// Rebuilds the saved-card list. `focusId` keeps keyboard focus on that card's star button; `focusFilter` on a chip.
function renderList(focusId, focusFilter) {
  const box = S.nodes.list;
  if (!box) return;
  // A redraw the user did not ask for (extras arriving, star settling) keeps focus on the same card control.
  const at = document.activeElement;
  const keepOpen = !focusId && !focusFilter && at && box.contains(at) && at.getAttribute("data-open-id");
  const keepStar = !focusId && !focusFilter && at && box.contains(at) && at.getAttribute("data-star-id");
  const keepChip = !focusId && !focusFilter && at && box.contains(at) && at.getAttribute("data-filter");
  if (keepStar) focusId = keepStar;
  if (keepChip) focusFilter = keepChip;
  const kids = [h("h2", { id: "motif-list-title", class: "sr-only" }, `저장한 카드 (${S.cards.length}장)`)];
  S.nodes.listAlert = h("div", { class: "list-alert" });
  kids.push(S.nodes.listAlert);
  if (!S.cards.length) {
    kids.push(h("p", { class: "muted motif-empty" }, S.data.ref
      ? "아직 저장한 카드가 없습니다. 위에서 새 카드를 뽑아 보세요"
      : "레퍼런스를 넣은 뒤 카드를 뽑을 수 있어요"));
  } else {
    kids.push(filterChips());
    const shown = filterCards(S.cards, S.filter, S.pinned);
    const grid = shown.length ? h("div", { class: "motif-cards" }, ...shown.map(savedCard)) : emptyFilter();
    kids.push(h("div", { class: "motif-layout" }, grid, detailSlot()));
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
  if (keepOpen) {
    const b = box.querySelector(`[data-open-id="${CSS.escape(keepOpen)}"]`);
    if (b) b.focus({ preventScroll: true });
  }
  // The detail node is re-attached, not rebuilt here: focus inside it comes back to the same element.
  if (at && S.nodes.detail && S.nodes.detail.contains(at) && document.activeElement !== at && at.isConnected) {
    at.focus({ preventScroll: true });
  }
  if (focusFilter) {
    const b = box.querySelector(`[data-filter="${CSS.escape(focusFilter)}"]`);
    if (b) b.focus({ preventScroll: true });
  }
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

// ---- detail: right panel (wide) / bottom sheet (below 900px) ---------------------------------

// The detail node lives across list re-renders (star / filter clicks), so an open sheet keeps its focus.
function detailSlot() {
  if (!S.nodes.detail) {
    S.nodes.detail = h("aside", { id: DETAIL_ID, class: "motif-detail", tabindex: "-1" });
    S.nodes.detail.addEventListener("keydown", onDetailKey);
    S.nodes.scrim = h("div", { class: "motif-scrim", hidden: true });
    S.nodes.scrim.addEventListener("click", () => closeDetail());
  }
  return h("div", { class: "motif-detail-wrap" }, S.nodes.scrim, S.nodes.detail);
}

const isSheet = () => !!(S && S.mq && S.mq.matches);
const sheetOpen = () => isSheet() && !!S.selected && !!cardById(S.selected);
const inDetail = () => !!(S && S.nodes.detail && S.nodes.detail.contains(document.activeElement));

function focusDetailTitle() {
  const title = S.nodes.detail && S.nodes.detail.querySelector("#motif-detail-title");
  if (title) title.focus({ preventScroll: !isSheet() });
}

// The selected card's border and every title button's aria-expanded, without rebuilding the grid.
function markSelected() {
  const box = S.nodes.list;
  if (!box) return;
  for (const tile of box.querySelectorAll(".motif-tile")) {
    const btn = tile.querySelector("[data-open-id]");
    const on = !!btn && btn.getAttribute("data-open-id") === S.selected;
    tile.classList.toggle("is-selected", on);
    if (btn) btn.setAttribute("aria-expanded", String(on));
  }
}

function openDetail(id) {
  S.selected = id;
  S.returnFocus = id;
  markSelected();
  renderDetail();
  focusDetailTitle();
  if (!isSheet() && S.nodes.detail) S.nodes.detail.scrollIntoView({ block: "nearest" });
}

function closeDetail() {
  const back = S.returnFocus;
  S.selected = null;
  S.returnFocus = null;
  markSelected();
  renderDetail();
  const btn = back && S.nodes.list.querySelector(`[data-open-id="${CSS.escape(back)}"]`);
  if (btn) btn.focus();
  else {
    const heading = S.nodes.list.querySelector("#motif-list-title");
    if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus(); }
  }
}

function focusables(root) {
  return [...root.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), [tabindex='0']")]
    .filter((el) => el.offsetParent !== null || el === document.activeElement);
}

// Esc closes (both widths); on phones Tab stays inside the sheet (modal).
function onDetailKey(e) {
  if (!S || !S.selected) return;
  if (e.key === "Escape") { e.preventDefault(); closeDetail(); return; }
  if (e.key !== "Tab" || !sheetOpen()) return;
  const list = focusables(S.nodes.detail);
  if (!list.length) { e.preventDefault(); return; }
  const first = list[0];
  const last = list[list.length - 1];
  const at = document.activeElement;
  if (e.shiftKey && (at === first || !list.includes(at))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
}

// Sheet attributes follow the width: a modal dialog below 900px, a labelled side panel on wider screens.
function applyDetailMode() {
  const panel = S.nodes.detail;
  if (!panel) return;
  const card = S.selected ? cardById(S.selected) : null;
  const modal = isSheet() && !!card;
  panel.classList.toggle("is-open", !!card);
  if (modal) {
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
  } else {
    panel.removeAttribute("aria-modal");
    panel.setAttribute("role", "region");
  }
  if (card) panel.setAttribute("aria-labelledby", "motif-detail-title");
  else { panel.removeAttribute("aria-labelledby"); panel.setAttribute("aria-label", "선택한 카드"); }
  if (card) panel.removeAttribute("aria-label");
  S.nodes.scrim.hidden = !modal;
  document.body.classList.toggle("motif-sheet-open", modal);
}

function renderDetail() {
  const panel = S && S.nodes.detail;
  if (!panel) return;
  const card = S.selected ? cardById(S.selected) : null;
  if (!card) {
    fill(panel, h("p", { class: "muted motif-detail-empty" }, "카드를 누르면 여기에 자세히 보여요"));
    applyDetailMode();
    return;
  }
  const close = h("button", { type: "button", class: "btn btn-ghost btn-s motif-detail-close" }, "닫기");
  close.addEventListener("click", () => closeDetail());
  const ev = evidenceItems(card, S.refs);
  const made = S.drafts ? madeFrom(S.drafts, card.id) : [];
  const kids = [
    h("div", { class: "motif-detail-head" },
      tag(kindLabel(card), tagKind(card.type)),
      h("span", { class: "motif-detail-id" }, `${card.id}${card.starred ? " · ★" : ""}`),
      h("span", { class: "motif-gap" }), close),
    h("h2", { id: "motif-detail-title", class: "motif-detail-title", tabindex: "-1" }, card.title),
    card.idea ? h("p", { class: "motif-detail-idea" }, card.idea) : null,
  ];
  if (card.how && card.how.length) {
    kids.push(h("div", { class: "motif-detail-sec" },
      h("h3", {}, "이렇게 찍어 보세요"),
      h("ol", { class: "motif-how" }, ...card.how.map((s) => h("li", {}, s)))));
  }
  if (card.basis) kids.push(h("p", { class: "motif-detail-basis" }, h("span", { class: "muted" }, "바탕 · "), card.basis));
  if (ev.length) {
    kids.push(h("div", { class: "motif-detail-sec" },
      h("h3", {}, "근거 영상"),
      h("ul", { class: "motif-ev-list" }, ...ev.map((it) => h("li", {},
        h("a", { href: refHref(it.id) }, thumbBox(it, "motif-ev-thumb"), h("span", {}, it.title || "제목 없는 영상")))))));
  }
  if (made.length) {
    kids.push(h("p", { class: "motif-made" }, "이 카드로 만든 영상 · ",
      ...made.flatMap((m, i) => [i ? ", " : null, h("a", { href: videoHref(videoId(m.path)) }, m.title)]).filter(Boolean)));
  }
  if (card.made) kids.push(h("p", { class: "muted hint" }, `만든 날 ${card.made}`));
  // The existing draft flow (#/draft/new/<card>): pick length → Claude → preview → save, which lands on the video.
  kids.push(h("a", { class: "btn btn-rec motif-start", href: `#/draft/new/${encodeURIComponent(card.id)}` }, "이 카드로 새 영상 시작"));
  fill(panel, ...kids);
  applyDetailMode();
}

// Crossing the breakpoint with a card open: entering sheet mode moves focus into the (now modal) sheet.
function onMedia() {
  if (!S) return;
  applyDetailMode();
  if (sheetOpen() && !inDetail()) focusDetailTitle();
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  dropMedia();
  S = freshState(el, ctx, ctx.category);
  watchMedia();
  await load();
}

function watchMedia() {
  if (typeof matchMedia !== "function") return;
  S.mq = matchMedia(SHEET);
  S.mq.addEventListener("change", onMedia);
}

function dropMedia() {
  if (S && S.mq) S.mq.removeEventListener("change", onMedia);
  document.body.classList.remove("motif-sheet-open");
}

export function onCategory(category) {
  if (!S || S.category === category) return;
  stopPolling();
  dropMedia();
  S = freshState(S.el, S.ctx, category);
  watchMedia();
  load();
}

// The router calls this before it shows the leave question: an open sheet (and its scrim) would cover the
// question, so it closes first and focus goes back to the card; "계속 편집" then returns focus there.
export function onStay() {
  if (sheetOpen()) closeDetail();
}

// Unsaved preview edits (titles, ideas, which cards to keep) ask before leaving the page.
export function unsavedNote() {
  return S && S.preview && hasEdits() ? "저장하지 않은 카드 미리보기" : null;
}

export function unmount() {
  stopPolling();
  dropMedia();
  S = null;
}
