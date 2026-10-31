// 콘티 탭의 장면 도구 줄: 장면으로 합치기(미리보기 → 저장), 사진 설명 만들기, 사진 다시 고르기, 장면 그림 그리기.
// 서버: POST /api/conti/merge/preview·/apply (작업 kind conti_merge), GET /api/refs/describe-status,
// POST /api/refs/describe (kind refs_describe), POST /api/conti/refs/pick (kind conti_refs),
// POST /api/conti/art (kind conti_art), /api/jobs/<id>(/cancel).
//
// - 버튼은 작은 보조 버튼이다(화면의 주요 행동이 아니다). 작업 진행은 큰 배너 없이 한 줄(data-job-kind)로 보인다.
//   머리말 상태 칩(status-chip.js)도 같은 작업을 보여 주고, 이 줄은 이 화면에만 있는 것(걸리는 시간, 장 수·멈추기,
//   묶음 진행)을 더한다. 끝난 알림은 이 화면이 직접 띄우고, 상태 칩은 다른 화면에 있을 때만 띄운다.
// - 합치기 미리보기는 저장 전에는 파일을 바꾸지 않는다. 저장하면 편집기가 새 응답으로 다시 그린다(opts.onMerged).
// - 화면에 보이는 글은 모두 h()의 글자 노드로 넣는다.
// - 장면 그림: 저장 응답이 옛 그림이 된 장면(art_stale)을 알려 주면 ART_DEBOUNCE_MS(4초) 기다렸다가(그 사이 또 고치면
//   다시 4초) 그 장면만 다시 그린다(artRedrawPlan). 콘티당 작업은 하나라 도는 중에 고친 장면은 끝난 뒤 한 번 더.
//   자동 시작이 Claude 준비 오류로 막히면 토스트 없이 한 줄만 보이고 이 화면에서 자동 시작을 멈춘다.
//   화면을 떠나면 예약만 지운다(시작한 작업은 계속).
// - 그림 버튼은 콘티 응답의 art_counts로 정한다(artButtons): 그림이 없으면 "장면 그림 그리기", 있으면 "남은 장면 그리기"와
//   "처음부터 다시 그리기". 작업이 실패·멈춤·일부 실패로 끝나 남은 장면이 있으면 "N장면을 못 그렸어요"와 이어 그리기 버튼.
// opts: { path, category, getData() -> GET /api/conti 응답, getHash(), isBusy() (컷 저장·편집 중),
//         hasDrafts() (패널에 저장하지 않은 입력이 있음: 합치기를 막는다),
//         onMerged(res) (합친 콘티 응답), onReload() (사진·그림이 저장됐거나 콘티가 밖에서 바뀜: 편집기 다시 불러오기,
//         promise를 돌려줄 수 있음), onArtSaved() (그림 묶음이 저장됨: 편집 칸은 그대로 두고 카드·그림만 새로, promise),
//         onArtChange() (그림 작업이 시작·끝남, 다시 그릴 장면이 바뀜: 캡션 고치기) }
import { h, fill } from "/static/dom.js";
import { t } from "/static/copy.js";
import { tag } from "/static/ui.js";
import { sectionLabelName } from "/static/labels.js";
import {
  aboutSec, artBusy as artBusyOf, artButtons, artRedrawPlan, artTrouble, describeHint, hasRepeats, mergeApplyCuts, mergeView, tagKind,
} from "/static/conti-utils.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const ACTIVE = new Set(["running", "awaiting"]);
const HANDLED_KEY = "ytc.conti.merge.handled";
const MERGE_LABEL = "장면으로 합치기";
const DESCRIBE_LABEL = "사진 설명 만들기";
const PICK_LABEL = "사진 다시 고르기";
const MERGE_FIRST = "반복 컷을 먼저 장면으로 합쳐 주세요";
const AFTER_DESCRIBE = "사진 설명이 생겼어요. 사진 다시 고르기를 누르면 장면에 맞는 사진이 붙어요";
const STALE_MERGE = "미리보기를 만든 뒤 콘티가 바뀌었어요. 장면으로 합치기를 다시 눌러 주세요";
const DRAFTS_FIRST = "저장하지 않은 입력이 있어요. 저장하거나 지운 뒤 합쳐 주세요";
const READY_ERRORS = new Set(["claude_missing", "llm_not_configured"]);
const KINDS = { merge: "conti_merge", describe: "refs_describe", pick: "conti_refs", art: "conti_art" };

