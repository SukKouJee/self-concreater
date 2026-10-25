// 레퍼런스 추가 화면: 링크 입력 → 1초 폴링 진행 표시 → 영상별 확인 카드 → 마무리 요약.
// 서버 작업(kind "collect")이 이미 있으면 화면에 들어올 때 그 작업을 이어서 보여 준다.
import { h, filesUrl } from "/static/dom.js";
import { fmtDur, fmtClock, fmtRemain, spokenDur, rowView, itemRemaining, monotonicFraction } from "/static/progress-utils.js";

const POLL_MS = 1000;
const LABELS = ["hook", "intro", "body", "demo", "transition", "cta", "outro"];
const STEPS = [
  { key: "fetch", name: "자막" },
  { key: "cuts", name: "컷" },
  { key: "analyze", name: "분석" },
  { key: "confirm", name: "확인" },
];
const ROW_NAMES = { fetch: "자막", cuts: "컷", analyze: "분석", shots: "샷 분류" };
const CLOCK_MS = 1000;
const STATE_TEXT = {
  waiting: "대기", running: "진행 중", awaiting: "확인 필요", saved: "저장됨", skipped: "건너뜀", failed: "실패",
};
const ACTIVE = new Set(["running", "awaiting"]);
const FINISHED_TEXT = { done: "작업을 마쳤습니다", stopped: "작업이 중단되었습니다", failed: "작업이 실패했습니다" };

// ---- helpers ----------------------------------------------------------------

function fmtTime(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function itemLabel(item) {
  return item.title || item.url || item.id;
}

function isActive(job) {
  return !!job && ACTIVE.has(job.status);
}

// ---- page state (reset on every mount) ---------------------------------------

let S = null;

function freshState(el, ctx) {
  return {
    el, ctx, timer: null, clock: null, offset: 0, job: null, view: null,
    rows: new Map(),       // item key -> { li, sig }
    edits: new Map(),      // item id -> edited analysis (deep copy of preview)
    openFeedback: new Set(), // item ids whose feedback input is open
    feedbackText: new Map(),
    busy: new Map(),       // item id -> preview signature at decision time (cleared once the item moves on)
    draft: { role: "ref", links: "", shots: false, force: false, refetch: false },
    category: ctx.category, names: [],
  };
}

function stopPolling() {
  if (S && S.timer) { clearTimeout(S.timer); S.timer = null; }
}

function stopClock() {
  if (S && S.clock) { clearInterval(S.clock); S.clock = null; }
}

// Browser time corrected by the server clock offset (refreshed on every poll).
function nowSec() {
  return Date.now() / 1000 + S.offset;
}

function startClock() {
  if (S.clock) return;
  const mine = S;
  mine.clock = setInterval(() => {
    if (S !== mine || !mine.ctx.isCurrent() || !isActive(mine.job)) { clearInterval(mine.clock); mine.clock = null; return; }
    tickClock();
  }, CLOCK_MS);
}

// Updates only text and bar widths of the step rows; cards are never rebuilt here.
function tickClock() {
  if (!S.job) return;
  S.job.items.forEach((item, idx) => {
    const row = S.rows.get(String(idx));
    if (row && row.li._tick) row.li._tick(item);
  });
}

// ---- API --------------------------------------------------------------------

async function fetchJob(id) {
  const data = await S.ctx.api("GET", `/api/jobs/${encodeURIComponent(id)}`);
  return data.job;
}

const POLL_MAX_MS = 15000;

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function schedulePoll() {
  stopPolling();
  const mine = S;
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    if (S !== mine || !mine.ctx.isCurrent()) return;
    try {
      const job = await fetchJob(mine.job.id);
      if (S !== mine || !mine.ctx.isCurrent()) return;
      if (mine.pollErrors) mine.ctx.toast("다시 연결되었습니다", "info");
      mine.pollErrors = 0;
      applyJob(job);
    } catch (e) {
      if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        // Server restarted and forgot the job: stop polling and fall back to whatever the server has now.
        mine.ctx.toast("작업을 찾을 수 없어 처음 화면으로 돌아갑니다", "error");
        mine.job = null;
        mine.pollErrors = 0;
        renderForm();
        await resume();
        return;
      }
      mine.pollErrors = (mine.pollErrors || 0) + 1;
      if (mine.pollErrors === 1) mine.ctx.toast("진행 상황을 가져오지 못하고 있습니다. 계속 다시 시도합니다", "error");
    }
    if (S === mine && mine.ctx.isCurrent() && isActive(mine.job)) schedulePoll();
  }, pollDelay(mine.pollErrors || 0));
}

