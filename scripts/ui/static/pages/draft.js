// 기획안 화면
//   #/draft                 현재 카테고리의 저장된 기획안 목록
//   #/draft/view/<path>     기획안 상세(읽기 전용) + 콘티 화면으로 가는 링크 + 훅·제목·썸네일 다듬기(작업 kind=polish)
//   #/draft/new[/<source>]  새 기획안: 출처 고르기 → 만들기(작업: 자료 모으기 → Claude) → 미리보기(편집) → 저장
// 미리보기 단계에서는 drafts/가 바뀌지 않는다. 저장은 사용자가 누른 뒤에만, 고친 draft 그대로 보낸다.
// 작업 폴링·재진입·handled·job_id·aborted 처리는 pages/motif.js와 같은 방식이다.
import { h, fill } from "/static/dom.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const HANDLED_KEY = "ytc.draft.handled";
const ACTIVE = new Set(["running", "awaiting"]);
const MAX_TOPIC = 100;
const MIN_TARGET = 5;
const MAX_TARGET = 1800;
const GAP_BIG = 0.1;            // |기획 - 아키타입| > 10%p 이면 강조
const SUM_TOL = 0.05;           // 샷 길이 합과 섹션 길이의 허용 오차(초)
const STEP_TEXT = { context: "자료 모으는 중", claude: "Claude가 기획안 만드는 중" };
const SHOT_SIZES = ["익스트림 클로즈업", "클로즈업", "바스트", "미디엄", "와이드", "탑뷰", "POV", "인서트"];
const NEW_SHOT = { size: "미디엄", angle: "", sec: "2", action: "" };

// ---- helpers ------------------------------------------------------------------

const isActive = (job) => !!job && ACTIVE.has(job.status);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const viewHref = (path) => `#/draft/view/${encodeURIComponent(path)}`;
const contiHref = (path) => `#/conti/${encodeURIComponent(path)}`;
const round2 = (x) => Math.round(x * 100) / 100;
const pct = (x) => `${Math.round(x * 100)}%`;

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(num(sec)));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const fmtRange = (s) => `${fmtTime(s.start)}–${fmtTime(s.end)}`;
const sortByStart = (list) => [...list].sort((a, b) => num(a.start) - num(b.start));
const lastEnd = (sections) => Math.max(0, ...(sections || []).map((s) => num(s && s.end)));
const sectionTitle = (s) => `${s.label}${s.sub ? `(${s.sub})` : ""}`;

// The server escapes real md headings in scripts ("\## x"); show them as typed. Saving escapes them again.
const unescapeHeadings = (text) => text.replace(/^\\(#{1,6})(?=\s|$)/gm, "$1");

// "" and non-numbers become NaN so callers can tell "not a number" from 0.
function parseNum(text) {
  const t = String(text).trim();
  const v = Number(t);
  return t !== "" && Number.isFinite(v) ? v : NaN;
}

// Segment widths are percentages of the last section end (same drawing as the reference screens).
function sectionBand(sections) {
  const segs = sortByStart((sections || []).filter((s) => s && typeof s.label === "string" && s.label));
  const total = lastEnd(segs);
  if (!segs.length || !(total > 0)) return null;
  const band = h("div", {
    class: "sec-band", role: "img",
    "aria-label": `섹션 흐름: ${segs.map((s) => s.label).join(" → ")}`,
  });
  let cursor = 0;
  for (const s of segs) {
    const from = Math.max(num(s.start), cursor);
    const gap = Math.min(100, (Math.max(0, from - cursor) / total) * 100);
    const width = Math.max(0, Math.min(100, ((num(s.end) - from) / total) * 100));
    const text = `${s.label} ${fmtRange(s)}`;
    const seg = h("span", { class: `lb-${s.label}`, title: text, "aria-label": text });
    seg.style.width = `${width}%`;
    if (gap > 0) seg.style.marginLeft = `${gap}%`;
    band.append(seg);
    cursor = Math.max(cursor, num(s.end));
  }
  return band;
}

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
  const p = item && item.state === "awaiting" ? item.preview : null;
  return p && p.draft && Array.isArray(p.draft.sections) && p.draft.sections.length ? p : null;
}

// ---- page state ---------------------------------------------------------------

let S = null;

// Each mount / category change builds a fresh state, so late async work of the old one sees S !== mine and stops.
function freshState(el, ctx, category, params) {
  return {
    el, ctx, category, mode: params.mode || "list", path: params.path || null, wantSource: params.source || null,
    sources: null, source: params.source || "", topic: "", target: "", targetBad: false, sourceNote: null,
    job: null, jobId: null, preview: null, edit: null, dirty: false,
    timer: null, pollErrors: 0, running: false, saving: false,
    runError: null, saveError: null, confirmRerun: false, confirmDiscard: false,
    runSig: null, alertSig: null, nodes: {},
    detail: null, pl: freshPolish(), plWarnings: [],
  };
}

const alive = (mine) => S === mine && mine.ctx.isCurrent();
const ignorable = (e) => !e || e.error === "aborted";

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
  if (S && S.pl.timer) { clearTimeout(S.pl.timer); S.pl.timer = null; }
  if (S && S.pl.lintTimer) { clearTimeout(S.pl.lintTimer); S.pl.lintTimer = null; }
}