let A = null;

const isActive = (job) => !!job && ACTIVE.has(job.status);
const enc = encodeURIComponent;

function alive(mine) {
  return !!mine && A === mine && mine.ctx.isCurrent();
}

function readHandled() {
  try {
    const v = JSON.parse(localStorage.getItem(HANDLED_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

// A merge preview saved or closed in this browser is not offered again after a reload.
function markHandled(jobId) {
  const ids = readHandled().filter((x) => x !== jobId);
  ids.push(jobId);
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify(ids.slice(-20))); } catch { /* storage unavailable */ }
}

function announce(text) {
  if (!A || !A.live) return;
  A.live.textContent = "";
  A.live.textContent = text;
}

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function stopTimers() {
  if (!A) return;
  for (const run of Object.values(A.runs)) if (run.timer) { clearTimeout(run.timer); run.timer = null; }
  if (A.art.timer) { clearTimeout(A.art.timer); A.art.timer = null; }
}

// ---- jobs -----------------------------------------------------------------------------------

// One poller per kind. onUpdate(job) runs after every answer; polling goes on while the job is active.
function poll(name, onUpdate) {
  const mine = A;
  const run = mine.runs[name];
  if (run.timer) clearTimeout(run.timer);
  run.timer = setTimeout(async () => {
    run.timer = null;
    if (!alive(mine) || !run.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${enc(run.job.id)}`);
      if (!alive(mine)) return;
      run.errors = 0;
      run.job = data.job;
      onUpdate(data.job);
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        run.job = null;
        run.error = "작업을 찾을 수 없어요. 서버가 다시 켜졌다면 다시 눌러 주세요";
        paint();
        return;
      }
      run.errors += 1;
    }
    if (alive(mine) && isActive(run.job)) poll(name, onUpdate);
  }, pollDelay(run.errors));
}

async function cancel(name) {
  const mine = A;
  const run = mine.runs[name];
  if (!run.job || run.stopping) return;
  run.stopping = true;
  paint();
  try {
    await mine.ctx.api("POST", `/api/jobs/${enc(run.job.id)}/cancel`, {});
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error !== "not_running") run.error = e.message || "멈추지 못했어요";
    run.stopping = false;
    paint();
  }
}

// ---- 장면으로 합치기 ---------------------------------------------------------------------------

function mergePreview(job) {
  const item = job && job.items && job.items[0];
  return item && item.state === "awaiting" && item.preview ? item.preview : null;
}

function adoptMerge(job) {
  const run = A.runs.merge;
  run.job = job;
  if (isActive(job) && !mergePreview(job)) { paint(); return; }
  if (mergePreview(job)) {
    run.error = "";
    paint();
    announce("장면으로 합치기 미리보기가 준비됐어요. 확인한 뒤 저장하세요");
    const title = A.slots.preview.querySelector("h3");
    if (title && run.focus) title.focus({ preventScroll: true });   // the user asked for it in this visit
    run.focus = false;
    return;
  }
  if (job.status === "failed" || job.status === "stopped") {
    const item = job.items && job.items[0];
    run.error = (item && item.message) || job.message || "장면으로 합치지 못했어요";
    run.job = null;
    markHandled(job.id);
    announce(`장면으로 합치기 실패: ${run.error}`);
  }
  paint();
}

async function startMerge() {
  const mine = A;
  const run = mine.runs.merge;
  if (run.starting || isActive(run.job) || mergePreview(run.job)) return;
  run.starting = true;
  run.error = "";
  run.note = "";
  paint();
  try {
    const data = await mine.ctx.api("POST", "/api/conti/merge/preview", { path: mine.opts.path });
    if (!alive(mine)) return;
    run.errors = 0;
    run.focus = true;
    adoptMerge(data.job);
    if (isActive(run.job) && !mergePreview(run.job)) poll("merge", adoptMerge);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error === "nothing_to_merge") run.note = e.message || "합칠 장면이 없어요";
    else run.error = e.message || "시작하지 못했어요";
  } finally {
    if (alive(mine)) { run.starting = false; paint(); }
  }
}

function closeMerge() {
  const run = A.runs.merge;
  if (run.job) markHandled(run.job.id);
  run.job = null;
  run.saving = false;
  paint();
}

async function saveMerge() {
  const mine = A;
  const run = mine.runs.merge;
  const preview = mergePreview(run.job);
  if (!preview || run.saving) return;
  if (mine.opts.isBusy()) { run.error = "저장 중이에요. 잠시 뒤 다시 눌러 주세요"; paint(); return; }
  // merged-away cuts would drop their drafts, and a merged scene's line no longer matches what was typed
  if (mine.opts.hasDrafts()) { run.error = DRAFTS_FIRST; paint(); return; }
  run.saving = true;
  run.error = "";
  paint();
  try {
    const res = await mine.ctx.api("POST", "/api/conti/merge/apply",
      { path: mine.opts.path, cuts: mergeApplyCuts(preview), hash: preview.hash });
    if (!alive(mine)) return;
    markHandled(run.job.id);
    run.job = null;
    run.saving = false;
    const m = res.merged || { before: preview.before, after: preview.after };
    mine.ctx.toast(`장면으로 합쳤어요 · 컷 ${m.before}개 → 장면 ${m.after}개`, "info");
    // the merge's own warnings (footage_not_transferred) in plain words; cut warnings stay in the header warn box
    for (const w of m.warnings || []) mine.ctx.toast(w.message, "error");
    paint();
    mine.opts.onMerged(res);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    run.saving = false;
    if (e.error === "conti_changed") {
      markHandled(run.job.id);
      run.job = null;
      run.error = STALE_MERGE;
      paint();
      mine.opts.onReload();
      return;
    }
    run.error = e.message || "저장하지 못했어요";
    paint();
  }
}

function mergeCard(preview) {
  const run = A.runs.merge;
  const sections = mergeView(preview);
  const warns = preview.warnings || [];
  const save = h("button", { type: "button", class: "btn btn-primary btn-s", disabled: run.saving, "data-fk": "merge-save" },
    run.saving ? "저장하는 중…" : "저장");
  const close = h("button", { type: "button", class: "btn btn-s", disabled: run.saving, "data-fk": "merge-cancel" }, "취소");
  save.addEventListener("click", saveMerge);
  close.addEventListener("click", () => {
    closeMerge();
    A.ctx.toast("미리보기를 닫았어요. 저장하지 않았어요", "info");
    const b = A.root.querySelector('[data-fk="merge"]');
    if (b) b.focus();
  });
  const list = h("div", { class: "mp-sections" }, ...sections.map((s) => {
    const changed = s.changed && s.before !== s.scenes.length;
    const summary = h("summary", {},
      tag(sectionLabelName(s.label), tagKind(s.label)),
      h("span", { class: "mp-count" }, changed ? `컷 ${s.before}개 → 장면 ${s.scenes.length}개` : `장면 ${s.scenes.length}개 · 그대로`));
    const scenes = h("ol", { class: "mp-scenes" }, ...s.scenes.map((sc) => h("li", {},
      h("span", { class: "mp-head" }, h("span", { class: "mp-n num" }, `#${sc.n}`), h("strong", {}, sc.action || ""),
        h("span", { class: "muted" }, ` · ${sc.size || ""} · ${aboutSec(sc.sec)}`)),
      h("span", { class: "mp-line" + (sc.line ? "" : " muted") }, sc.line ? `“${sc.line}”` : "(대사 없음)"))));
    const box = h("details", { class: "mp-sec" }, summary, scenes);
    if (changed) box.open = true;
    return box;
  }));
  return h("section", { class: "merge-preview", "aria-labelledby": "merge-title" },
    h("h3", { id: "merge-title", tabindex: "-1" }, "장면으로 합치기 미리보기"),
    h("p", { class: "mp-total" }, h("strong", {}, `컷 ${preview.before}개 → 장면 ${preview.after}개`),
      " · 같은 장면을 반복한 컷을 하나로 묶고, 대사는 글자를 바꾸지 않고 장면에 나눴어요. 저장하기 전에는 바뀌지 않아요."),
    warns.length ? h("ul", { class: "warn-list", "aria-label": `확인할 점 ${warns.length}개` }, ...warns.map((w) => h("li", {}, w.message))) : null,
    list,
    h("p", { class: "muted small" }, "저장하면 합치기 전 상태가 이력에 남아 되돌릴 수 있어요. 촬영 영상에서 직접 고른 판정은 남은 장면으로 옮겨요."),
    h("div", { class: "btn-row" }, save, close));
}