async function startJob(path, body) {
  const mine = S;
  try {
    const data = await mine.ctx.api("POST", path, body);
    if (S !== mine || !mine.ctx.isCurrent()) return;
    mine.rows.clear();
    mine.edits.clear();
    mine.openFeedback.clear();
    mine.feedbackText.clear();
    mine.pollErrors = 0;
    applyJob(data.job);
    if (isActive(mine.job)) schedulePoll();
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent()) return;
    if (e.error === "job_running") {
      mine.ctx.toast("이미 진행 중인 작업이 있습니다. 이어서 보여 드립니다", "error");
      await resume();
    } else if (e.error !== "aborted") {
      mine.ctx.toast(e.message, "error");
    }
  }
}

async function resume() {
  const mine = S;
  try {
    const data = await mine.ctx.api("GET", "/api/jobs/current?kind=collect");
    if (S !== mine || !mine.ctx.isCurrent()) return;
    if (data.job) {
      applyJob(data.job);
      if (isActive(data.job)) schedulePoll();
    }
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    mine.ctx.toast(e.message, "error");
  }
}

async function decide(item, payload) {
  const mine = S;
  const key = item.id;
  if (mine.busy.has(key)) return;
  mine.busy.set(key, JSON.stringify(item.preview));
  renderItem(item);
  try {
    const data = await mine.ctx.api(
      "POST",
      `/api/jobs/${encodeURIComponent(mine.job.id)}/items/${encodeURIComponent(item.id)}/decision`,
      payload,
    );
    if (S !== mine || !mine.ctx.isCurrent()) return;
    if (payload.action === "reanalyze") {
      mine.openFeedback.delete(key);
      mine.feedbackText.delete(key);
    }
    applyJob(data.job);
    if (isActive(mine.job) && !mine.timer) schedulePoll();
  } catch (e) {
    if (S !== mine || !mine.ctx.isCurrent() || e.error === "aborted") return;
    mine.busy.delete(key);
    mine.ctx.toast(e.message, "error");
    // 서버 상태를 다시 읽어 카드가 낡았으면 정리한다
    try {
      const job = await fetchJob(mine.job.id);
      if (S !== mine || !mine.ctx.isCurrent()) return;
      applyJob(job);
    } catch { /* keep the card as is */ }
    if (S === mine && mine.ctx.isCurrent()) {
      const cur = mine.job.items.find((i) => i.id === key);
      if (cur) renderItem(cur);
    }
  }
}

// ---- rendering: entry -------------------------------------------------------

function applyJob(job) {
  S.job = job;
  if (typeof job.now === "number") S.offset = job.now - Date.now() / 1000;
  renderJobView();
}

function setView(name) {
  if (S.view === name) return false;
  S.view = name;
  S.el.replaceChildren();
  S.rows.clear();
  return true;
}

