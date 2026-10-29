// 유형화 화면
//   상태(ref 수·단계·저장된 결과) → "유형화 실행"(작업: 계산 → Claude 해석) → 미리보기(편집) → 저장.
//   미리보기 단계에서는 파일이 바뀌지 않는다. 저장은 사용자가 누른 뒤에만, 미리보기의 run_at과 함께 보낸다.
import { h, fill } from "/static/dom.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.typify.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const AXIS_ORDER = ["structure", "hook", "narrative", "title", "pacing", "shots"];
const AXIS_NAMES = {
  structure: "구조", hook: "훅", narrative: "서사", title: "제목", pacing: "컷 속도", shots: "샷",
};
const VERDICT_TEXT = {
  typable: "유형 있음", weak: "약한 경향", untypable: "유형 없음", insufficient: "데이터 부족",
};
const STEP_TEXT = { cluster: "계산 중", claude: "Claude 해석 중" };
const CHANGED_TEXT = "미리보기를 만든 뒤 유형화가 다시 실행되었습니다. 다시 실행해 주세요. 고친 근거·이름은 같은 축·아키타입이 있으면 다시 실행한 미리보기에도 유지됩니다.";

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
// Labels may carry a qualifier ("body/story"); colors use the base label.
const baseLabel = (label) => String(label).split("/")[0].trim();
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const refHref = (id) => `#/refs/${encodeURIComponent(id)}`;