// ---- 사진 설명 만들기 ---------------------------------------------------------------------------

async function loadStatus() {
  const mine = A;
  if (!mine.opts.category) { mine.status = null; paint(); return; }
  try {
    const st = await mine.ctx.api("GET", `/api/refs/describe-status?category=${enc(mine.opts.category)}`);
    if (!alive(mine)) return;
    mine.status = st;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.status = null;
  }
  paint();
}

function describeCounts(job) {
  let done = 0;
  let total = 0;
  for (const it of (job && job.items) || []) { done += Number(it.done) || 0; total += Number(it.total) || 0; }
  return { done, total };
}

function adoptDescribe(job) {
  const run = A.runs.describe;
  run.job = job;
  if (isActive(job)) { paint(); return; }
  run.stopping = false;
  run.job = null;
  const described = (job.summary && job.summary.described) || 0;
  if (job.status === "done") {
    run.note = described ? AFTER_DESCRIBE : "설명할 사진이 더 없어요";
    A.ctx.toast(described ? `사진 ${described}장에 설명을 붙였어요` : "설명할 사진이 더 없어요", "info");
  } else if (job.status === "stopped") {
    run.note = described ? `멈췄어요 · 사진 ${described}장의 설명은 남아요. ${AFTER_DESCRIBE}` : "";
    run.error = job.message && !described ? job.message : "";
    if (!run.note && !run.error) run.note = "멈췄어요";
  } else {
    run.error = job.message || "사진 설명을 만들지 못했어요";
  }
  announce(run.error || run.note);
  loadStatus();
}