function focusStatus() {
  const t = S && S.nodes.status;
  if (t) { t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
}

const crumb = () => h("p", { class: "crumb" }, h("a", { href: "#/draft" }, "← 기획안 목록"));

// ---- load ---------------------------------------------------------------------

function loadError(e, mine) {
  const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
  retry.addEventListener("click", () => { if (alive(mine)) load(); });
  fill(mine.el, mine.mode === "list" ? null : crumb(), h("h1", {}, "기획안"),
    h("p", { class: "error", role: "alert" }, (e && e.message) || "불러오지 못했습니다"),
    h("div", { class: "btn-row" }, retry));
}

async function load() {
  const mine = S;
  if (mine.mode === "view") { await loadView(mine); return; }
  const cat = mine.category;
  if (!cat) {
    fill(mine.el, h("h1", {}, "기획안"), h("p", { class: "muted" }, "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요."));
    return;
  }
  fill(mine.el, h("h1", {}, "기획안"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  if (mine.mode === "new") await loadNew(mine, cat); else await loadList(mine, cat);
}

async function loadList(mine, cat) {
  try {
    const data = await mine.ctx.api("GET", `/api/draft/list?category=${encodeURIComponent(cat)}`);
    if (!alive(mine)) return;
    renderList(mine, data.drafts || []);
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e, mine);
  }
}

async function loadView(mine) {
  if (!mine.path) { fill(mine.el, crumb(), h("h1", {}, "기획안"), h("p", { class: "error", role: "alert" }, "기획안 경로가 없습니다")); return; }
  fill(mine.el, crumb(), h("h1", {}, "기획안"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  try {
    const data = await mine.ctx.api("GET", `/api/draft?path=${encodeURIComponent(mine.path)}`);
    if (!alive(mine)) return;
    await adoptExistingPolish(mine, data.path);
    if (!alive(mine)) return;
    renderView(mine, data);
    if (isActive(mine.pl.job) && !mine.pl.timer) schedulePolishPoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e, mine);
  }
}

async function loadNew(mine, cat) {
  try {
    const [src, cur] = await Promise.all([
      mine.ctx.api("GET", `/api/draft/sources?category=${encodeURIComponent(cat)}`),
      mine.ctx.api("GET", "/api/jobs/current?kind=draft"),
    ]);
    if (!alive(mine)) return;
    mine.sources = src;
    const ids = sourceIds(src);
    if (mine.wantSource && !ids.has(mine.wantSource)) {
      mine.sourceNote = `고른 출처(${mine.wantSource})를 찾을 수 없어 목록에서 다시 골라야 합니다`;
      mine.source = "";
    }
    const job = cur.job;
    // The latest draft job may belong to another category.
    if (job && job.category === cat && !isHandled(job.id)) {
      if (isActive(job) || (job.status === "done" && previewOf(job))) {
        adoptJob(mine, job);
      } else if (job.status === "failed" || job.status === "stopped") {
        adoptJob(mine, job);          // shown once, then not again
        markHandled(job.id);
      }
    }
    renderNew();
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    loadError(e, mine);
  }
}

function sourceIds(src) {
  const ids = new Set();
  for (const list of [src.stars, src.cards, src.archetypes]) for (const x of list || []) ids.add(x.id);
  return ids;
}

function adoptJob(mine, job) {
  mine.job = job;
  const preview = job.status === "done" ? previewOf(job) : null;
  if (preview && (!mine.preview || mine.jobId !== job.id)) {
    mine.preview = preview;
    mine.jobId = job.id;
    if (preview.source && preview.source.id) mine.source = preview.source.id;
    initEdit(mine, preview);
  }
}

function initEdit(mine, preview) {
  const d = preview.draft;
  mine.edit = {
    title: String(d.title || ""),
    sections: d.sections.map((s) => ({
      src: s,
      script: unescapeHeadings(String(s.script || "")),
      shots: (s.shots || []).map((sh) => ({
        src: sh, size: String(sh.size || ""), angle: String(sh.angle || ""),
        sec: sh.sec === null || sh.sec === undefined ? "" : String(sh.sec), action: String(sh.action || ""),
      })),
      nodes: {},
    })),
    prep: (d.prep || []).join("\n"),
  };
  mine.dirty = false;
  mine.confirmRerun = false;
  mine.confirmDiscard = false;
  mine.runError = null;
  mine.saveError = null;
}

// ---- list ---------------------------------------------------------------------

function draftRow(d) {
  const sections = (d.sections || []).filter((s) => s && s.label);
  const total = lastEnd(sections) || num(d.target_sec);
  const conti = d.conti
    ? h("span", { class: "badge conti-yes" }, "콘티 있음")
    : h("span", { class: "badge" }, "콘티 없음");
  const meta = [String(d.date || "").slice(0, 10), d.source ? `출처 ${d.source}` : null, total > 0 ? `길이 ${fmtTime(total)}` : null]
    .filter(Boolean).join(" · ");
  return h("li", { class: "card dr-row" },
    h("div", { class: "card-head" },
      h("h2", {}, h("a", { href: viewHref(d.path) }, String(d.title))), conti),
    h("p", { class: "muted small" }, meta),
    sectionBand(sections));
}

function renderList(mine, drafts) {
  const make = h("a", { class: "btn btn-primary", href: "#/draft/new" }, "새 기획안 만들기");
  const kids = [h("h1", {}, "기획안"), h("p", { class: "lead" }, `${mine.category} · 저장한 기획안 ${drafts.length}개`)];
  if (!drafts.length) {
    kids.push(h("p", { class: "muted" }, "아직 기획안이 없습니다"), h("div", { class: "btn-row" }, make));
  } else {
    kids.push(h("div", { class: "btn-row dr-tools" }, make),
      h("ul", { class: "dr-list", "aria-label": "저장한 기획안" }, ...drafts.map(draftRow)));
  }
  fill(mine.el, ...kids);
}

// ---- view (read only) ---------------------------------------------------------

async function copyText(mine, text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    if (alive(mine)) mine.ctx.toast(okMsg, "info");
  } catch {
    if (alive(mine)) mine.ctx.toast("복사하지 못했습니다. 직접 선택해 복사하세요", "error");
  }
}

function renderView(mine, d) {
  const sections = (d.sections || []).filter((s) => s && s.label);
  const pacing = d.pacing && typeof d.pacing === "object" ? d.pacing : null;
  const meta = [
    String(d.date || "").slice(0, 10),
    d.source ? `출처 ${d.source}` : null,
    d.archetype ? `아키타입 ${d.archetype}` : null,
    lastEnd(sections) > 0 ? `길이 ${fmtTime(lastEnd(sections))}` : null,
    d.hook_type ? `훅 ${d.hook_type}` : null,
    d.narrative_type ? `서사 ${d.narrative_type}` : null,
    pacing && pacing.avg_cut_sec ? `평균 컷 ${pacing.avg_cut_sec}초` : null,
  ].filter(Boolean).join(" · ");
  mine.detail = d;
  mine.nodes = {};
  mine.nodes.status = h("h1", {}, String(d.title));
  mine.nodes.plBar = h("div", { class: "pol-bar" });
  mine.nodes.plBody = h("div", { class: "pol-body" });
  const kids = [crumb(), mine.nodes.status, h("p", { class: "muted small" }, meta),
    mine.nodes.plBar, mine.nodes.plBody, sectionBand(sections)];
  if (sections.length) {
    kids.push(h("ul", { class: "ref-sec-list dr-sec-list", "aria-label": "섹션" }, ...sortByStart(sections).map((s) =>
      h("li", {}, h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" }), `${sectionTitle(s)} ${fmtRange(s)}`))));
  }
  for (const part of d.body || []) {
    const text = h("div", { class: "md-text" });
    text.textContent = part.text || "";
    kids.push(h("section", { class: "ref-block" }, part.heading ? h("h2", {}, part.heading) : null, text));
  }
  const copy = h("button", { type: "button", class: "btn" }, "경로 복사");
  copy.addEventListener("click", () => copyText(mine, d.path, "경로를 복사했습니다"));
  kids.push(
    h("div", { class: "btn-row" },
      h("a", { class: "btn btn-primary", href: contiHref(d.path) }, d.conti ? "콘티 보기" : "콘티 만들기")),
    h("div", { class: "ref-path" }, h("span", { class: "muted small" }, "md 파일"), h("code", {}, d.path), copy));
  fill(mine.el, ...kids);
  mine.pl.barSig = null;
  renderPolishBar(mine);
  renderPolishBody(mine);
}

// ---- new: run box -------------------------------------------------------------

function sourceLabel(x) {
  if (x.name !== undefined) {                                   // archetype
    const dur = num(x.mean_duration) > 0 ? ` · 평균 ${fmtTime(x.mean_duration)}` : "";
    return `${x.id}${x.name ? ` ${x.name}` : ""}${dur}`;
  }
  return `${x.id} · ${x.type_ko ? `[${x.type_ko}] ` : ""}${x.title}`;
}

function sourceSelect(disabled) {
  const sel = h("select", { id: "draft-source", disabled });
  sel.append(h("option", { value: "" }, "출처를 고르세요"));
  const groups = [["★ 카드", S.sources.stars], ["카드", S.sources.cards], ["아키타입", S.sources.archetypes]];
  for (const [label, list] of groups) {
    if (!list || !list.length) continue;
    sel.append(h("optgroup", { label }, ...list.map((x) => h("option", { value: x.id }, sourceLabel(x)))));
  }
  sel.value = S.source;
  sel.addEventListener("change", () => { S.source = sel.value; S.sourceNote = null; updateRunControls(); });
  return sel;
}

// Why the run button is disabled (empty string when it can be used).
function runBlocker() {
  if (!S.source) return "출처를 골라 주세요";
  if (S.targetBad) return "목표 길이는 숫자로 적어 주세요";
  if (S.target.trim() !== "") {
    const v = parseNum(S.target);
    if (!(v >= MIN_TARGET && v <= MAX_TARGET)) return `목표 길이는 ${MIN_TARGET}~${MAX_TARGET}초로 적어 주세요`;
  }
  return "";
}

function updateRunControls() {
  const n = S.nodes;
  if (!n.runBtn) return;
  const reason = runBlocker();
  n.runBtn.disabled = S.running || isActive(S.job) || S.saving || !!reason;
  n.runHint.textContent = reason;
}

function requestRerun(from) {
  if (S.preview && S.dirty && !S.confirmRerun) {
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
  const yes = h("button", { type: "button", class: "btn btn-danger", "data-rerun-yes": "" }, "다시 만들기");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  yes.addEventListener("click", startRun);
  no.addEventListener("click", () => {
    S.confirmRerun = false;
    S.alertSig = null;
    renderRunBox();
    renderAlerts();
    const back = from === "preview" ? S.nodes.rerunBtn : S.nodes.runBtn;
    if (back) back.focus();
  });
  return h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, "지금 미리보기에서 고친 내용은 사라집니다. 저장한 기획안은 그대로입니다."),
    h("div", { class: "btn-row" }, yes, no));
}

async function startRun() {
  const mine = S;
  if (mine.running || mine.saving || isActive(mine.job) || runBlocker()) return;
  mine.running = true;
  mine.runError = null;
  mine.confirmRerun = false;
  mine.alertSig = null;
  renderRunBox();
  try {
    const body = { category: mine.category, source: mine.source };
    const topic = mine.topic.trim();
    if (topic) body.topic = topic;
    if (mine.target.trim() !== "") body.target_sec = parseNum(mine.target);
    const res = await mine.ctx.api("POST", "/api/draft/run", body);
    if (!alive(mine)) return;
    if (mine.jobId) markHandled(mine.jobId);
    mine.job = res.job;
    mine.preview = null;
    mine.jobId = null;
    mine.edit = null;
    mine.dirty = false;
    mine.pollErrors = 0;
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    mine.runError = e.message || "기획안을 만들지 못했습니다";
  } finally {
    if (alive(mine)) {
      mine.running = false;
      if (mine.runError) {            // keep the preview and its focus; only the run box changes
        renderRunBox();
        if (mine.nodes.runBtn) mine.nodes.runBtn.focus();
      } else {
        renderNew();
        focusStatus();
      }
    }
  }
}

// Source / topic / target inputs, run button, progress and errors. Re-rendered by polling without touching the (editable) preview.
function renderRunBox() {
  const box = S.nodes.run;
  if (!box) return;
  const job = S.job;
  const active = isActive(job);
  const failed = !!job && (job.status === "failed" || job.status === "stopped");
  const item0 = job && job.items && job.items[0];
  const sig = JSON.stringify([active, failed, S.running, S.runError, S.confirmRerun, S.sourceNote,
    item0 && item0.step, item0 && item0.message, job && job.message, !!S.preview, S.saving]);
  if (sig === S.runSig && box.childNodes.length) return;
  S.runSig = sig;
  const busy = S.running || active || S.saving;
  const topic = h("input", {
    id: "draft-topic", type: "text", maxlength: String(MAX_TOPIC), placeholder: "예: 편의점 라면", disabled: busy,
  });
  topic.value = S.topic;
  topic.addEventListener("input", () => { S.topic = topic.value; });
  const target = h("input", {
    id: "draft-target", type: "number", min: String(MIN_TARGET), max: String(MAX_TARGET), step: "1",
    inputmode: "numeric", placeholder: "예: 60", disabled: busy, "aria-describedby": "draft-run-hint",
  });
  target.value = S.target;
  S.targetBad = false;          // a rebuilt input starts from the stored value, so a typed-but-invalid text is gone
  target.addEventListener("input", () => { S.target = target.value; S.targetBad = target.validity.badInput; updateRunControls(); });
  S.nodes.runBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "draft-run-hint" }, "기획안 만들기");
  S.nodes.runBtn.addEventListener("click", () => requestRerun("run"));
  S.nodes.runHint = h("span", { class: "muted small", id: "draft-run-hint" });
  const kids = [
    h("div", { class: "field" }, h("label", { for: "draft-source" }, "출처"), sourceSelect(busy)),
    S.sourceNote ? h("p", { class: "muted small", role: "status" }, S.sourceNote) : null,
    h("div", { class: "field" }, h("label", { for: "draft-topic" }, "주제(선택)"), topic),
    h("div", { class: "field" }, h("label", { for: "draft-target" }, `목표 길이 초(선택, ${MIN_TARGET}~${MAX_TARGET})`), target),
    h("div", { class: "btn-row" }, S.nodes.runBtn, S.nodes.runHint, h("span", { class: "muted small" }, "Claude를 한 번 부릅니다(수십 초)")),
  ];
  if (S.confirmRerun === "run") kids.push(rerunConfirm("run"));
  if (active) {
    const step = (item0 && STEP_TEXT[item0.step]) || "준비 중";
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "기획안 만들기 진행" }),
      h("span", {}, `${step}…`)));
  }
  if (failed) {
    kids.push(h("p", { class: "error", role: "alert" },
      `${(item0 && item0.message) || job.message || "기획안을 만들지 못했습니다"} 저장한 기획안은 그대로입니다.`));
  }
  if (S.runError) kids.push(h("p", { class: "error", role: "alert" }, S.runError));
  fill(box, ...kids);
  updateRunControls();
}