function renderJobView() {
  const job = S.job;
  if (!job) { renderForm(); return; }
  const fresh = setView("job");
  if (isActive(job)) startClock(); else stopClock();
  if (fresh) {
    S.el.append(h("h1", {}, "레퍼런스 추가"));
    S.head = h("div", { class: "job-head" });
    S.list = h("ul", { class: "job-items", "aria-label": "영상별 진행 상황" });
    S.foot = h("div", { class: "job-foot" });
    S.live = h("div", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
    S.lastAnnounce = "";
    S.el.append(S.live, S.head, S.list, S.foot);
  }
  renderJobHead(job);
  syncItems(job);
  tickClock();
  renderJobFoot(job);
  announce(job);
}

function announce(job) {
  let text;
  if (!isActive(job)) text = `작업이 끝났습니다. ${FINISHED_TEXT[job.status] || ""}`.trim();
  else {
    const idx = job.items.findIndex((i) => i.state === "awaiting");
    text = idx >= 0 ? `${idx + 1}번째 영상이 확인을 기다립니다` : `${job.items.filter((i) => i.state === "saved").length}개 저장됨`;
  }
  if (text !== S.lastAnnounce) { S.lastAnnounce = text; S.live.textContent = text; }
}

function renderJobHead(job) {
  const total = job.items.length;
  const doneCount = job.items.filter((i) => !ACTIVE.has(i.state) && i.state !== "waiting").length;
  const active = isActive(job);
  const text = active
    ? `${job.category || ""} · ${doneCount}/${total}개 처리`
    : `${job.category || ""} · ${FINISHED_TEXT[job.status] || "끝났습니다"}`;
  const head = h("div", { class: "job-title" },
    h("h2", {}, active ? "분석 진행 중" : "분석 끝"),
    h("span", { class: "muted" }, text));
  S.head.replaceChildren(head);
}

function renderJobFoot(job) {
  if (isActive(job)) { S.foot.replaceChildren(); return; }
  const sum = job.summary;
  const box = h("section", { class: "card done-card", "aria-label": "마무리" });
  box.append(h("h2", {}, FINISHED_TEXT[job.status] || "끝났습니다"));
  if (job.message) box.append(h("p", { class: job.status === "done" ? "muted" : "error" }, job.message));
  if (sum) {
    box.append(h("p", { class: "counts" },
      h("span", { class: "count count-ok" }, `저장 ${sum.saved}`),
      h("span", { class: "count" }, `건너뜀 ${sum.skipped}`),
      h("span", { class: "count count-bad" }, `실패 ${sum.failed}`)));
    const lines = (sum.stage_text || "").split("\n").map((l) => l.replace(/^\s*-\s*/, "").trim()).filter(Boolean);
    if (lines.length) box.append(h("ul", { class: "stage-notes" }, ...lines.map((l) => h("li", {}, l))));
  }
  box.append(h("p", { class: "muted" }, "분석이 틀리면 md를 고치고 ", h("code", {}, "reviewed: true"), "로 바꿔 두세요."));
  const again = h("button", { type: "button", class: "btn btn-primary" }, "새로 추가");
  again.addEventListener("click", () => {
    stopPolling();
    stopClock();
    S.job = null;
    S.rows.clear();
    S.edits.clear();
    renderForm();
    const first = S.el.querySelector("textarea");
    if (first) first.focus();
  });
  box.append(again);
  S.foot.replaceChildren(box);
}

// ---- rendering: items -------------------------------------------------------

// Structure only (row set and each row start; state and pause are repainted by the clock). Fractions, texts and estimates are patched in place by the clock.
function timingSig(timing) {
  if (!timing || !timing.steps) return null;
  return Object.entries(timing.steps).map(([k, r]) => [k, r.started_at ?? null]);
}

function itemSig(item) {
  return JSON.stringify([item.id, item.title, item.step, item.state, item.message, item.ask,
    JSON.stringify(item.preview), (item.frames || []).length, S.busy.has(item.id), S.openFeedback.has(item.id),
    timingSig(item.timing)]);
}

function releaseBusy(job) {
  for (const item of job.items) {
    if (!S.busy.has(item.id)) continue;
    if (item.state !== "awaiting" || JSON.stringify(item.preview) !== S.busy.get(item.id)) S.busy.delete(item.id);
  }
}

function syncItems(job) {
  releaseBusy(job);
  const seen = new Set();
  job.items.forEach((item, idx) => {
    const key = String(idx);
    seen.add(key);
    const sig = itemSig(item);
    const row = S.rows.get(key);
    if (row && row.sig === sig) return;
    const li = buildItem(item);
    if (row) row.li.replaceWith(li);
    else S.list.append(li);
    S.rows.set(key, { li, sig, id: item.id });
  });
  for (const [key, row] of [...S.rows]) {
    if (!seen.has(key)) { row.li.remove(); S.rows.delete(key); }
  }
}

function renderItem(item) {
  const idx = S.job.items.findIndex((i) => i.id === item.id);
  if (idx < 0) return;
  const key = String(idx);
  const row = S.rows.get(key);
  const li = buildItem(item);
  if (row) { row.li.replaceWith(li); row.li = li; row.sig = itemSig(item); }
}

function stepChips(item) {
  const ol = h("ol", { class: "chips", "aria-label": "단계" });
  const idx = STEPS.findIndex((s) => s.key === item.step);
  const finished = item.state === "saved" || item.step === "done";
  if (item.step === "shots") {
    ol.append(h("li", { class: "chip chip-current", "aria-current": "step" }, "샷 분류"));
    return ol;
  }
  STEPS.forEach((s, i) => {
    let cls = "chip";
    let note = "";
    if (finished || i < idx) { cls += " chip-done"; note = " (완료)"; }
    else if (i === idx) { cls += " chip-current"; note = " (지금)"; }
    const li = h("li", { class: cls, "aria-current": i === idx && !finished ? "step" : false }, s.name);
    if (note) li.append(h("span", { class: "sr-only" }, note));
    ol.append(li);
  });
  return ol;
}

// ---- step rows (progress bars, remaining time) --------------------------------

function pctOf(row) {
  return typeof row.fraction === "number" ? Math.round(Math.min(1, Math.max(0, row.fraction)) * 100) : null;
}

function rowText(row, v) {
  const exp = v.expected;
  const hasExp = exp !== null && exp !== undefined;
  switch (row.state) {
    case "done":
      return `✓ ${fmtDur(v.elapsed)} 걸림${hasExp ? ` (예상 ${fmtDur(exp)})` : ""}`;
    case "failed":
      return v.elapsed !== null ? `실패 ${fmtDur(v.elapsed)}` : "실패";
    case "skipped":
      return "건너뜀";
    case "running": {
      const p = pctOf(row);
      const pct = p === null ? "" : `${p}% · `;
      return hasExp ? `${pct}${fmtDur(v.elapsed)} / 예상 ${fmtDur(exp)}` : `${pct}${fmtDur(v.elapsed)} · 예상 계산 중`;
    }
    default:
      return hasExp ? `예상 ${fmtDur(exp)}` : "예상 계산 중";
  }
}

function buildStepRow(key) {
  const name = h("span", { class: "step-name" }, ROW_NAMES[key] || key);
  const time = h("span", { class: "step-time" });
  const fill = h("span", { class: "bar-fill" });
  const bar = h("span", { class: "bar" }, fill);
  const sub = h("div", { class: "step-sub muted", hidden: true });
  const li = h("li", { class: "step-row" }, h("div", { class: "step-main" }, name, bar, time), sub);
  return { li, bar, fill, time, sub, maxFrac: null };
}

function paintStepRow(r, key, row, nowS, frozen) {
  const v = rowView(row, nowS);
  const running = row.state === "running";
  r.li.className = `step-row step-${row.state}${running && v.over ? " is-over" : ""}`;
  r.time.textContent = rowText(row, v);
  // The shown bar never moves backward (estimate-based value -> measured fraction, or a grown estimate).
  if (running) r.maxFrac = monotonicFraction(r.maxFrac, v.fraction);
  const shown = running ? r.maxFrac : v.fraction;
  r.fill.style.width = `${shown === null ? 0 : Math.round(shown * 100)}%`;
  r.bar.classList.toggle("bar-indet", running && shown === null && !frozen && row.paused_at == null);
  if (!running) {
    for (const a of ["role", "aria-label", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"]) r.bar.removeAttribute(a);
    r.bar.setAttribute("aria-hidden", "true");
    r.sub.hidden = true;
    r.sub.textContent = "";
    return;
  }
  r.bar.removeAttribute("aria-hidden");
  r.bar.setAttribute("role", "progressbar");
  r.bar.setAttribute("aria-label", `${ROW_NAMES[key] || key} 진행률`);
  r.bar.setAttribute("aria-valuemin", "0");
  r.bar.setAttribute("aria-valuemax", "100");
  const p = pctOf(row);
  if (p !== null) {
    r.bar.setAttribute("aria-valuenow", String(p));
    r.bar.removeAttribute("aria-valuetext");
  } else {
    r.bar.removeAttribute("aria-valuenow");
    r.bar.setAttribute("aria-valuetext", v.expected !== null
      ? `${spokenDur(v.elapsed)} 경과, 예상 ${spokenDur(v.expected)}`
      : `${spokenDur(v.elapsed)} 경과`);
  }
  const parts = [];
  if (row.phase_text) parts.push(row.phase_text);
  if (!frozen) {
    if (v.over) parts.push("예상보다 길어지는 중", "곧 끝날 예정");
    else if (v.remaining !== null) parts.push(`${fmtRemain(v.remaining, false)} 남음`, `${fmtClock(nowS + v.remaining)}쯤 끝`);
  }
  r.sub.textContent = parts.join(" · ");
  r.sub.hidden = parts.length === 0;
}

// Builds the step list once per structural change; card._tick(item) patches times and widths in place.
function stepRows(card, item) {
  const keys = Object.keys(item.timing.steps);
  const ol = h("ol", { class: "steps", "aria-label": "단계" });
  const rows = new Map(keys.map((k) => [k, buildStepRow(k)]));
  rows.forEach((r) => ol.append(r.li));
  const total = h("p", { class: "step-total muted", hidden: true });
  card._tick = (cur) => {
    const timing = cur.timing;
    if (!timing || !timing.steps) return;
    const waiting = cur.state === "awaiting";
    // While the user decides, the clock stays at the last server snapshot.
    const nowS = waiting && S.job && typeof S.job.now === "number" ? S.job.now : nowSec();
    for (const [k, r] of rows) {
      const row = timing.steps[k];
      if (row) paintStepRow(r, k, row, nowS, waiting);
    }
    if (waiting) {
      total.textContent = "확인을 기다리는 중";
      total.hidden = false;
      return;
    }
    const rem = cur.state === "running" ? itemRemaining(timing, nowS) : null;
    total.hidden = rem === null;
    if (rem !== null) total.textContent = `이 영상 전체: ${fmtRemain(rem, true)} 남음 · 예상 완료 ${fmtClock(nowS + rem)}`;
  };
  card._tick(item);
  return h("div", { class: "step-block" }, ol, total);
}

function buildItem(item) {
  const li = h("li", { class: `job-item is-${item.state}` });
  const line = h("div", { class: "item-line" });
  line.append(
    h("span", { class: "item-title", title: item.url || "" }, itemLabel(item)),
    h("span", { class: `state state-${item.state}` }, STATE_TEXT[item.state] || item.state),
  );
  li.append(line);
  if (item.timing && item.timing.steps) li.append(stepRows(li, item));
  else if (item.state === "running" || item.state === "awaiting" || item.state === "waiting") li.append(stepChips(item));
  if (item.message) li.append(h("p", { class: item.state === "failed" ? "error item-msg" : "muted item-msg" }, item.message));
  if (item.state === "awaiting") {
    if (item.ask === "no_transcript") li.append(noTranscriptCard(item));
    else if (item.preview) li.append(confirmCard(item));
  }
  return li;
}

// ---- no transcript ----------------------------------------------------------

function noTranscriptCard(item) {
  const busy = S.busy.has(item.id);
  const card = h("section", { class: "confirm", "aria-label": "자막 없음" });
  card.append(h("p", { class: "confirm-title" }, "자막이 없습니다"),
    h("p", { class: "muted" }, "음성 인식은 시간이 걸립니다. 화면만 보고 분석하면 빠르지만 대사는 반영되지 않습니다."));
  const row = h("div", { class: "btn-row" });
  const stt = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "음성 인식(STT)으로");
  const screen = h("button", { type: "button", class: "btn", disabled: busy }, "화면만 보고 분석");
  const skip = h("button", { type: "button", class: "btn", disabled: busy }, "건너뛰기");
  stt.addEventListener("click", () => decide(item, { action: "stt" }));
  screen.addEventListener("click", () => decide(item, { action: "screen" }));
  skip.addEventListener("click", () => decide(item, { action: "skip" }));
  row.append(stt, screen, skip);
  card.append(row);
  return card;
}

// ---- confirm card -----------------------------------------------------------

function workingAnalysis(item) {
  const src = JSON.stringify(item.preview);
  let rec = S.edits.get(item.id);
  if (!rec || rec.src !== src) {
    rec = { src, a: structuredClone(item.preview) };
    S.edits.set(item.id, rec);
  }
  return rec.a;
}

function isEdited(item) {
  const rec = S.edits.get(item.id);
  return !!rec && rec.src === JSON.stringify(item.preview) && JSON.stringify(rec.a) !== rec.src;
}

function timelineBar(sections) {
  const total = Math.max(...sections.map((s) => Number(s.end) || 0), 1);
  const bar = h("div", { class: "tl-bar", role: "img",
    "aria-label": `섹션 ${sections.length}개: ${sections.map((s) => s.label).join(", ")}` });
  for (const s of sections) {
    const w = Math.max(((Number(s.end) - Number(s.start)) / total) * 100, 1.5);
    bar.append(h("span", { class: `tl-seg lb-${s.label}`, style: `flex-grow:${w.toFixed(2)}`, title: `${s.label} ${fmtTime(s.start)}` }));
  }
  return bar;
}

function sectionRow(item, a, idx) {
  const s = a.sections[idx];
  const id = `sec-${S.job.id}-${S.job.items.indexOf(item)}-${idx}`;
  const sel = h("select", { id, class: "label-select", "aria-label": `${fmtTime(s.start)} 섹션 라벨` });
  const labels = LABELS.includes(s.label) ? LABELS : [s.label, ...LABELS];
  for (const l of labels) {
    const opt = h("option", { value: l }, l);
    if (l === s.label) opt.selected = true;
    sel.append(opt);
  }
  const bullet = h("span", { class: `lb-dot lb-${s.label}`, "aria-hidden": "true" });
  const sub = h("span", { class: "sec-sub muted" }, s.sub ? s.sub : "");
  sel.addEventListener("change", () => {
    s.label = sel.value;
    if (s.label !== "body") delete s.sub;
    bullet.className = `lb-dot lb-${s.label}`;
    sub.textContent = s.sub || "";
    li.classList.toggle("edited", s.label !== item.preview.sections[idx].label);
    const warn = li.closest(".confirm");
    if (warn) {
      const note = warn.querySelector(".edit-note");
      if (note) note.hidden = !isEdited(item);
    }
  });
  const li = h("li", { class: "sec-row" + (s.label !== item.preview.sections[idx].label ? " edited" : "") });
  li.append(
    h("span", { class: "sec-time" }, `${fmtTime(s.start)}–${fmtTime(s.end)}`),
    h("span", { class: "sec-pick" }, bullet, sel, sub),
    h("span", { class: "sec-content" }, s.content || ""),
  );
  return li;
}

function frameStrip(item) {
  const frames = item.frames || [];
  if (!frames.length) return null;
  const wrap = h("div", { class: "frames", role: "list", "aria-label": `프레임 ${frames.length}장`, tabindex: "0" });
  frames.forEach((f, i) => {
    wrap.append(h("img", { src: filesUrl(f), alt: `프레임 ${i + 1}/${frames.length}`, loading: "lazy", role: "listitem" }));
  });
  return wrap;
}

function confirmCard(item) {
  const a = workingAnalysis(item);
  const busy = S.busy.has(item.id);
  const card = h("section", { class: "confirm", "aria-label": `${itemLabel(item)} 분석 확인` });
  if (a.summary) card.append(h("p", { class: "confirm-title" }, a.summary));
  const meta = h("p", { class: "meta-line" });
  if (a.hook_type) meta.append(h("span", { class: "tag" }, `훅 ${a.hook_type}`));
  if (a.narrative_type) meta.append(h("span", { class: "tag" }, `서사 ${a.narrative_type}`));
  if (meta.childNodes.length) card.append(meta);
  if (a.synopsis) card.append(h("p", { class: "muted synopsis" }, a.synopsis));

  const secs = a.sections || [];
  if (secs.length) {
    card.append(h("h3", {}, "섹션 타임라인"), timelineBar(secs));
    const ul = h("ul", { class: "sections" });
    secs.forEach((_, i) => ul.append(sectionRow(item, a, i)));
    card.append(ul);
  }
  const techs = a.techniques || [];
  if (techs.length) {
    card.append(h("h3", {}, "기법"));
    card.append(h("ul", { class: "techs" }, ...techs.map((t) => h("li", {},
      h("span", { class: "tag" }, t.kind), " ", h("strong", {}, t.name),
      t.at !== undefined && t.at !== null ? h("span", { class: "muted" }, ` ${typeof t.at === "number" ? fmtTime(t.at) : t.at}`) : null,
      t.desc ? h("span", { class: "muted" }, ` — ${t.desc}`) : null))));
  }
  const strip = frameStrip(item);
  if (strip) card.append(h("h3", {}, "프레임"), strip);

  card.append(h("p", { class: "edit-note muted", hidden: !isEdited(item), role: "status" }, "고친 라벨이 저장에 반영됩니다."));

  const row = h("div", { class: "btn-row" });
  const savePayload = (action) => (isEdited(item) ? { action, analysis: S.edits.get(item.id).a } : { action });
  const save = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "저장");
  const saveAll = h("button", { type: "button", class: "btn", disabled: busy }, "남은 영상 모두 저장");
  const skip = h("button", { type: "button", class: "btn", disabled: busy }, "건너뛰기");
  const open = S.openFeedback.has(item.id);
  const redo = h("button", { type: "button", class: "btn", "aria-expanded": String(open), disabled: busy }, "고칠 점 입력 후 다시 분석");
  save.addEventListener("click", () => decide(item, savePayload("save")));
  saveAll.addEventListener("click", () => decide(item, savePayload("save_all")));
  skip.addEventListener("click", () => decide(item, { action: "skip" }));
  redo.addEventListener("click", () => {
    if (S.openFeedback.has(item.id)) S.openFeedback.delete(item.id);
    else S.openFeedback.add(item.id);
    renderItem(item);
    const ta = S.el.querySelector(`[data-fb="${CSS.escape(item.id)}"]`);
    if (ta) ta.focus();
  });
  row.append(save, saveAll, skip, redo);
  card.append(row);

  if (open) {
    const fbId = `fb-${S.job.id}-${S.job.items.indexOf(item)}`;
    const ta = h("textarea", { id: fbId, rows: "3", "data-fb": item.id,
      placeholder: "예: 2:10 이후는 데모가 아니라 비교입니다" });
    ta.value = S.feedbackText.get(item.id) || "";
    ta.addEventListener("input", () => S.feedbackText.set(item.id, ta.value));
    const go = h("button", { type: "button", class: "btn btn-primary", disabled: busy }, "다시 분석 요청");
    go.addEventListener("click", () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); S.ctx.toast("고칠 점을 입력해 주세요", "error"); return; }
      decide(item, { action: "reanalyze", feedback: text });
    });
    card.append(h("div", { class: "feedback" },
      h("label", { for: fbId }, "고칠 점"), ta,
      h("p", { class: "muted" }, "다시 분석하면 Claude 호출이 한 번 더 일어납니다."), go));
  }
  return card;
}