async function startDescribe() {
  const mine = A;
  const run = mine.runs.describe;
  if (run.starting || isActive(run.job) || !mine.opts.category) return;
  run.starting = true;
  run.error = "";
  run.note = "";
  paint();
  try {
    const data = await mine.ctx.api("POST", "/api/refs/describe", { category: mine.opts.category });
    if (!alive(mine)) return;
    run.errors = 0;
    adoptDescribe(data.job);
    if (isActive(run.job)) poll("describe", adoptDescribe);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    run.error = e.message || "시작하지 못했어요";
    if (e.error === "nothing_to_do") loadStatus();
  } finally {
    if (alive(mine)) { run.starting = false; paint(); }
  }
}

// ---- 사진 다시 고르기 ---------------------------------------------------------------------------

function adoptPick(job) {
  const run = A.runs.pick;
  run.job = job;
  if (isActive(job)) { paint(); return; }
  run.job = null;
  if (job.status === "done") {
    const s = job.summary || {};
    A.ctx.toast(`장면 ${s.scenes ?? 0}개 중 ${s.picked ?? 0}개에 맞는 사진을 골랐어요`, "info");
    run.warnings = (s.warnings || []).map((w) => w.message).filter(Boolean);
    announce("사진을 다시 골랐어요");
    paint();
    A.opts.onReload();
    return;
  }
  const item = job.items && job.items[0];
  run.error = (item && item.message) || job.message || "사진을 고르지 못했어요";
  announce(`사진 다시 고르기 실패: ${run.error}`);
  paint();
}

async function startPick() {
  const mine = A;
  const run = mine.runs.pick;
  if (run.starting || isActive(run.job)) return;
  if (mine.opts.isBusy()) { run.error = "저장 중이에요. 잠시 뒤 다시 눌러 주세요"; paint(); return; }
  run.starting = true;
  run.error = "";
  run.warnings = [];
  paint();
  try {
    const data = await mine.ctx.api("POST", "/api/conti/refs/pick", { path: mine.opts.path, hash: mine.opts.getHash() });
    if (!alive(mine)) return;
    run.errors = 0;
    adoptPick(data.job);
    if (isActive(run.job)) poll("pick", adoptPick);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    run.error = e.message || "시작하지 못했어요";
    if (e.error === "conti_changed") mine.opts.onReload();
    if (e.error === "no_ref_descs") loadStatus();
  } finally {
    if (alive(mine)) { run.starting = false; paint(); }
  }
}