// ---- polling ------------------------------------------------------------------

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
        renderNew();
        focusStatus();
      } else {
        renderRunBox();
      }
    } catch (e) {
      if (!alive(mine) || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 기획안을 다시 만드세요", "error");
        mine.job = null;
        renderNew();
        return;
      }
      mine.pollErrors += 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors));
}

// ---- new: page ----------------------------------------------------------------

function renderNew() {
  S.nodes = {};
  S.runSig = null;
  S.alertSig = null;
  S.nodes.status = h("h1", { id: "draft-title" }, "새 기획안 만들기");
  const kids = [crumb(), S.nodes.status];
  const src = S.sources;
  const any = sourceIds(src).size > 0;
  if (!any) {
    kids.push(h("p", { class: "muted hint" }, "고를 수 있는 출처가 없습니다. ",
      h("a", { href: "#/motif" }, "모티브에서 카드 만들기"), " 또는 ", h("a", { href: "#/typify" }, "유형화로 아키타입 만들기")));
  } else {
    S.nodes.run = h("section", { class: "job-box motif-run", "aria-label": "기획안 만들기" });
    kids.push(S.nodes.run);
  }
  S.nodes.body = h("div", { class: "motif-body" });
  kids.push(S.nodes.body);
  fill(S.el, ...kids);
  renderRunBox();
  renderBody();
}

function renderBody() {
  const body = S.nodes.body;
  if (!body) return;
  if (!S.preview || !S.edit) { fill(body); return; }
  fill(body, previewView());
  updateAll();
  renderAlerts();
}

// ---- preview ------------------------------------------------------------------

const cutCount = () => S.edit.sections.reduce((n, s) => n + s.shots.length, 0);

function shotsSum(sec) {
  return sec.shots.reduce((sum, sh) => {
    const v = parseNum(sh.sec);
    return Number.isFinite(v) && v > 0 ? sum + v : sum;
  }, 0);
}

// Why the save button is disabled (empty string when it can be used). Over/under-long shots only warn.
function saveBlocker() {
  if (!S.edit.title.trim()) return "제목이 비어 있습니다";
  for (const [i, sec] of S.edit.sections.entries()) {
    if (!sec.shots.length) return `${i + 1}번 섹션에 샷이 하나 이상 있어야 합니다`;
    for (const [k, sh] of sec.shots.entries()) {
      if (!SHOT_SIZES.includes(sh.size)) return `${i + 1}번 섹션 ${k + 1}번 샷의 크기를 고르세요`;
      if (!(parseNum(sh.sec) > 0)) return `${i + 1}번 섹션 ${k + 1}번 샷의 길이는 0보다 큰 숫자여야 합니다`;
    }
  }
  return "";
}

function draftPayload() {
  const e = S.edit;
  const { script_only: _scriptOnly, ...base } = S.preview.draft;   // the screen never saves script-only drafts
  return {
    ...base,
    title: e.title.trim(),
    sections: e.sections.map((sec) => ({
      ...sec.src,
      script: sec.script,
      shots: sec.shots.map((sh) => ({ ...sh.src, size: sh.size, angle: sh.angle, sec: parseNum(sh.sec), action: sh.action })),
    })),
    prep: e.prep.split("\n").map((l) => l.trim()).filter(Boolean),
  };
}

function compareView(c) {
  const rows = (c.ratio || []).map((r) => {
    const big = Math.abs(num(r.gap)) > GAP_BIG + 1e-9;
    const gap = `${num(r.gap) > 0 ? "+" : ""}${Math.round(num(r.gap) * 100)}%p`;
    return h("tr", { class: big ? "is-warn gap-big" : null },
      h("th", { scope: "row" }, h("span", { class: `lb-dot lb-${r.label}`, "aria-hidden": "true" }), ` ${r.label}`),
      h("td", { class: "num" }, pct(num(r.draft))),
      h("td", { class: "num" }, pct(num(r.archetype))),
      h("td", { class: "num" }, gap, big ? h("span", { class: "badge gap-flag" }, "차이 큼") : null));
  });
  return h("section", { class: "dr-compare", "aria-labelledby": "draft-compare-title" },
    h("h2", { id: "draft-compare-title" }, `아키타입 ${c.archetype}${c.name ? ` ${c.name}` : ""}와 섹션 비율`),
    h("div", { class: "table-wrap" },
      h("table", { class: "preview-table" },
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "섹션"), h("th", { scope: "col" }, "기획"),
          h("th", { scope: "col" }, "아키타입"), h("th", { scope: "col" }, "차이"))),
        h("tbody", {}, ...rows))));
}