// ---- rendering: form --------------------------------------------------------

function renderForm() {
  setView("form");
  S.el.replaceChildren();
  S.rows.clear();
  S.el.append(h("h1", {}, "레퍼런스 추가"));
  const card = h("section", { class: "card", "aria-label": "추가 설정" });
  S.el.append(card);
  const names = S.names;
  if (names.length === 0) {
    card.append(h("h2", {}, "카테고리가 없습니다"),
      h("p", { class: "muted" }, "먼저 ", h("code", {}, "/yt-coach:yt-init"), " 또는 홈에서 폴더 만들기를 진행하세요."),
      h("a", { class: "btn", href: "#/home" }, "홈으로"));
    return;
  }

  const form = h("form", { class: "add-form", novalidate: "" });

  const sel = h("select", { id: "add-category" });
  for (const n of names) {
    const o = h("option", { value: n }, n);
    if (n === S.category) o.selected = true;
    sel.append(o);
  }
  sel.addEventListener("change", () => { S.category = sel.value; S.ctx.setCategory(sel.value); });
  form.append(h("div", { class: "field" }, h("label", { for: "add-category" }, "카테고리"), sel));

  const fs = h("fieldset", { class: "field seg" }, h("legend", {}, "구분"));
  for (const [val, text] of [["ref", "레퍼런스"], ["mine", "내 영상"]]) {
    const rid = `add-role-${val}`;
    const input = h("input", { type: "radio", name: "role", id: rid, value: val });
    input.checked = S.draft.role === val;
    input.addEventListener("change", () => { S.draft.role = val; });
    fs.append(h("label", { class: "seg-opt", for: rid }, input, h("span", {}, text)));
  }
  form.append(fs);

  const ta = h("textarea", { id: "add-links", rows: "6", placeholder: "유튜브 링크를 한 줄에 하나씩 붙여 넣으세요", spellcheck: "false" });
  ta.value = S.draft.links;
  ta.addEventListener("input", () => { S.draft.links = ta.value; err.hidden = true; });
  const err = h("p", { class: "error field-error", id: "add-links-err", role: "alert", hidden: true }, "");
  ta.setAttribute("aria-describedby", "add-links-err");
  form.append(h("div", { class: "field" }, h("label", { for: "add-links" }, "링크"), ta, err));

  const opts = h("div", { class: "field checks" });
  const checks = [
    ["shots", "샷 분류도 함께", null],
    ["force", "이미 분석한 영상도 다시 분석", null],
    ["refetch", "자막·컷을 새로 받기(요청이 늘어 차단될 수 있어요)", null],
  ];
  for (const [key, text] of checks) {
    const cid = `add-opt-${key}`;
    const cb = h("input", { type: "checkbox", id: cid });
    cb.checked = !!S.draft[key];
    cb.addEventListener("change", () => { S.draft[key] = cb.checked; });
    opts.append(h("label", { class: "check", for: cid }, cb, h("span", {}, text)));
  }
  form.append(opts);

  const row = h("div", { class: "btn-row" });
  const start = h("button", { type: "submit", class: "btn btn-primary" }, "추가 시작");
  const shotsOnly = h("button", { type: "button", class: "btn" }, "샷 분류만 보강");
  row.append(start, shotsOnly);
  form.append(row);
  form.append(h("p", { class: "muted hint" }, "영상은 하나씩 처리합니다. 분석마다 Claude 호출이 일어납니다."));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const links = ta.value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
    if (links.length === 0) {
      err.textContent = "분석할 유튜브 링크를 한 줄에 하나씩 넣어 주세요.";
      err.hidden = false;
      ta.setAttribute("aria-invalid", "true");
      ta.focus();
      return;
    }
    ta.removeAttribute("aria-invalid");
    start.disabled = true;
    shotsOnly.disabled = true;
    await startJob("/api/add", {
      category: sel.value, role: S.draft.role, links,
      shots: S.draft.shots, force: S.draft.force, refetch: S.draft.refetch,
    });
    if (S && S.ctx.isCurrent() && S.view === "form") { start.disabled = false; shotsOnly.disabled = false; }
  });
  shotsOnly.addEventListener("click", async () => {
    start.disabled = true;
    shotsOnly.disabled = true;
    await startJob("/api/add/shots-only", { category: sel.value });
    if (S && S.ctx.isCurrent() && S.view === "form") { start.disabled = false; shotsOnly.disabled = false; }
  });
  card.append(form);
}