// ---- 장면 그림 -----------------------------------------------------------------------------------
// One drawing job per conti, started by the button, the panel's 다시 그리기 or the auto redraw (artRedrawPlan).

function artChanged() {
  if (A && typeof A.opts.onArtChange === "function") A.opts.onArtChange();
}

// Feeds the auto redraw plan: (re)starts its wait timer (ART_DEBOUNCE_MS after an edit, 0 after a job) and starts a
// job when the plan says so.
function feedArt(event) {
  const mine = A;
  if (!mine) return;
  const r = artRedrawPlan(mine.art.plan, event);
  mine.art.plan = r.state;
  if (event.type === "leave" && mine.art.timer) { clearTimeout(mine.art.timer); mine.art.timer = null; }
  if (r.delayMs !== null) {
    if (mine.art.timer) clearTimeout(mine.art.timer);
    mine.art.timer = setTimeout(() => {
      mine.art.timer = null;
      if (alive(mine)) feedArt({ type: "timer" });
    }, r.delayMs);
  }
  if (r.start) startArt({ ids: r.start, auto: true });
  artChanged();
}

function artDone(job) {
  let done = 0;
  for (const it of (job && job.items) || []) done += Number(it.done) || 0;
  return done;
}

function adoptArt(job) {
  const mine = A;
  const run = mine.runs.art;
  run.job = job;
  if (isActive(job)) {
    // every two scenes are saved as they come: show them and take the new hash so the next edit does not clash.
    // Light path only: the edit form is not rebuilt (it would break typing, Korean IME composition included).
    const done = artDone(job);
    if (run.seen !== null && done > run.seen) mine.opts.onArtSaved();
    run.seen = done;
    paint();
    return;
  }
  run.job = null;
  run.seen = null;
  run.ids = null;
  run.stopping = false;
  const s = job.summary || {};
  // a manual job, or any job that left scenes undrawn, decides the "N장면을 못 그렸어요" line (a clean auto redraw keeps it)
  if (!run.auto || job.status !== "done" || (s.failed ?? 0) > 0) {
    run.last = { status: job.status, failed: s.failed ?? 0, remaining: s.remaining ?? 0 };
  }
  if (job.status === "done") {
    const drawn = s.drawn ?? 0;
    // an auto redraw is quiet (the card caption tells); the buttons say what they did
    if (!run.auto) mine.ctx.toast(drawn ? `장면 ${drawn}개를 그렸어요` : "새로 그린 장면이 없어요", "info");
    run.warnings = (s.warnings || []).filter((w) => typeof w === "string" && w);
    announce(drawn ? `장면 그림 ${drawn}개를 그렸어요` : "새로 그린 장면 그림이 없어요");
  } else if (job.status === "stopped") {
    run.note = "멈췄어요 · 그린 그림은 남아요";
    announce(run.note);
  } else {
    const item = job.items && job.items[0];
    run.error = (item && item.message) || job.message || "장면 그림을 그리지 못했어요";
    announce(`장면 그림 실패: ${run.error}`);
  }
  run.auto = false;
  mine.art.lastEnd = Date.now();
  paint();
  // new pictures first, then scenes edited while it ran go with the new hash
  // (the reloaded art_counts repaint the tool row's buttons and the undrawn line)
  Promise.resolve(mine.opts.onArtSaved()).catch(() => {}).then(() => {
    if (!alive(mine)) return;
    paint();
    feedArt({ type: "done" });
  });
}

// Another drawing job holds this conti (started in another tab): follow it; the plan goes on when it ends.
async function followArt() {
  const mine = A;
  const job = await mine.ctx.api("GET", `/api/jobs/current?kind=${KINDS.art}`).then((r) => r.job, () => null);
  if (!alive(mine)) return;
  const run = mine.runs.art;
  if (isActive(run.job)) return;
  if (job && isActive(job) && job.items && job.items[0] && job.items[0].id === mine.opts.path) {
    run.job = job;
    run.seen = artDone(job);
    run.ids = null;                     // its scenes are not known here: any stale scene may be in it
    run.auto = true;                    // not started here: it ends without the 그렸어요 toast
    poll("art", adoptArt);
    paint();
    artChanged();
    return;
  }
  // not found (it just ended, another conti's job is the latest, or the request failed): try again after a full wait
  feedArt({ type: "missed" });
}