function shotRow(sec, si, k) {
  const sh = sec.shots[k];
  const label = (what) => `${si + 1}번 섹션 ${k + 1}번 샷 ${what}`;
  // The section warning is a live region: it is refreshed on change/blur, not on every keystroke.
  const touch = () => { S.dirty = true; updateSummary(); updateActions(); };
  const settle = () => updateSection(sec, si);
  const size = h("select", { "aria-label": label("크기") },
    ...(SHOT_SIZES.includes(sh.size) ? [] : [""]).concat(SHOT_SIZES).map((v) => h("option", { value: v }, v || "크기 선택")));
  size.value = SHOT_SIZES.includes(sh.size) ? sh.size : "";
  size.addEventListener("change", () => { sh.size = size.value; touch(); settle(); });
  const angle = h("input", { type: "text", "aria-label": label("앵글"), maxlength: "60" });
  angle.value = sh.angle;
  angle.addEventListener("input", () => { sh.angle = angle.value; touch(); });
  const secInput = h("input", { type: "number", min: "0.1", step: "0.1", class: "dr-sec-input", "aria-label": label("길이(초)") });
  secInput.value = sh.sec;
  secInput.addEventListener("input", () => { sh.sec = secInput.value; touch(); });
  secInput.addEventListener("change", settle);
  const action = h("input", { type: "text", "aria-label": label("내용"), maxlength: "200" });
  action.value = sh.action;
  action.addEventListener("input", () => { sh.action = action.value; touch(); });
  const del = h("button", { type: "button", class: "btn", "aria-label": label("삭제"), "data-del": String(k) }, "삭제");
  del.addEventListener("click", () => {
    sec.shots.splice(k, 1);
    S.dirty = true;
    renderShots(sec, si);
    updateSection(sec, si);
    updateSummary();
    updateActions();
    const next = sec.nodes.tbody.querySelector(`[data-del="${Math.min(k, sec.shots.length - 1)}"]`);
    (next || sec.nodes.addBtn).focus();
  });
  return h("tr", {},
    h("td", {}, size), h("td", {}, angle), h("td", {}, secInput), h("td", { class: "dr-action" }, action), h("td", {}, del));
}

function renderShots(sec, si) {
  fill(sec.nodes.tbody, ...sec.shots.map((_, k) => shotRow(sec, si, k)));
}

// Warning under a section when the shot lengths do not add up to the section length (save stays possible).
function updateSection(sec, si) {
  const warn = sec.nodes.warn;
  const dur = num(sec.src.end) - num(sec.src.start);
  let text = "";
  if (!sec.shots.length) {
    text = "샷이 하나 이상 있어야 저장할 수 있습니다";
  } else {
    const sum = shotsSum(sec);
    if (Math.abs(sum - dur) > SUM_TOL) text = `샷 길이 합 ${round2(sum)}초 · 섹션 ${round2(dur)}초`;
  }
  if (warn.textContent !== text) warn.textContent = text;
  sec.nodes.table.setAttribute("aria-label", `${si + 1}번 섹션 샷 표`);
}

function sectionCard(sec, si) {
  const s = sec.src;
  const n = si + 1;
  const script = h("textarea", { rows: "4", "aria-label": `${n}번 섹션 대본` });
  script.value = sec.script;
  script.addEventListener("input", () => { sec.script = script.value; S.dirty = true; });
  sec.nodes.tbody = h("tbody");
  sec.nodes.warn = h("p", { class: "warn-text small", role: "status" });
  sec.nodes.addBtn = h("button", { type: "button", class: "btn" }, "샷 추가");
  sec.nodes.addBtn.addEventListener("click", () => {
    sec.shots.push({ src: {}, ...NEW_SHOT });
    S.dirty = true;
    renderShots(sec, si);
    updateSection(sec, si);
    updateSummary();
    updateActions();
    const rows = sec.nodes.tbody.querySelectorAll("tr");
    const first = rows.length ? rows[rows.length - 1].querySelector("select") : null;
    if (first) first.focus();
  });
  sec.nodes.table = h("table", { class: "preview-table dr-shots" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "크기"), h("th", { scope: "col" }, "앵글"),
      h("th", { scope: "col" }, "길이(초)"), h("th", { scope: "col" }, "내용"), h("th", { scope: "col" }, "삭제"))),
    sec.nodes.tbody);
  renderShots(sec, si);
  return h("article", { class: "card dr-sec" },
    h("h3", { class: "dr-sec-title" }, h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" }),
      ` ${fmtRange(s)} · ${sectionTitle(s)}`, h("span", { class: "muted small" }, ` (${round2(num(s.end) - num(s.start))}초)`)),
    h("div", { class: "field" }, h("label", {}, "대본", script)),
    h("div", { class: "table-wrap" }, sec.nodes.table),
    h("div", { class: "btn-row" }, sec.nodes.addBtn),
    sec.nodes.warn);
}

function previewView() {
  const p = S.preview;
  const e = S.edit;
  const title = h("input", { type: "text", id: "draft-edit-title", maxlength: "120" });
  title.value = e.title;
  title.addEventListener("input", () => { e.title = title.value; S.dirty = true; updateActions(); });
  S.nodes.summary = h("p", { class: "dr-summary", role: "status" });
  const prep = h("textarea", { rows: "4", id: "draft-prep" });
  prep.value = e.prep;
  prep.addEventListener("input", () => { e.prep = prep.value; S.dirty = true; });
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "draft-save-hint" }, "저장");
  const rerunBtn = h("button", { type: "button", class: "btn" }, "다시 만들기");
  const discardBtn = h("button", { type: "button", class: "btn" }, "버리기");
  saveBtn.addEventListener("click", save);
  rerunBtn.addEventListener("click", () => requestRerun("preview"));
  discardBtn.addEventListener("click", askDiscard);
  S.nodes.saveBtn = saveBtn;
  S.nodes.rerunBtn = rerunBtn;
  S.nodes.discardBtn = discardBtn;
  S.nodes.hint = h("p", { class: "muted hint", id: "draft-save-hint" });
  S.nodes.alerts = h("div", { class: "preview-alerts" });
  S.alertSig = null;
  const src = p.source;
  return h("section", { class: "motif-sec", "aria-labelledby": "draft-preview-title" },
    h("h2", { id: "draft-preview-title" }, src && src.title ? `미리보기 · 출처: ${src.title}` : "미리보기"),
    h("p", { class: "muted small" }, "저장하기 전에는 파일이 바뀌지 않습니다. 섹션 구간·라벨은 고칠 수 없고, 다른 화면으로 가면 고친 내용은 사라집니다."),
    h("div", { class: "field" }, h("label", { for: "draft-edit-title" }, "제목"), title),
    S.nodes.summary,
    p.compare ? compareView(p.compare) : null,
    ...e.sections.map(sectionCard),
    h("div", { class: "field" }, h("label", { for: "draft-prep" }, "준비물 (한 줄에 하나)"), prep),
    h("div", { class: "preview-actions" },
      h("div", { class: "btn-row" }, saveBtn, rerunBtn, discardBtn), S.nodes.hint, S.nodes.alerts));
}

function updateSummary() {
  const node = S.nodes.summary;
  if (!node || !S.edit) return;
  const dur = lastEnd(S.edit.sections.map((s) => s.src));
  const cuts = cutCount();
  const parts = [`길이 ${fmtTime(dur)}`, `컷 ${cuts}개`, cuts ? `평균 컷 ${round2(dur / cuts)}초` : "평균 컷 -"];
  const arch = S.preview.compare && S.preview.compare.archetype_avg_cut;
  if (arch) parts.push(`아키타입 평균 컷 ${round2(num(arch))}초`);
  const text = parts.join(" · ");
  if (node.textContent !== text) node.textContent = text;
}

function updateActions() {
  if (!S.preview || !S.nodes.saveBtn) return;
  const reason = saveBlocker();
  S.nodes.saveBtn.disabled = S.saving || !!reason;
  S.nodes.rerunBtn.disabled = S.saving || S.running || isActive(S.job);
  S.nodes.discardBtn.disabled = S.saving;
  S.nodes.hint.textContent = reason;
}