function fmtDuration(sec) {
  const n = num(sec);
  if (n === null) return null;
  const total = Math.round(n);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A preview that was saved (or discarded) in this browser is not restored after a reload.
function markHandled(jobId) {
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

const isHandled = (jobId) => readHandled().includes(jobId);

function previewOf(job) {
  const item = job && job.items && job.items[0];
  return item && item.state === "awaiting" && item.preview && item.preview.result ? item.preview : null;
}

// ---- page state ---------------------------------------------------------------

let S = null;

// Each category change builds a fresh state, so late async work of the old one sees S !== mine and stops.
function freshState(el, ctx, category) {
  return {
    el, ctx, category, data: null, job: null, preview: null, jobId: null,
    timer: null, pollErrors: 0, running: false, saving: false,
    runError: null, changed: false, saveError: null, confirmRerun: false, confirmDiscard: false,
    notes: { axes: {}, names: {} }, orig: { axes: {}, names: {} }, carry: null,
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

// ---- load ---------------------------------------------------------------------

async function load() {
  const mine = S;
  const cat = mine.category;
  if (!cat) {
    fill(mine.el, h("h1", {}, "유형화"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", {}, "유형화"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const [data, cur] = await Promise.all([
      mine.ctx.api("GET", `/api/typify?category=${encodeURIComponent(cat)}`),
      mine.ctx.api("GET", "/api/jobs/current?kind=typify"),
    ]);
    if (!alive(mine)) return;
    mine.data = data;
    const job = cur.job;
    // The latest typify job may belong to another category.
    // A running job or an open preview is restored; a failure that happened meanwhile is shown once.
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
  fill(S.el, h("h1", {}, "유형화"),
    h("p", { class: "error", role: "alert" }, e.message || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

// Take a job snapshot into the state; a fresh awaiting preview starts a new set of editable notes.
function adoptJob(mine, job) {
  mine.job = job;
  const preview = job.status === "done" ? previewOf(job) : null;
  if (preview && (!mine.preview || mine.jobId !== job.id)) {
    const applied = mine.data && mine.data.applied;
    // A preview whose run was already saved is not an open preview.
    if (applied && applied.run_at === preview.result.run_at) {
      mine.preview = null;
      return;
    }
    mine.preview = preview;
    mine.jobId = job.id;
    initNotes(mine, preview);
  }
}

function initNotes(mine, preview) {
  const given = preview.notes || {};
  const axes = {};
  for (const [axis, text] of Object.entries(given.axes || {})) axes[axis] = String(text || "");
  const names = {};
  for (const a of given.archetypes || []) if (a && a.id) names[a.id] = String(a.name || "");
  mine.orig = { axes: { ...axes }, names: { ...names } };
  // Edits made before "다시 실행" survive for axes / archetype ids that still exist.
  const carry = mine.carry;
  mine.carry = null;
  if (carry) {
    // A rationale is reused only when the axis verdict is unchanged.
    for (const axis of Object.keys(axes)) {
      const old = carry.axes[axis];
      const res = preview.result.axes[axis];
      if (old && res && res.verdict === old.verdict) axes[axis] = old.text;
    }
    // A name follows its group, not its position: best member overlap (Jaccard >= 0.5), each old name used once.
    const pairs = [];
    for (const a of preview.result.archetypes) {
      for (const [oldId, old] of Object.entries(carry.names)) {
        const score = jaccard(a.members || [], old.members);
        if (score >= 0.5) pairs.push({ id: a.id, oldId, score, text: old.text });
      }
    }
    pairs.sort((x, y) => y.score - x.score);
    const usedNew = new Set();
    const usedOld = new Set();
    for (const m of pairs) {
      if (usedNew.has(m.id) || usedOld.has(m.oldId)) continue;
      usedNew.add(m.id);
      usedOld.add(m.oldId);
      names[m.id] = m.text;
    }
  }
  mine.notes = { axes, names };
  mine.confirmRerun = false;
  mine.confirmDiscard = false;
  mine.runError = null;
  mine.changed = false;
  mine.saveError = null;
}

// ---- run + polling ------------------------------------------------------------

function jaccard(a, b) {
  const x = new Set(a);
  const y = new Set(b);
  let both = 0;
  for (const v of x) if (y.has(v)) both += 1;
  const union = x.size + y.size - both;
  return union ? both / union : 0;
}

// Non-empty notes the user changed compared with what Claude proposed, with what they belong to
// (axis verdict, archetype members) so they are reused only for the same thing.
function editedNotes() {
  const out = { axes: {}, names: {} };
  const result = S.preview.result;
  for (const [k, v] of Object.entries(S.notes.axes)) {
    if (v.trim() && v !== S.orig.axes[k] && result.axes[k]) out.axes[k] = { text: v, verdict: result.axes[k].verdict };
  }
  for (const a of result.archetypes) {
    const v = S.notes.names[a.id] || "";
    if (v.trim() && v !== S.orig.names[a.id]) out.names[a.id] = { text: v, members: a.members || [] };
  }
  return out;
}

const hasEdits = () => {
  if (!S.preview) return false;
  const e = editedNotes();
  return Object.keys(e.axes).length + Object.keys(e.names).length > 0;
};

// "다시 실행" button of the run box: with unsaved edits in an open preview it asks inline first.
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
  if (mine.running || isActive(mine.job)) return;
  mine.running = true;
  mine.runError = null;
  mine.changed = false;
  mine.confirmRerun = false;
  mine.carry = mine.preview ? editedNotes() : mine.carry;
  mine.alertSig = null;
  renderRunBox();
  renderAlerts();
  try {
    const res = await mine.ctx.api("POST", "/api/typify/run", { category: mine.category });
    if (!alive(mine)) return;
    mine.job = res.job;
    mine.preview = null;
    mine.jobId = null;
    mine.pollErrors = 0;
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.runError = e.message || "유형화를 시작하지 못했습니다";
    mine.carry = null;
  } finally {
    if (alive(mine)) { mine.running = false; render(); focusStatus(); }
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
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 유형화를 다시 실행하세요", "error");
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

function focusStatus() {
  const t = S && S.nodes.status;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

// ---- save / discard -----------------------------------------------------------

function currentNotes() {
  const p = S.preview;
  const axes = {};
  for (const [axis, res] of Object.entries(p.result.axes)) {
    if (res.verdict !== "insufficient") axes[axis] = (S.notes.axes[axis] || "").trim();
  }
  return {
    axes,
    archetypes: p.result.archetypes.map((a) => ({ id: a.id, name: (S.notes.names[a.id] || "").trim() })),
  };
}

function missingNames() {
  return S.preview.result.archetypes.filter((a) => !(S.notes.names[a.id] || "").trim());
}

async function save() {
  const mine = S;
  if (mine.saving || !mine.preview || missingNames().length) return;
  mine.saving = true;
  mine.saveError = null;
  mine.changed = false;
  updateActions();
  renderAlerts();
  try {
    await mine.ctx.api("POST", "/api/typify/save", {
      category: mine.category, run_at: mine.preview.result.run_at, notes: currentNotes(),
    });
    if (!alive(mine)) return;
    markHandled(mine.jobId);
    mine.ctx.toast("유형화 결과를 저장했습니다", "info");
    mine.preview = null;
    mine.job = null;
    mine.saving = false;
    await reloadStatus(mine);
    if (!alive(mine)) return;
    if (typeof mine.ctx.refreshCategories === "function") await mine.ctx.refreshCategories();
    if (!alive(mine)) return;
    render();
    focusStatus();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    if (e.error === "typify_changed") mine.changed = true;
    else if (e.error === "invalid_notes") mine.saveError = `근거 또는 이름이 올바르지 않아 저장하지 않았습니다. ${e.message || ""}`.trim();
    else mine.saveError = e.message || "저장하지 못했습니다";
    mine.saving = false;
    updateActions();
    renderAlerts();
  }
}

async function reloadStatus(mine) {
  try {
    mine.data = await mine.ctx.api("GET", `/api/typify?category=${encodeURIComponent(mine.category)}`);
  } catch (e) {
    if (alive(mine) && !ignorable(e)) mine.ctx.toast(e.message, "error");
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
  if (S.jobId) markHandled(S.jobId);
  S.preview = null;
  S.job = null;
  S.changed = false;
  S.saveError = null;
  S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  render();
  focusStatus();
}

// ---- render -------------------------------------------------------------------

function render() {
  const d = S.data;
  S.nodes = {};
  const stageText = d.stage === "formal" ? "정식 판정" : d.stage === "preliminary" ? "예비 관찰" : "판정 전";
  const last = d.typified_at ? `마지막 유형화 ${d.typified_at}` : "마지막 유형화 아직 없음";
  S.nodes.status = h("h1", { id: "typify-title" }, "유형화");
  const kids = [
    S.nodes.status,
    h("p", { class: "lead" }, `ref ${d.ref}개 · ${stageText} · ${last}`),
    h("p", { class: "muted hint" }, "내 영상(mine)은 기준에 섞지 않습니다 — 내 영상과 비교하면 진단이 자기 자신과 비교하게 되기 때문입니다"),
  ];
  if (d.stage === "too_few") {
    kids.push(h("section", { class: "card empty", "aria-label": "ref 부족" },
      h("p", {}, `ref가 ${d.ref}개라 아직 유형을 볼 수 없습니다. ${d.need}개 더 넣으면 예비 관찰을 할 수 있어요`),
      h("a", { class: "btn btn-primary", href: "#/add" }, "레퍼런스 넣기")));
    fill(S.el, ...kids);
    return;
  }
  S.nodes.run = h("div", { class: "job-box typify-run" });
  kids.push(S.nodes.run);
  S.nodes.body = h("div", { class: "typify-body" });
  kids.push(S.nodes.body);
  fill(S.el, ...kids);
  renderRunBox();
  renderBody();
}

// Run button, progress and errors. Re-rendered by polling without touching the (editable) preview below.
function renderRunBox() {
  const box = S.nodes.run;
  if (!box) return;
  const d = S.data;
  const job = S.job;
  const active = isActive(job);
  const failed = !!job && (job.status === "failed" || job.status === "stopped");
  const hasSaved = !!d.applied;
  const label = hasSaved || S.preview ? "다시 실행" : "유형화 실행";
  const item0 = job && job.items && job.items[0];
  const sig = JSON.stringify([label, active, failed, S.running, S.runError, S.confirmRerun, d.stage,
    item0 && item0.step, item0 && item0.message, job && job.message]);
  if (sig === S.runSig && box.childNodes.length) return;
  S.runSig = sig;
  const btn = h("button", { type: "button", class: "btn btn-primary", disabled: S.running || active }, label);
  btn.addEventListener("click", requestRerun);
  const kids = [h("div", { class: "btn-row" }, btn,
    h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)"))];
  if (S.confirmRerun) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-rerun-yes": "" }, "다시 실행");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", startRun);
    no.addEventListener("click", () => { S.confirmRerun = false; renderRunBox(); S.nodes.run.querySelector("button").focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "고친 내용은 같은 축·아키타입이 있으면 유지됩니다. 새 미리보기가 나오면 다시 확인하세요."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (d.stage === "preliminary") {
    kids.push(h("p", { class: "muted hint" }, "예비 관찰입니다. 10개부터 정식 판정이 나옵니다"));
  }
  if (active) {
    const item = job.items && job.items[0];
    const step = (item && STEP_TEXT[item.step]) || "준비 중";
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "유형화 진행" }),
      h("span", {}, `${step}…`)));
  }
  if (failed) {
    const item = job.items && job.items[0];
    kids.push(h("p", { class: "error", role: "alert" }, `${(item && item.message) || job.message || "유형화를 하지 못했습니다"} 저장된 결과는 그대로입니다.`));
  }
  if (S.runError) kids.push(h("p", { class: "error", role: "alert" }, S.runError));
  fill(box, ...kids);
}

function renderBody() {
  const body = S.nodes.body;
  if (!body) return;
  if (S.preview) {
    fill(body, ...previewView());
    updateActions();
    renderAlerts();
    return;
  }
  if (S.data.applied) { fill(body, ...appliedView(S.data.applied)); return; }
  if (isActive(S.job)) { fill(body); return; }
  const text = S.data.typified_at
    ? "이전에 저장한 결과가 있습니다 — 다시 실행하면 이 화면에서 볼 수 있어요"
    : "아직 유형화한 결과가 없습니다. 실행 버튼을 누르면 미리보기가 나옵니다.";
  fill(body, h("p", { class: "muted" }, text));
}

function axisRows(axes, editable) {
  const table = h("table", { class: "preview-table verdict-table" },
    h("caption", { class: "sr-only" }, "축별 판정"),
    h("thead", {}, h("tr", {}, ...["축", "판정", "영상/묶음", "근거"].map((t) => h("th", { scope: "col" }, t)))));
  const tbody = h("tbody");
  for (const axis of AXIS_ORDER) {
    const res = axes[axis];
    if (!res) continue;
    const name = AXIS_NAMES[axis];
    let reason;
    if (res.verdict === "insufficient") {
      reason = h("span", { class: "muted" }, "데이터 부족");
    } else if (editable) {
      const area = h("textarea", { rows: "2", "aria-label": `${name} 근거`, "data-axis": axis });
      area.value = S.notes.axes[axis] || "";
      area.addEventListener("input", () => { S.notes.axes[axis] = area.value; });
      reason = area;
    } else {
      reason = h("span", { class: "md-text" }, res.note || "");
    }
    const groups = res.verdict === "insufficient" ? `${res.n}개` : `${res.n}개 · ${res.k}묶음`;
    tbody.append(h("tr", {},
      h("th", { scope: "row" }, name),
      h("td", {}, h("span", { class: `badge verdict verdict-${res.verdict}` }, VERDICT_TEXT[res.verdict] || res.verdict)),
      h("td", { class: "nowrap num" }, groups),
      h("td", {}, reason)));
  }
  table.append(tbody);
  return h("div", { class: "table-wrap", role: "region", "aria-label": "축별 판정 표", tabindex: "0" }, table);
}

function skeletonChips(skeleton) {
  const labels = String(skeleton || "").split("→").map((s) => s.trim()).filter(Boolean);
  if (!labels.length) return null;
  const list = h("ol", { class: "skeleton", "aria-label": `골격: ${labels.join(" → ")}` });
  labels.forEach((label, i) => {
    list.append(h("li", { class: "skel-chip" },
      h("span", { class: `lb-dot lb-${baseLabel(label)}`, "aria-hidden": "true" }), label,
      i < labels.length - 1 ? h("span", { class: "skel-arrow", "aria-hidden": "true" }, "→") : null));
  });
  return list;
}

function ratioBand(ratio) {
  const entries = Object.entries(ratio || {}).filter(([, v]) => num(v) !== null && v > 0);
  if (!entries.length) return null;
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const text = entries.map(([k, v]) => `${k} ${Math.round((v / total) * 100)}%`).join(" · ");
  const band = h("div", { class: "sec-band", role: "img", "aria-label": `평균 비율: ${text}` });
  for (const [label, v] of entries) {
    const seg = h("span", { class: `lb-${baseLabel(label)}`, title: `${label} ${Math.round((v / total) * 100)}%` });
    seg.style.width = `${(v / total) * 100}%`;
    band.append(seg);
  }
  return band;
}

function refLink(id, titles) {
  return h("a", { href: refHref(id) }, titles.get(id) || id);
}

function archetypeCard(a, titles, editable) {
  const titleId = `arch-${a.id}`;
  const card = h("article", { class: "card archetype", "aria-labelledby": titleId });
  const mean = [];
  const dur = fmtDuration(a.mean_duration);
  if (dur) mean.push(`평균 길이 ${dur}`);
  const cut = num(a.pacing && a.pacing.avg_cut_sec);
  if (cut !== null) mean.push(`평균 컷 ${cut.toFixed(1)}초`);
  const head = h("div", { class: "card-head" }, h("h3", { id: titleId }, `${a.id} · ${a.size}개`));
  const nameValue = editable ? (S.notes.names[a.id] || "") : (a.name || "");
  if (a.prev_name && a.prev_name === nameValue) head.append(h("span", { class: "badge" }, "이전 이름 그대로"));
  card.append(head);

  if (editable) {
    const inputId = `arch-name-${a.id}`;
    const input = h("input", { id: inputId, type: "text", required: true, "aria-label": `${a.id} 아키타입 이름`, "data-arch": a.id });
    input.value = nameValue;
    input.addEventListener("input", () => {
      S.notes.names[a.id] = input.value;
      head.querySelector(".badge")?.remove();
      if (a.prev_name && a.prev_name === input.value) head.append(h("span", { class: "badge" }, "이전 이름 그대로"));
      updateActions();
    });
    card.append(h("div", { class: "field" }, h("label", { for: inputId }, `${a.id} 아키타입 이름`), input));
  } else {
    card.append(h("p", { class: "arch-name" }, nameValue));
  }
  const chips = skeletonChips(a.skeleton);
  const band = ratioBand(a.mean_ratio);
  if (chips) card.append(chips);
  if (band) card.append(band);
  if (mean.length) card.append(h("p", { class: "muted small" }, mean.join(" · ")));
  if (a.representative && a.representative.id) {
    card.append(h("p", { class: "small" }, "대표 영상 ", refLink(a.representative.id, titles)));
  }
  const members = a.members || [];
  if (members.length) {
    card.append(h("ul", { class: "member-list", "aria-label": `${a.id} 구성 영상` },
      ...members.map((id) => h("li", {}, refLink(id, titles)))));
  }
  return card;
}

function titleMap(result, known) {
  const titles = new Map();
  for (const a of result.archetypes || []) {
    if (a.representative && a.representative.id && a.representative.title) titles.set(a.representative.id, a.representative.title);
  }
  for (const o of result.outliers || []) if (o.id && o.title) titles.set(o.id, o.title);
  for (const [id, title] of Object.entries(known || {})) if (title) titles.set(id, title);
  return titles;
}

function outlierAxes(o) {
  const names = (o.axes || []).map((axis) => AXIS_NAMES[axis] || axis);
  return names.length ? names.join("·") : "일부 축";
}

function outlierList(outliers, titles) {
  if (!outliers || !outliers.length) return null;
  return h("section", { class: "typify-sec", "aria-labelledby": "typify-outliers" },
    h("h2", { id: "typify-outliers" }, "이상치"),
    h("ul", { class: "outlier-list" },
      ...outliers.map((o) => h("li", {}, refLink(o.id, titles), h("span", { class: "muted" }, ` — ${outlierAxes(o)}에서 혼자 떨어짐`)))));
}

function archetypeSection(list, titles, editable) {
  if (!list.length) {
    return h("section", { class: "typify-sec", "aria-labelledby": "typify-arch" },
      h("h2", { id: "typify-arch" }, "아키타입"),
      h("p", { class: "muted" }, "2개 이상 모인 구조 묶음이 없어 아키타입이 없습니다."));
  }
  return h("section", { class: "typify-sec", "aria-labelledby": "typify-arch" },
    h("h2", { id: "typify-arch" }, "아키타입"),
    h("div", { class: "archetype-grid" }, ...list.map((a) => archetypeCard(a, titles, editable))));
}

function appliedView(applied) {
  const titles = titleMap(applied, applied.titles);
  return [
    h("p", { class: "muted small" }, `저장된 결과 · ${String(applied.applied_at || "").slice(0, 10)} 저장`),
    h("section", { class: "typify-sec", "aria-labelledby": "typify-axes" },
      h("h2", { id: "typify-axes" }, "축별 판정"), axisRows(applied.axes || {}, false)),
    archetypeSection(applied.archetypes || [], titles, false),
    outlierList(applied.outliers, titles),
  ];
}

function previewView() {
  const result = S.preview.result;
  const titles = titleMap(result, S.preview.titles);
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "typify-save-hint" }, "저장");
  const discardBtn = h("button", { type: "button", class: "btn" }, "버리기");
  saveBtn.addEventListener("click", save);
  discardBtn.addEventListener("click", askDiscard);
  S.nodes.saveBtn = saveBtn;
  S.nodes.discardBtn = discardBtn;
  S.nodes.hint = h("p", { class: "muted hint", id: "typify-save-hint" });
  S.nodes.alerts = h("div", { class: "preview-alerts" });
  S.alertSig = null;
  return [
    h("p", { class: "muted small" }, "미리보기 · 저장하기 전에는 파일이 바뀌지 않습니다. 근거와 이름은 고칠 수 있습니다."),
    h("p", { class: "muted small" }, "다른 화면으로 가면 고친 내용은 사라집니다"),
    h("p", { class: "muted hint" }, "판정이 이상해 보이면 ",
      h("a", { href: "#/refs" }, "레퍼런스 목록"), "에서 영상 라벨을 고친 뒤 다시 실행하세요"),
    h("section", { class: "typify-sec", "aria-labelledby": "typify-axes" },
      h("h2", { id: "typify-axes" }, "축별 판정"), axisRows(result.axes, true)),
    archetypeSection(result.archetypes, titles, true),
    outlierList(result.outliers, titles),
    h("div", { class: "preview-actions" }, h("div", { class: "btn-row" }, saveBtn, discardBtn), S.nodes.hint, S.nodes.alerts),
  ];
}

// After the preview is in the DOM and while typing: only touches disabled state and the reason hint.
function updateActions() {
  if (!S.preview || !S.nodes.saveBtn) return;
  const missing = missingNames();
  S.nodes.saveBtn.disabled = S.saving || missing.length > 0;
  S.nodes.discardBtn.disabled = S.saving;
  S.nodes.hint.textContent = missing.length
    ? `아키타입 이름이 비어 있어 저장할 수 없습니다: ${missing.map((a) => a.id).join(", ")}` : "";
}

// Alerts live in their own node and are rebuilt only when their content changes (no re-announcing on keystrokes).
function renderAlerts() {
  const box = S.nodes.alerts;
  if (!box || !S.preview) return;
  const sig = JSON.stringify([S.changed, S.saveError, S.confirmDiscard, S.saving, S.running]);
  if (sig === S.alertSig) return;
  S.alertSig = sig;
  const kids = [];
  if (S.confirmDiscard) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-discard-yes": "", disabled: S.saving }, "버리기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", discard);
    no.addEventListener("click", () => { S.confirmDiscard = false; renderAlerts(); S.nodes.discardBtn.focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "미리보기를 버립니다. 고친 내용은 사라집니다. 저장된 결과는 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (S.changed) {
    const rerun = h("button", { type: "button", class: "btn", disabled: S.running || isActive(S.job) }, "다시 실행");
    rerun.addEventListener("click", startRun);
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, CHANGED_TEXT), rerun));
  }
  if (S.saveError) kids.push(h("p", { class: "error", role: "alert" }, S.saveError));
  fill(box, ...kids);
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