// ids: these scenes only; staleOnly: scenes with no drawing or an old one; neither: every scene (처음부터 다시 그리기).
async function startArt({ ids = null, staleOnly = false, auto = false } = {}) {
  const mine = A;
  const run = mine.runs.art;
  // a manual start (or its job) holds the conti: the auto ids wait for it to end
  if (auto && (run.starting || isActive(run.job))) { feedArt({ type: "requeue", ids }); return; }
  if (!auto) {
    if (run.starting || isActive(run.job)) return;
    if (mine.opts.isBusy()) { run.error = "저장 중이에요. 잠시 뒤 다시 눌러 주세요"; paint(); return; }
    run.error = "";                     // an auto start keeps what a manual one said
    run.note = "";
    run.last = null;                    // a new try: the undrawn line goes until this job ends
  }
  run.starting = true;
  run.ids = ids;                        // the scenes this job draws (null: whole / stale-only): only they show 다시 그리는 중
  paint();
  artChanged();
  const body = { path: mine.opts.path, hash: mine.opts.getHash() };
  if (ids) body.ids = ids;
  if (staleOnly) body.stale_only = true;
  const post = (b) => mine.ctx.api("POST", "/api/conti/art", b);
  try {
    let data;
    try {
      data = await post(body);
    } catch (e) {
      if (!auto || e.error !== "conti_changed" || !alive(mine)) throw e;
      // the file moved on (a drawing just saved, an outside edit): reload and try once more with the newest hash
      const fresh = await mine.ctx.api("GET", `/api/conti?path=${enc(mine.opts.path)}`);
      if (!alive(mine)) return;
      mine.opts.onReload();
      data = await post({ ...body, hash: fresh.hash });
    }
    if (!alive(mine)) return;
    run.errors = 0;
    run.warnings = [];
    run.auto = auto;
    if (!auto) mine.art.blocked = false;     // Claude answered a manual start: the blocked line goes (auto start stays off)
    run.seen = artDone(data.job);
    adoptArt(data.job);
    if (isActive(run.job)) poll("art", adoptArt);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error === "conti_busy") {
      if (auto) feedArt({ type: "requeue", ids });
      else run.error = e.message || "이 콘티의 장면 그림을 그리는 중이에요";
      followArt();
    } else if (auto && READY_ERRORS.has(e.error)) {
      mine.art.blocked = true;
      feedArt({ type: "fail_ready" });
    } else if (auto) {
      mine.ctx.toast(e.message || "장면 그림을 다시 그리지 못했어요", "error");
      feedArt({ type: "done" });
    } else {
      run.error = e.message || "시작하지 못했어요";
      if (e.error === "conti_changed") mine.opts.onReload();
    }
    // auto ids requeued behind this manual start: no job came, so let them go after the wait
    if (!auto && mine.art.plan.running && !isActive(run.job)) feedArt({ type: "missed" });
  } finally {
    if (alive(mine)) { run.starting = false; paint(); artChanged(); }
  }
}

// ---- paint ------------------------------------------------------------------------------------

function progressLine(kind, text, stop) {
  return h("div", { class: "sa-progress", role: "status", "data-job-kind": kind },
    h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, text), stop);
}