function updateAll() {
  S.edit.sections.forEach((sec, i) => updateSection(sec, i));
  updateSummary();
  updateActions();
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
      h("p", { class: "warn-text" }, "미리보기를 버립니다. 고친 내용은 사라집니다. 저장한 기획안은 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (S.confirmRerun === "preview") kids.push(rerunConfirm("preview"));
  if (S.saveError) kids.push(h("p", { class: "error", role: "alert" }, S.saveError));
  fill(box, ...kids);
}

// ---- save / discard -----------------------------------------------------------

async function save() {
  const mine = S;
  if (mine.saving || !mine.preview || !mine.edit || saveBlocker()) return;
  mine.saving = true;
  mine.saveError = null;
  updateActions();
  renderAlerts();
  renderRunBox();
  try {
    const res = await mine.ctx.api("POST", "/api/draft/save", {
      category: mine.category, draft: draftPayload(), job_id: mine.jobId,
    });
    markHandled(mine.jobId);                    // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.ctx.toast("기획안을 저장했습니다", "info");
    mine.saving = false;
    location.hash = viewHref(res.path);          // hashchange mounts the detail page
  } catch (e) {
    // An aborted request (page left mid-save) may still have been saved by the server: never restore that preview.
    if (e && e.error === "aborted") markHandled(mine.jobId);
    if (!alive(mine) || ignorable(e)) return;
    mine.saveError = e.message || "저장하지 못했습니다";
    mine.saving = false;
    updateActions();
    renderAlerts();
    renderRunBox();
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
  S.edit = null;
  S.dirty = false;
  S.job = null;
  S.jobId = null;
  S.saveError = null;
  S.confirmDiscard = false;
  S.ctx.toast("미리보기를 닫았습니다. 저장하지 않았습니다", "info");
  renderNew();
  focusStatus();
}

// ---- polish: 훅·제목·썸네일 다듬기 (상세 화면) ------------------------------------
// 작업(kind=polish)은 후보와 점수·지적·근거를 만들어 미리보기까지만 간다. 기획안 파일은 저장을 누른 뒤에만 바뀐다.
// 미리보기는 서버가 계산한 값이 기준이다. 고른 후보를 고치거나 다른 후보를 고르면 /api/polish/lint로 점수를 다시 계산한다(Claude 호출 없음).

const POLISH_STEP_TEXT = { context: "기획안 읽는 중", claude: "Claude가 후보 만드는 중" };
const THUMB_SLOTS = 3;
const MAX_HOOK_SCRIPT = 600;
const MAX_POLISH_TITLE = 120;
const MAX_THUMB_TEXT = 200;
const RELINT_DELAY = 400;
const round1 = (x) => Math.round(x * 10) / 10;

function freshPolish() {
  return {
    job: null, jobId: null, result: null, context: null, edit: null, dirty: false,
    running: false, saving: false, runError: null, saveError: null, conflict: false, refreshed: false,
    confirmRerun: false, confirmClose: false, timer: null, pollErrors: 0, barSig: null, alertSig: null,
    lintTimer: null, lintSeq: { hook: 0, tt: 0 }, lintPending: new Set(), lintFailed: false, thumbIssuesNode: null,
    nodes: {}, pv: {},
  };
}

// The preview is only valid for the draft it was made from (job item id = draft path).
function polishPreviewOf(job, path) {
  const item = job && job.items && job.items[0];
  if (!item || item.id !== path || item.state !== "awaiting") return null;
  const p = item.preview;
  const r = p && p.result;
  const ok = r && Array.isArray(r.hooks) && r.hooks.length && Array.isArray(r.titles) && r.titles.length
    && r.thumbnail && p.context;
  return ok ? p : null;
}

function initPolishEdit(pl, preview) {
  const r = preview.result;
  const words = (r.thumbnail.words || []).map(String);
  // More than THUMB_SLOTS phrases are folded into the last slot so nothing is dropped silently.
  const slots = Array.from({ length: THUMB_SLOTS }, (_, i) =>
    (i < THUMB_SLOTS - 1 ? words[i] : words.slice(i).join(" ")) || "");
  pl.result = r;
  pl.context = preview.context;
  pl.edit = {
    hook: 0, title: 0,
    hooks: r.hooks.map((x) => String(x.script || "")),
    titles: r.titles.map((x) => String(x.title || "")),
    words: slots,
    expression: String(r.thumbnail.expression || ""),
    framing: String(r.thumbnail.framing || ""),
    background: String(r.thumbnail.background || ""),
  };
  pl.dirty = false;
  pl.conflict = false;
  pl.refreshed = false;
  pl.confirmRerun = false;
  pl.confirmClose = false;
  pl.saveError = null;
  pl.alertSig = null;
}

function adoptPolishJob(mine, job, path) {
  const pl = mine.pl;
  pl.job = job;
  const p = job.status === "done" ? polishPreviewOf(job, path) : null;
  if (p && (!pl.result || pl.jobId !== job.id)) {
    pl.jobId = job.id;
    initPolishEdit(pl, p);
  }
}

// Re-entry: a running / finished polish job for this very draft is picked up again after a reload.
async function adoptExistingPolish(mine, path) {
  const pl = mine.pl;
  if (pl.job || pl.result) return;
  try {
    const cur = await mine.ctx.api("GET", "/api/jobs/current?kind=polish");
    if (!alive(mine)) return;
    const job = cur.job;
    const item = job && job.items && job.items[0];
    if (!job || !item || item.id !== path || isHandled(job.id)) return;
    if (isActive(job) || (job.status === "done" && polishPreviewOf(job, path))) {
      adoptPolishJob(mine, job, path);
    } else if (job.status === "failed" || job.status === "stopped") {
      adoptPolishJob(mine, job, path);          // shown once, then not again
      markHandled(job.id);
    }
  } catch { /* the detail works without polish state */ }
}

// ---- polish: run bar ----------------------------------------------------------

function renderPolishBar(mine) {
  const box = mine.nodes.plBar;
  const pl = mine.pl;
  if (!box) return;
  const job = pl.job;
  const active = isActive(job);
  const failed = !!job && (job.status === "failed" || job.status === "stopped");
  const item0 = job && job.items && job.items[0];
  const unreadable = !!job && job.status === "done" && !pl.result;
  const sig = JSON.stringify([active, failed, unreadable, pl.running, pl.runError, pl.saving, pl.confirmRerun === "bar",
    item0 && item0.step, item0 && item0.message, job && job.message, mine.plWarnings]);
  if (sig === pl.barSig && box.childNodes.length) return;
  pl.barSig = sig;
  const btn = h("button", { type: "button", class: "btn", id: "polish-run", "aria-describedby": "polish-run-hint" },
    "훅·제목·썸네일 다듬기");
  btn.disabled = pl.running || active || pl.saving;
  btn.addEventListener("click", () => requestPolish("bar"));
  pl.nodes.runBtn = btn;
  const kids = [h("div", { class: "btn-row" }, btn,
    h("span", { class: "muted small", id: "polish-run-hint" }, "Claude를 한 번 부릅니다(수십 초)"))];
  if (pl.confirmRerun === "bar") kids.push(polishRerunConfirm("bar"));
  if (active) {
    const step = (item0 && POLISH_STEP_TEXT[item0.step]) || "준비 중";
    kids.push(h("div", { class: "job-status", role: "status" },
      h("progress", { class: "job-progress", "aria-label": "다듬기 진행" }), h("span", {}, `${step}…`)));
  }
  if (failed) {
    kids.push(h("p", { class: "error", role: "alert" },
      `${(item0 && item0.message) || job.message || "다듬지 못했습니다"} 기획안은 그대로입니다.`));
  }
  if (unreadable) kids.push(h("p", { class: "error", role: "alert" }, "결과를 읽지 못했습니다. 다시 다듬어 주세요"));
  if (pl.runError) kids.push(h("p", { class: "error", role: "alert" }, pl.runError));
  if (mine.plWarnings.length) {
    const ok = h("button", { type: "button", class: "btn" }, "확인");
    ok.addEventListener("click", () => { mine.plWarnings = []; renderPolishBar(mine); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "기획안에 반영했지만 확인할 것이 있습니다"),
      h("ul", { class: "pol-issues" }, ...mine.plWarnings.map((w) => h("li", {}, w))),
      h("div", { class: "btn-row" }, ok)));
  }
  fill(box, ...kids);
}

function polishRerunConfirm(from) {
  const yes = h("button", { type: "button", class: "btn btn-danger", "data-polish-rerun-yes": "" }, "다시 다듬기");
  const no = h("button", { type: "button", class: "btn" }, "취소");
  yes.addEventListener("click", startPolish);
  no.addEventListener("click", () => {
    const mine = S;
    mine.pl.confirmRerun = false;
    mine.pl.barSig = null;
    mine.pl.alertSig = null;
    renderPolishBar(mine);
    renderPolishAlerts(mine);
    const back = from === "bar" ? mine.pl.nodes.runBtn : mine.pl.pv.rerunBtn;
    if (back) back.focus();
  });
  return h("div", { class: "inline-confirm", role: "alert" },
    h("p", { class: "warn-text" }, "지금 고친 내용은 사라집니다. 기획안 파일은 그대로입니다."),
    h("div", { class: "btn-row" }, yes, no));
}

function requestPolish(from) {
  const mine = S;
  const pl = mine.pl;
  if (pl.result && pl.dirty && !pl.confirmRerun) {
    pl.confirmRerun = from;
    pl.barSig = null;
    pl.alertSig = null;
    renderPolishBar(mine);
    renderPolishAlerts(mine);
    const box = from === "bar" ? mine.nodes.plBar : pl.pv.alerts;
    const yes = box && box.querySelector("[data-polish-rerun-yes]");
    if (yes) yes.focus();
    return;
  }
  startPolish();
}

async function startPolish() {
  const mine = S;
  const pl = mine.pl;
  if (pl.running || pl.saving || isActive(pl.job) || !mine.detail) return;
  pl.running = true;
  pl.runError = null;
  pl.confirmRerun = false;
  pl.confirmClose = false;
  pl.barSig = null;
  pl.alertSig = null;
  updatePolishActions(mine);
  renderPolishBar(mine);
  renderPolishAlerts(mine);
  try {
    const res = await mine.ctx.api("POST", "/api/polish/run", { draft: mine.detail.path });
    if (!alive(mine)) return;
    if (pl.jobId) markHandled(pl.jobId);
    pl.job = res.job;
    pl.result = null;
    pl.context = null;
    pl.edit = null;
    pl.jobId = null;
    pl.dirty = false;
    pl.conflict = false;
    pl.refreshed = false;
    pl.saveError = null;
    pl.pollErrors = 0;
    if (isActive(pl.job)) schedulePolishPoll();
  } catch (e) {
    if (!alive(mine) || ignorable(e)) return;
    pl.runError = e.message || "다듬지 못했습니다";
  } finally {
    if (alive(mine)) {
      pl.running = false;
      pl.barSig = null;
      renderPolishBar(mine);
      if (pl.runError) {             // keep the preview and its focus; only the bar changes
        if (pl.nodes.runBtn) pl.nodes.runBtn.focus();
      } else {
        renderPolishBody(mine);
      }
    }
  }
}

function schedulePolishPoll() {
  const mine = S;
  const pl = mine.pl;
  if (pl.timer) { clearTimeout(pl.timer); pl.timer = null; }
  pl.timer = setTimeout(async () => {
    pl.timer = null;
    if (!alive(mine) || mine.pl !== pl || !pl.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${encodeURIComponent(pl.job.id)}`);
      if (!alive(mine) || mine.pl !== pl) return;
      if (pl.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      pl.pollErrors = 0;
      const wasActive = isActive(pl.job);
      adoptPolishJob(mine, data.job, mine.detail.path);
      if (wasActive && !isActive(pl.job)) {
        if (pl.job.status === "failed" || pl.job.status === "stopped") markHandled(pl.job.id);
        renderPolishBar(mine);
        renderPolishBody(mine);
        if (pl.pv.heading) pl.pv.heading.focus();
      } else {
        renderPolishBar(mine);
      }
    } catch (e) {
      if (!alive(mine) || mine.pl !== pl || ignorable(e)) return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.ctx.toast("작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 다듬어 주세요", "error");
        pl.job = null;
        renderPolishBar(mine);
        return;
      }
      pl.pollErrors += 1;
      if (pl.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (alive(mine) && mine.pl === pl && isActive(pl.job)) schedulePolishPoll();
  }, pollDelay(pl.pollErrors));
}

// ---- polish: preview ----------------------------------------------------------

function issueList(issues) {
  const list = Array.isArray(issues) ? issues : [];
  return list.length
    ? h("ul", { class: "pol-issues" }, ...list.map((t) => h("li", {}, String(t))))
    : h("p", { class: "muted small" }, "지적 없음");
}

function syncPicked(cards, picked) {
  cards.forEach((c, i) => {
    c.root.classList.toggle("is-picked", i === picked);
    c.field.disabled = i !== picked;
    c.radio.checked = i === picked;
  });
}

function polishGroup(legendText, cards, picked, note) {
  syncPicked(cards, picked);
  return h("fieldset", { class: "pol-group" }, h("legend", {}, legendText), note, ...cards.map((c) => c.root));
}

function hookTimingText(lint, hookSec) {
  const parts = [];
  if (hookSec !== null) parts.push(`훅 구간 ${hookSec}초`);
  if (num(lint.speak_sec) > 0) parts.push(`말하면 약 ${round1(num(lint.speak_sec))}초`);
  if (num(lint.read_sec) > 0) parts.push(`자막 읽기 약 ${round1(num(lint.read_sec))}초`);
  return parts.join(" · ") + (hookSec === null ? " (기획안에 훅 구간이 없습니다)" : "");
}

function hookSecOf(pl) {
  const hook = pl.context.hook;
  return hook ? round2(num(hook.end) - num(hook.start)) : null;
}

function paintHookCard(card, lint, hookSec) {
  card.score.textContent = `점수 ${num(lint.score)}`;
  card.timing.className = lint.fits === false ? "warn-text small" : "muted small";
  card.timing.textContent = hookTimingText(lint, hookSec);
  fill(card.issues, issueList(lint.issues));
}

function hookCards(pl) {
  const { result, edit: e } = pl;
  const hookSec = hookSecOf(pl);
  const cards = result.hooks.map((cand, i) => {
    const rank = i + 1;
    const radio = h("input", { type: "radio", name: "polish-hook", value: String(i) });
    const field = h("textarea", { rows: "4", maxlength: String(MAX_HOOK_SCRIPT), "aria-label": `${rank}위 훅 대본` });
    field.value = e.hooks[i];
    field.addEventListener("input", () => {
      e.hooks[i] = field.value; pl.dirty = true; updatePolishActions(S); scheduleRelint(S, "hook");
    });
    const score = h("span", { class: "badge pol-score" });
    const timing = h("p", {});
    const issues = h("div", {});
    const root = h("div", { class: "pol-cand" },
      h("label", { class: "pol-pick" }, radio, h("span", {}, `${rank}위 · ${cand.formula_name || cand.formula}`), score),
      field, timing, issues,
      cand.evidence ? h("p", { class: "muted small" }, String(cand.evidence)) : null);
    const card = { root, field, radio, score, timing, issues };
    paintHookCard(card, cand.lint || {}, hookSec);
    return card;
  });
  cards.forEach((card, i) => card.radio.addEventListener("change", () => {
    if (card.radio.checked) { e.hook = i; syncPicked(cards, i); scheduleRelint(S, "hook"); }
  }));
  return cards;
}

function cutBadges(lint) {
  const badges = [lint.mobile_cut ? h("span", { class: "badge pol-cut" }, "모바일 잘림") : null,
    lint.desktop_cut ? h("span", { class: "badge pol-cut" }, "데스크톱 잘림") : null];
  if (!lint.mobile_cut && !lint.desktop_cut) badges.push(h("span", { class: "badge" }, "잘림 없음"));
  return badges;
}

function paintTitleCard(card, lint) {
  card.score.textContent = `점수 ${num(lint.score)}`;
  fill(card.cuts, ...cutBadges(lint));
  fill(card.issues, issueList(lint.issues));
}

function titleCards(pl) {
  const { result, edit: e } = pl;
  const cards = result.titles.map((cand, i) => {
    const rank = i + 1;
    const radio = h("input", { type: "radio", name: "polish-title", value: String(i) });
    const field = h("input", { type: "text", maxlength: String(MAX_POLISH_TITLE), "aria-label": `${rank}위 제목` });
    field.value = e.titles[i];
    field.addEventListener("input", () => {
      e.titles[i] = field.value; pl.dirty = true; updatePolishActions(S); scheduleRelint(S, "tt");
    });
    const score = h("span", { class: "badge pol-score" });
    const cuts = h("span", {});
    const issues = h("div", {});
    const root = h("div", { class: "pol-cand" },
      h("label", { class: "pol-pick" }, radio, h("span", {}, `${rank}위`), score, cuts),
      field, issues,
      cand.evidence ? h("p", { class: "muted small" }, String(cand.evidence)) : null);
    const card = { root, field, radio, score, cuts, issues };
    paintTitleCard(card, cand.lint || {});
    return card;
  });
  cards.forEach((card, i) => card.radio.addEventListener("change", () => {
    if (card.radio.checked) { e.title = i; syncPicked(cards, i); scheduleRelint(S, "tt"); }
  }));
  return cards;
}

// ---- polish: re-lint edits (no Claude) ---------------------------------------------
// Edits to the picked hook / title / thumbnail words, and a changed pick, are re-scored by the server after a short pause.
// Each key keeps a sequence number so a slow, older answer never overwrites a newer one.

function scheduleRelint(mine, key) {
  const pl = mine && mine.pl;
  if (!pl || !pl.result || !pl.edit) return;
  pl.lintPending.add(key);
  if (pl.lintTimer) clearTimeout(pl.lintTimer);
  pl.lintTimer = setTimeout(() => { pl.lintTimer = null; runRelint(mine, pl); }, RELINT_DELAY);
}

async function runRelint(mine, pl) {
  if (!alive(mine) || mine.pl !== pl || !pl.result || !pl.edit || !pl.lintPending.size) return;
  const keys = new Set(pl.lintPending);
  pl.lintPending.clear();
  const e = pl.edit;
  const body = { draft: mine.detail.path };
  if (keys.has("hook")) body.hook_script = e.hooks[e.hook];
  if (keys.has("tt")) {
    body.title = e.titles[e.title];
    body.thumb_words = e.words.map((w) => w.trim()).filter(Boolean);
  }
  const stamps = {};
  for (const k of keys) stamps[k] = ++pl.lintSeq[k];
  const pv = pl.pv;
  const picked = { hook: e.hook, title: e.title };
  try {
    const res = await mine.ctx.api("POST", "/api/polish/lint", body);
    if (!alive(mine) || mine.pl !== pl || pl.pv !== pv) return;
    if (res.hook && stamps.hook === pl.lintSeq.hook && pv.hooks[picked.hook]) {
      paintHookCard(pv.hooks[picked.hook], res.hook, hookSecOf(pl));
    }
    if (stamps.tt === pl.lintSeq.tt) {
      if (res.title && pv.titles[picked.title]) paintTitleCard(pv.titles[picked.title], res.title);
      if (res.thumbnail && pv.thumbIssues) fill(pv.thumbIssues, issueList(res.thumbnail.issues));
    }
    pl.lintFailed = false;
  } catch (err) {
    if (!alive(mine) || mine.pl !== pl || pl.pv !== pv || ignorable(err)) return;
    pl.lintFailed = true;
  }
  if (pv.lintNote) pv.lintNote.textContent = pl.lintFailed ? "점수를 다시 계산하지 못했습니다" : "";
}

function thumbnailView(pl) {
  const e = pl.edit;
  const touch = () => { pl.dirty = true; updatePolishActions(S); };
  const issues = h("div", {}, issueList(pl.result.thumbnail.lint && pl.result.thumbnail.lint.issues));
  pl.thumbIssuesNode = issues;
  const lintOver = !!(pl.result.thumbnail.lint && pl.result.thumbnail.lint.over_limit);
  const note = h("p", { class: "warn-text small pol-words-note", id: "polish-words-note" });
  let edited = false;
  const syncNote = () => {
    const multi = e.words.some((w) => w.trim().split(/\s+/).filter(Boolean).length > 1);
    const text = multi || (lintOver && !edited)
      ? "한 칸에 낱말이 여러 개이거나 문구가 3단어를 넘습니다. 썸네일 문구는 3단어 이하가 좋습니다" : "";
    if (note.textContent !== text) note.textContent = text;
  };
  const words = e.words.map((w, i) => {
    const input = h("input", { type: "text", maxlength: "40", "aria-label": `썸네일 문구 ${i + 1}`, "aria-describedby": "polish-words-note" });
    input.value = w;
    input.addEventListener("input", () => { e.words[i] = input.value; edited = true; syncNote(); touch(); scheduleRelint(S, "tt"); });
    return input;
  });
  syncNote();
  const area = (key, label, id) => {
    const t = h("textarea", { rows: "2", id, maxlength: String(MAX_THUMB_TEXT) });
    t.value = e[key];
    t.addEventListener("input", () => { e[key] = t.value; touch(); });
    return h("div", { class: "field" }, h("label", { for: id }, label), t);
  };
  return h("section", { class: "pol-thumb", "aria-labelledby": "polish-thumb-title" },
    h("h3", { id: "polish-thumb-title" }, "썸네일 지시서"),
    h("fieldset", { class: "pol-group" }, h("legend", {}, `문구 (낱말 최대 ${THUMB_SLOTS}칸)`),
      h("div", { class: "pol-words" }, ...words), note),
    area("expression", "표정", "polish-expression"),
    area("framing", "구도", "polish-framing"),
    area("background", "배경", "polish-background"),
    issues);
}

function nowView(c) {
  const script = h("div", { class: "md-text" });
  script.textContent = c.hook ? String(c.hook.script || "") : "훅 구간이 없습니다";
  return h("section", { class: "pol-now", "aria-labelledby": "polish-now-title" },
    h("h3", { id: "polish-now-title" }, "지금 기획안"),
    h("p", { class: "muted small" }, "제목"), h("p", {}, String(c.title || "")),
    h("p", { class: "muted small" }, "훅 대본"), script);
}

function polishPreview(mine) {
  const pl = mine.pl;
  const c = pl.context;
  const hooks = hookCards(pl);
  const titles = titleCards(pl);
  const pv = { hooks, titles, lintNote: h("p", { class: "muted small", id: "polish-lint-note" }) };
  pl.pv = pv;
  pv.heading = h("h2", { id: "polish-preview-title", tabindex: "-1" }, "훅·제목·썸네일 후보");
  pv.saveBtn = h("button", { type: "button", class: "btn btn-primary", "aria-describedby": "polish-save-hint" },
    "저장(기획안에 반영)");
  pv.rerunBtn = h("button", { type: "button", class: "btn" }, "다시 다듬기");
  pv.closeBtn = h("button", { type: "button", class: "btn" }, "닫기");
  pv.saveBtn.addEventListener("click", savePolish);
  pv.rerunBtn.addEventListener("click", () => requestPolish("preview"));
  pv.closeBtn.addEventListener("click", askClosePolish);
  pv.hint = h("p", { class: "muted hint", id: "polish-save-hint" });
  pv.alerts = h("div", { class: "preview-alerts" });
  pl.alertSig = null;
  const thumbView = thumbnailView(pl);
  pv.thumbIssues = pl.thumbIssuesNode;
  return h("section", { class: "motif-sec pol-preview", "aria-labelledby": "polish-preview-title" },
    pv.heading,
    h("p", { class: "muted small" }, "저장하기 전에는 기획안 파일이 바뀌지 않습니다. 후보는 점수 순서이고, 고른 것만 고칠 수 있습니다."),
    nowView(c),
    c.conti_exists
      ? h("p", { class: "hint", role: "note" }, "저장해도 콘티는 이전 훅 그대로입니다. 콘티 화면에서 다시 만들면 반영됩니다")
      : null,
    polishGroup("훅", hooks, pl.edit.hook),
    polishGroup("제목", titles, pl.edit.title, null),
    thumbView,
    pv.lintNote,
    h("div", { class: "preview-actions" },
      h("div", { class: "btn-row" }, pv.saveBtn, pv.rerunBtn, pv.closeBtn), pv.hint, pv.alerts));
}

function renderPolishBody(mine) {
  const box = mine.nodes.plBody;
  const pl = mine.pl;
  if (!box) return;
  if (!pl.result || !pl.edit) { pl.pv = {}; fill(box); return; }
  fill(box, polishPreview(mine));
  updatePolishActions(mine);
  renderPolishAlerts(mine);
  if (pl.dirty) {                    // a rebuilt preview shows the server's first scores: re-score the edits
    scheduleRelint(mine, "hook");
    scheduleRelint(mine, "tt");
  }
}

// Why save is disabled (empty string when it can be used).
function polishBlocker(pl) {
  const e = pl.edit;
  if (pl.conflict) return "기획안이 바뀌어 이 후보로는 저장할 수 없습니다";
  if (!e.titles[e.title].trim()) return "제목이 비어 있습니다";
  if (!e.hooks[e.hook].trim()) return "훅 대본이 비어 있습니다";
  if (!e.words.some((w) => w.trim())) return "썸네일 문구를 한 칸 이상 적어 주세요";
  if (!e.expression.trim() || !e.framing.trim() || !e.background.trim()) return "썸네일의 표정·구도·배경을 비우지 마세요";
  return "";
}

function updatePolishActions(mine) {
  const pl = mine && mine.pl;
  if (!pl || !pl.result || !pl.pv.saveBtn) return;
  const reason = polishBlocker(pl);
  pl.pv.saveBtn.disabled = pl.saving || pl.running || isActive(pl.job) || !!reason;
  pl.pv.rerunBtn.disabled = pl.saving || pl.running || isActive(pl.job);
  pl.pv.closeBtn.disabled = pl.saving;
  pl.pv.hint.textContent = reason;
}

function polishChoice(pl) {
  const e = pl.edit;
  const hook = pl.result.hooks[e.hook];
  return {
    title: e.titles[e.title].trim(),
    hook_script: e.hooks[e.hook].trim(),
    hook_formula: hook.formula,
    hook_type: hook.hook_type,
    thumbnail: {
      words: e.words.map((w) => w.trim()).filter(Boolean),
      expression: e.expression.trim(), framing: e.framing.trim(), background: e.background.trim(),
    },
  };
}

// Alerts live in their own node and are rebuilt only when their content changes.
function renderPolishAlerts(mine) {
  const pl = mine.pl;
  const box = pl.pv && pl.pv.alerts;
  if (!box || !pl.result) return;
  const sig = JSON.stringify([pl.saveError, pl.confirmClose, pl.saving, pl.confirmRerun === "preview", pl.conflict, pl.refreshed]);
  if (sig === pl.alertSig) return;
  pl.alertSig = sig;
  const kids = [];
  if (pl.confirmClose) {
    const yes = h("button", { type: "button", class: "btn btn-danger", "data-polish-close-yes": "", disabled: pl.saving }, "닫기");
    const no = h("button", { type: "button", class: "btn" }, "취소");
    yes.addEventListener("click", closePolish);
    no.addEventListener("click", () => { pl.confirmClose = false; renderPolishAlerts(mine); pl.pv.closeBtn.focus(); });
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" }, "후보를 닫습니다. 고친 내용은 사라지고 기획안 파일은 그대로입니다."),
      h("div", { class: "btn-row" }, yes, no)));
  }
  if (pl.confirmRerun === "preview") kids.push(polishRerunConfirm("preview"));
  if (pl.conflict) {
    const reload = h("button", { type: "button", class: "btn", "data-polish-reload": "" }, "다시 불러오기");
    reload.addEventListener("click", () => refreshAfterConflict(mine));
    kids.push(h("div", { class: "inline-confirm", role: "alert" },
      h("p", { class: "warn-text" },
        "후보를 만든 뒤 기획안 파일이 바뀌어 저장하지 않았습니다. 고친 입력은 이 화면에 그대로 남아 있습니다. 새 기획안을 불러오면 최신 기획안에 이 선택을 저장할 수 있습니다."),
      h("div", { class: "btn-row" }, reload)));
  }
  if (pl.refreshed) {
    kids.push(h("p", { class: "hint", role: "note" },
      "최신 기획안 기준으로 저장합니다. 그 사이 바뀐 제목·훅은 이번 선택으로 덮어써집니다."));
  }
  if (pl.saveError) kids.push(h("p", { class: "error", role: "alert" }, pl.saveError));
  fill(box, ...kids);
}

// After draft_changed: take the new hash, title and hook from the file, keep the typed edits, and let the user save again.
async function refreshAfterConflict(mine) {
  const pl = mine.pl;
  try {
    const d = await mine.ctx.api("GET", `/api/draft?path=${encodeURIComponent(mine.detail.path)}`);
    if (!alive(mine) || mine.pl !== pl) return;
    pl.context = { ...pl.context, hash: d.hash, title: d.title, hook: d.hook ? { ...pl.context.hook, ...d.hook } : null };
    pl.conflict = false;
    pl.refreshed = true;
    pl.saveError = null;
    pl.alertSig = null;
    renderView(mine, d);
    if (pl.pv.heading) pl.pv.heading.focus();
  } catch (e) {
    if (!alive(mine) || mine.pl !== pl || ignorable(e)) return;
    pl.saveError = e.message || "기획안을 불러오지 못했습니다";
    pl.alertSig = null;
    renderPolishAlerts(mine);
  }
}

async function savePolish() {
  const mine = S;
  const pl = mine.pl;
  if (pl.saving || pl.running || isActive(pl.job) || !pl.result || !pl.edit || polishBlocker(pl)) return;
  pl.saving = true;
  pl.saveError = null;
  pl.alertSig = null;
  pl.barSig = null;
  updatePolishActions(mine);
  renderPolishAlerts(mine);
  renderPolishBar(mine);
  const jobId = pl.jobId;
  let saved = false;
  try {
    const res = await mine.ctx.api("POST", "/api/polish/save", {
      draft: mine.detail.path, hash: pl.context.hash, choice: polishChoice(pl), job_id: jobId,
    });
    markHandled(jobId);                          // before the alive check: a finished save is never restored as a preview
    if (!alive(mine)) return;
    mine.plWarnings = Array.isArray(res.warnings) ? res.warnings.map(String) : [];
    mine.pl = freshPolish();
    mine.ctx.toast("기획안에 반영했습니다", "info");
    saved = true;
  } catch (e) {
    // An aborted request (page left mid-save) may still have been saved by the server: never restore that preview.
    if (e && e.error === "aborted") markHandled(jobId);
    if (!alive(mine) || ignorable(e)) return;
    if (e.error === "draft_changed") { pl.conflict = true; pl.refreshed = false; }
    else pl.saveError = e.message || "저장하지 못했습니다";
    pl.saving = false;
    pl.alertSig = null;
    pl.barSig = null;
    updatePolishActions(mine);
    renderPolishAlerts(mine);
    renderPolishBar(mine);
  }
  if (saved) {
    await loadView(mine);                       // title and body come from the saved file
    if (alive(mine)) focusStatus();
  }
}

function askClosePolish() {
  const pl = S.pl;
  if (pl.saving) return;
  pl.confirmClose = true;
  renderPolishAlerts(S);
  const yes = pl.pv.alerts && pl.pv.alerts.querySelector("[data-polish-close-yes]");
  if (yes) yes.focus();
}

function closePolish() {
  const mine = S;
  if (mine.pl.saving) return;
  markHandled(mine.pl.jobId);
  mine.pl = freshPolish();
  mine.ctx.toast("후보를 닫았습니다. 기획안은 바뀌지 않았습니다", "info");
  renderPolishBar(mine);
  renderPolishBody(mine);
  if (mine.pl.nodes.runBtn) mine.pl.nodes.runBtn.focus();
}

// ---- page contract ------------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  S = freshState(el, ctx, ctx.category, params || {});
  await load();
}

// A draft (or an unsaved preview) belongs to one category: leave for the list of the new category.
export function onCategory(category) {
  if (!S || S.category === category) return;
  stopPolling();
  if (S.mode === "list") {
    S = freshState(S.el, S.ctx, category, { mode: "list" });
    load();
    return;
  }
  location.hash = "#/draft";      // hashchange remounts the list page
}

export function unmount() {
  stopPolling();
  S = null;
}