// ---- page contract ----------------------------------------------------------

export async function mount(el, params, ctx) {
  stopPolling();
  stopClock();
  S = freshState(el, ctx);
  el.replaceChildren(h("h1", {}, "레퍼런스 추가"), h("p", { class: "muted", role: "status" }, "불러오는 중…"));
  const mine = S;
  try {
    const [cats, cur] = await Promise.all([
      ctx.api("GET", "/api/categories"),
      ctx.api("GET", "/api/jobs/current?kind=collect"),
    ]);
    if (S !== mine || !ctx.isCurrent()) return;
    mine.names = (cats.categories || []).map((c) => c.category);
    if (!mine.names.includes(mine.category)) mine.category = mine.names[0] || null;
    if (cur.job) {
      applyJob(cur.job);
      if (isActive(cur.job)) schedulePoll();
    } else {
      renderForm();
    }
  } catch (e) {
    if (S !== mine || !ctx.isCurrent() || e.error === "aborted") return;
    const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
    retry.addEventListener("click", () => { if (ctx.isCurrent()) mount(el, params, ctx); });
    el.replaceChildren(h("h1", {}, "레퍼런스 추가"), h("p", { class: "error", role: "alert" }, e.message), retry);
  }
}

export function unmount() {
  stopPolling();
  stopClock();
  S = null;
}

export function onCategory(category) {
  if (!S) return;
  S.category = category;
  const sel = S.el.querySelector("#add-category");
  if (sel && category && sel.value !== category) sel.value = category;
}