function paint() {
  if (!A) return;
  const data = A.opts.getData();
  if (!data) return;
  const keep = A.root.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
  const { merge, describe, pick, art } = A.runs;
  const repeats = hasRepeats(data.cuts);
  const preview = mergePreview(merge.job);
  const hint = describeHint(A.status);
  const buttons = [];
  if (repeats && !preview) {
    const b = h("button", { type: "button", class: "btn btn-s", "data-fk": "merge", disabled: merge.starting || isActive(merge.job) }, MERGE_LABEL);
    b.addEventListener("click", startMerge);
    buttons.push(b);
  }
  if (hint) {
    const b = h("button", { type: "button", class: "btn btn-s", "data-fk": "describe", disabled: describe.starting || isActive(describe.job) }, DESCRIBE_LABEL);
    b.addEventListener("click", startDescribe);
    buttons.push(b);
  }
  // status undefined = still asking; null = unknown (no category / request failed): let the server answer
  if (A.status === null || (A.status && A.status.described > 0)) {
    const b = h("button", {
      type: "button", class: "btn btn-s", "data-fk": "pick", disabled: pick.starting || isActive(pick.job) || repeats,
      "aria-describedby": repeats ? "sa-merge-first" : false,
    }, PICK_LABEL);
    b.addEventListener("click", startPick);
    buttons.push(b);
  }
  const artBusyNow = art.starting || isActive(art.job);
  for (const spec of artButtons(data.art_counts)) {
    const b = h("button", {
      type: "button", class: "btn btn-s", "data-fk": spec.fk, disabled: artBusyNow || repeats,
      "aria-describedby": repeats ? "sa-merge-first" : false,
    }, spec.label);
    b.addEventListener("click", () => startArt({ staleOnly: spec.staleOnly }));
    buttons.push(b);
  }
  const notes = [];
  if (hint && !isActive(describe.job)) notes.push(h("p", { class: "sa-hint" }, hint.text));
  if (repeats && buttons.some((b) => ["pick", "art", "art-all"].includes(b.dataset.fk))) {
    notes.push(h("p", { class: "sa-hint", id: "sa-merge-first" }, MERGE_FIRST));
  }
  if (A.art.blocked) notes.push(h("p", { class: "sa-note", role: "status" }, t("conti.artBlocked")));
  const undrawn = artBusyNow ? 0 : artTrouble(art.last, data.art_counts);
  if (undrawn) {
    const again = h("button", { type: "button", class: "btn btn-s", "data-fk": "art-resume", disabled: repeats }, "남은 장면 그리기");
    again.addEventListener("click", () => startArt({ staleOnly: true }));
    notes.push(h("div", { class: "sa-note sa-undrawn" }, h("span", { role: "status" }, `${undrawn}장면을 못 그렸어요`), again));
  }
  fill(A.slots.row, ...buttons);
  fill(A.slots.notes, ...notes);

  const lines = [];
  if (isActive(merge.job) && !preview) lines.push(progressLine(KINDS.merge, "Claude가 반복 컷을 장면으로 묶고 대사를 나누는 중… 보통 1~2분", null));
  if (isActive(describe.job)) {
    const { done, total } = describeCounts(describe.job);
    const stop = h("button", { type: "button", class: "btn btn-s btn-ghost", "data-fk": "describe-stop", disabled: describe.stopping }, describe.stopping ? "멈추는 중…" : "멈추기");
    stop.addEventListener("click", () => cancel("describe"));
    lines.push(progressLine(KINDS.describe, `사진 설명 만드는 중 · ${done}/${total}장 · 다른 일을 해도 돼요`, stop));
  }
  if (isActive(pick.job)) {
    const item = pick.job.items && pick.job.items[0];
    lines.push(progressLine(KINDS.pick, `Claude가 장면마다 맞는 사진을 고르는 중…${item && item.message ? ` ${item.message}` : ""}`, null));
  }
  if (isActive(art.job)) {
    const item = art.job.items && art.job.items[0];
    const stop = h("button", { type: "button", class: "btn btn-s btn-ghost", "data-fk": "art-stop", disabled: art.stopping },
      art.stopping ? "멈추는 중…" : "멈추기");
    stop.addEventListener("click", () => cancel("art"));
    lines.push(progressLine(KINDS.art, `Claude가 장면 그림을 그리는 중…${item && item.message ? ` ${item.message}` : ""}`, stop));
  }
  for (const run of [merge, describe, pick, art]) {
    if (run.error) lines.push(h("p", { class: "error", role: "alert" }, run.error));
    if (run.note) lines.push(h("p", { class: "sa-note", role: "status" }, run.note));
  }
  if (pick.warnings && pick.warnings.length) lines.push(h("ul", { class: "warn-list" }, ...pick.warnings.map((t) => h("li", {}, t))));
  if (art.warnings && art.warnings.length) lines.push(h("ul", { class: "warn-list" }, ...art.warnings.map((t) => h("li", {}, t))));
  fill(A.slots.progress, ...lines);
  fill(A.slots.preview, preview ? mergeCard(preview) : null);
  A.root.hidden = !buttons.length && !lines.length && !notes.length && !preview;
  if (keep) {
    const again = A.root.querySelector(`[data-fk="${keep}"]`);
    if (again && !again.disabled) again.focus({ preventScroll: true });
  }
}

// Bring back what was running (or a merge preview not yet saved) for this conti / category.
async function resume() {
  const mine = A;
  const ask = (kind) => mine.ctx.api("GET", `/api/jobs/current?kind=${kind}`).then((r) => r.job, () => null);
  const [merge, describe, pick, art] = await Promise.all([ask(KINDS.merge), ask(KINDS.describe), ask(KINDS.pick), ask(KINDS.art)]);
  if (!alive(mine)) return;
  const mineItem = (job) => !!job && !!job.items && !!job.items[0] && job.items[0].id === mine.opts.path;
  if (mineItem(merge) && !readHandled().includes(merge.id) && (isActive(merge) || mergePreview(merge))) {
    adoptMerge(merge);
    if (isActive(merge) && !mergePreview(merge)) poll("merge", adoptMerge);
  }
  if (describe && isActive(describe) && describe.category === mine.opts.category) {
    mine.runs.describe.job = describe;
    poll("describe", adoptDescribe);
  }
  if (mineItem(pick) && isActive(pick)) {
    mine.runs.pick.job = pick;
    poll("pick", adoptPick);
  }
  if (mineItem(art) && isActive(art) && !isActive(mine.runs.art.job)) {
    mine.runs.art.job = art;
    mine.runs.art.seen = artDone(art);
    mine.runs.art.ids = null;
    mine.runs.art.auto = true;          // picked up on return: it ends without the 그렸어요 toast
    poll("art", adoptArt);
  }
  paint();
  artChanged();
}

const freshRun = () => ({ job: null, timer: null, errors: 0, starting: false, stopping: false, saving: false, error: "", note: "", warnings: [],
  focus: false, auto: false, seen: null, ids: null, last: null });

export function mountSceneActions(ctx, opts) {
  stopTimers();
  const live = h("div", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
  const slots = {
    row: h("div", { class: "sa-row" }), notes: h("div", { class: "sa-notes" }),
    progress: h("div", { class: "sa-lines" }), preview: h("div", { class: "sa-preview" }),
  };
  const root = h("section", { class: "scene-actions", "aria-label": "장면 도구" }, live, slots.row, slots.notes, slots.progress, slots.preview);
  root.hidden = true;
  A = {
    ctx, opts, root, live, slots, status: undefined, runs: { merge: freshRun(), describe: freshRun(), pick: freshRun(), art: freshRun() },
    art: { plan: { pending: [], running: false, disabled: false }, timer: null, blocked: false, lastEnd: 0 },
  };
  const mine = A;
  loadStatus();
  resume();
  return {
    el: root,
    // The conti changed (edit, merge, restore): the buttons follow it (repeats left or not).
    refresh: () => { if (alive(mine)) paint(); },
    // A save made these scenes' drawings old (the answer's art_stale): redraw them after the wait.
    artStale: (ids) => { if (alive(mine) && Array.isArray(ids) && ids.length) feedArt({ type: "stale", ids }); },
    // "다시 그리는 중…": the running job draws this scene (any scene for a whole / stale-only job), or it waits.
    artBusy: (id) => alive(mine) && artBusyOf({
      active: mine.runs.art.starting || isActive(mine.runs.art.job) || mine.art.plan.running,
      ids: mine.runs.art.ids, pending: mine.art.plan.pending,
    }, id),
    // A drawing job runs on this conti or just ended (its saves change the file hash).
    artRunning: () => alive(mine) && (mine.runs.art.starting || isActive(mine.runs.art.job) || Date.now() - mine.art.lastEnd < 5000),
    // 다시 그리기 in the scene panel: these scenes now.
    redraw: (ids) => { if (alive(mine)) startArt({ ids }); },
    // leaving drops what was scheduled; a started job goes on (the status chip still shows it)
    unmount: () => {
      if (A !== mine) return;
      feedArt({ type: "leave" });
      clearTimeout(mine.art.timer);
      stopTimers();
      A = null;
    },
  };
}
