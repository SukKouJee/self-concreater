// 콘티 편집 화면의 "촬영 영상" 영역: 폴더 연결, 처리 진행, 남은 컷 요약·필터, 확인 필요 카드(재생),
// 새 컷 후보, 결과물(폰용 남은 컷·CSV·컷 파일). 서버: /api/footage/* (scripts/ui/api_footage.py).
//
// - 기본 접힘. 연결 폴더가 있으면 접힌 상태에서도 요약 줄이 보인다. 처리 작업이 돌고 있으면 연다.
// - AI가 쓴 글(이유·대사·가이드·받아쓴 말)은 모두 h()의 글자 노드로만 넣는다(HTML로 해석하지 않음).
// - 판정 배지는 색만이 아니라 글자(찍음 / 확인 필요 / 남은 컷 / 못 찍음, 직접 정하면 "· 직접")를 보인다.
// - 화면 일부만 다시 그린다(slot). 다시 그릴 때 data-fkey로 포커스를 되돌린다.
// opts: { path, getConti() -> 콘티 응답, getHash(), onVerdicts(Map id -> {kind,user}),
//         onContiChanged({ cutId }) (새 컷을 넣은 뒤 편집기 다시 불러오기), onSelectCut(id) }
import { h, filesUrl } from "/static/dom.js";
import { sectionLabelName } from "/static/labels.js";
import { fmtClock, fmtRemain, monotonicFraction } from "/static/progress-utils.js";
import {
  clipNote, filterCuts, fmtRange, folderLine, frameUrl, jobProgress, kindGroup, mediaUrl, scoreText,
  suggestAfter, summaryLine, verdictLabel,
} from "/static/footage-utils.js";

const POLL_MS = 1000;
const POLL_MAX_MS = 15000;
const ACTIVE = new Set(["running", "awaiting"]);
const LONG_NOTE = "2시간 분량은 처리에 수십 분 걸릴 수 있어요. 끝난 파일은 다음에 건너뛰어요";
const STOP_NOTE = "지금 파일이 끝나면 멈춰요";
const MATCH_NOTE = "AI가 컷과 장면을 맞추는 중이에요. 이 단계는 멈출 수 없어요";
const STT_NOTE = "받아쓰기(faster-whisper)가 설치돼 있지 않아 대사 없이 사진만으로 맞췄어요. 플러그인 폴더에서 pip install faster-whisper를 실행한 뒤 다시 처리하면 대사로도 맞춰요";
const REMOVE_NOTE = "빼기는 연결만 끊어요 — 처리 결과와 판정은 남아요";
const KEYFRAME_NOTE = "시작이 몇 초 앞당겨질 수 있어요 (다시 압축하지 않고 잘라서, 가까운 키프레임부터 시작해요)";
const UNPLAYABLE = "이 영상은 브라우저에서 재생할 수 없어요";
const MISSING_SOURCE = "원본 없음";
const OUT_ROUTES = { remaining: "/api/footage/remaining", csv: "/api/footage/csv" };
const EDITOR_BUSY = "편집을 저장한 뒤 넣어 주세요";
const FILTERS = [["all", "전체"], ["remaining", "남은 컷"], ["review", "확인 필요"]];

let F = null;

const isActive = (job) => !!job && ACTIVE.has(job.status);
const enc = encodeURIComponent;

function alive(mine) {
  return !!mine && F === mine && mine.ctx.isCurrent();
}

function nowSec() {
  return Date.now() / 1000 + (F ? F.offset : 0);
}

function pollDelay(errors) {
  return errors > 0 ? Math.min(POLL_MS * 2 ** errors, POLL_MAX_MS) : POLL_MS;
}

function announce(text) {
  if (!F || !F.live) return;
  F.live.textContent = "";
  F.live.textContent = text;
}

// ---- slots and focus -------------------------------------------------------------------

function paint(name, build) {
  const slot = F.slots[name];
  if (!slot) return;
  const active = document.activeElement;
  const key = slot.contains(active) && active.dataset ? active.dataset.fkey || null : null;
  slot.replaceChildren(...build().filter(Boolean));
  if (key) {
    const again = slot.querySelector(`[data-fkey="${CSS.escape(key)}"]`);
    if (again && !again.disabled) again.focus({ preventScroll: true });
  }
}

function paintAll() {
  paintHead();
  F.body.hidden = !F.open;
  if (!F.open) return;
  paint("folders", folderSlot);
  paint("process", processSlot);
  paint("cuts", cutsSlot);
  paint("suggest", suggestSlot);
  paint("outputs", outputsSlot);
}

// aria-disabled (not disabled) for a request in flight: the clicked button keeps the focus.
function busy(key) {
  return F.busy.has(key) ? "true" : false;
}

function markBusy(key, on) {
  const el = F.root.querySelector(`[data-fkey="${CSS.escape(key)}"]`);
  if (!el) return;
  if (on) el.setAttribute("aria-disabled", "true"); else el.removeAttribute("aria-disabled");
}

function btn(label, attrs, onClick) {
  const b = h("button", { type: "button", class: "btn btn-sm", ...attrs }, label);
  b.addEventListener("click", onClick);
  return b;
}

// ---- data ------------------------------------------------------------------------------

function contiCut(id) {
  const data = F.opts.getConti();
  return data ? data.cuts.find((c) => c.id === id) || null : null;
}

function emitVerdicts() {
  const v = F.view;
  const map = new Map();
  if (v && (v.folders.length || v.cuts.some((c) => c.kind !== "missing"))) {
    for (const c of v.cuts) map.set(c.id, { kind: c.kind, user: c.user });
  }
  F.opts.onVerdicts(map);
}

function setView(view) {
  F.view = view;
  for (const key of [...F.players.keys()]) if (!F.playing.has(key)) F.players.delete(key);
  F.error = null;
  emitVerdicts();
}

async function load() {
  const mine = F;
  try {
    const view = await mine.ctx.api("GET", `/api/footage?path=${enc(mine.opts.path)}`);
    if (!alive(mine)) return;
    setView(view);
    if (!mine.job && isActive(view.job)) adoptJob(view.job, false);
    else if (!mine.job && view.job) mine.lastJob = view.job;
    if (!mine.clipsJob && view.clips_job) adoptClips(view.clips_job, false);
    if ((isActive(view.job) || isActive(view.clips_job)) && !mine.open) mine.open = true;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.error = e.message || "불러오지 못했어요";
  }
  if (alive(mine)) paintAll();
}

// One verdict / folder / suggestion action. Returns the response or null (error shown as a toast).
async function act(key, method, route, body) {
  const mine = F;
  if (mine.busy.has(key)) return null;
  mine.busy.add(key);
  markBusy(key, true);
  try {
    const res = await mine.ctx.api(method, route, body);
    if (!alive(mine)) return null;
    return res;
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return null;
    mine.ctx.toast(e.message, "error");
    return null;
  } finally {
    if (alive(mine)) { mine.busy.delete(key); markBusy(key, false); }
  }
}

// ---- head (toggle + summary) -----------------------------------------------------------

function paintHead() {
  const v = F.view;
  const toggle = h("button", {
    type: "button", class: "footage-toggle", "aria-expanded": String(F.open), "aria-controls": "footage-body", "data-fkey": "toggle",
  }, h("span", { class: "footage-caret", "aria-hidden": "true" }, F.open ? "▾" : "▸"), h("span", { class: "footage-title" }, "촬영 영상"));
  toggle.addEventListener("click", () => { F.open = !F.open; paintAll(); });
  const kids = [toggle];
  if (v && v.folders.length) {
    kids.push(h("span", { class: "fc-summary" }, summaryLine(v)));
    if (isActive(F.job)) kids.push(h("span", { class: "tag" }, "처리 중"));
  } else if (v && !F.open) {
    kids.push(h("span", { class: "muted fc-summary" }, "찍은 영상 폴더를 연결하면 남은 컷을 볼 수 있어요"));
  }
  const active = document.activeElement;
  const had = F.head.contains(active);
  F.head.replaceChildren(...kids);
  if (had) F.head.querySelector("[data-fkey=toggle]").focus({ preventScroll: true });
}

// ---- folders ---------------------------------------------------------------------------

function folderSlot() {
  if (F.error && !F.view) {
    return [h("p", { class: "error", role: "alert" }, F.error), btn("다시 시도", { "data-fkey": "retry" }, () => load())];
  }
  if (!F.view) return [h("p", { class: "muted", role: "status" }, "불러오는 중…")];
  const v = F.view;
  const out = [h("h3", { class: "fc-h" }, "연결한 폴더")];
  if (!v.folders.length) {
    out.push(h("p", { class: "muted hint" }, "아직 연결한 폴더가 없어요. 날짜별로 복사해 둔 촬영 폴더의 경로를 붙여 넣어 더하세요. 하위 폴더는 보지 않아요"));
  } else {
    const ul = h("ul", { class: "fc-folders" });
    for (const f of v.folders) {
      const key = `rm:${f.path}`;
      const rm = btn("빼기", { "aria-label": `${f.path} 빼기`, "data-fkey": key, disabled: isActive(F.job), "aria-disabled": busy(key) },
        () => removeFolder(f.path));
      ul.append(h("li", { class: "fc-folder" + (f.missing ? " is-missing" : "") },
        h("div", { class: "fc-folder-text" },
          h("span", { class: "fc-path", title: f.path }, f.path),
          h("span", { class: f.missing ? "error fc-folder-line" : "muted fc-folder-line" }, folderLine(f))),
        rm));
    }
    out.push(ul, h("p", { class: "muted hint" }, REMOVE_NOTE));
  }
  const inputId = "footage-folder";
  const input = h("input", {
    type: "text", id: inputId, class: "fc-input", value: F.folderText, autocomplete: "off", spellcheck: "false",
    placeholder: "예: D:\\촬영\\1일차", "aria-describedby": "footage-folder-err", "data-fkey": "folder-input",
    "aria-invalid": F.folderError ? "true" : false,
  });
  input.addEventListener("input", () => { F.folderText = input.value; });
  const add = h("button", { type: "submit", class: "btn", "data-fkey": "folder-add", "aria-disabled": busy("add") }, "폴더 더하기");
  const form = h("form", { class: "fc-add" },
    h("div", { class: "field" }, h("label", { for: inputId }, "촬영 폴더 경로"), h("div", { class: "fc-add-row" }, input, add)),
    h("p", { class: "error field-error", id: "footage-folder-err", role: "alert" }, F.folderError || ""));
  form.addEventListener("submit", (e) => { e.preventDefault(); addFolder(); });
  out.push(form);
  return out;
}

async function addFolder() {
  const mine = F;
  const folder = (mine.folderText || "").trim();
  if (!folder) { mine.folderError = "폴더 경로를 넣어 주세요"; paint("folders", folderSlot); focusKey("folder-input"); return; }
  if (mine.busy.has("add")) return;
  mine.busy.add("add");
  mine.folderError = "";
  paint("folders", folderSlot);
  try {
    const view = await mine.ctx.api("POST", "/api/footage/folders", { path: mine.opts.path, folder });
    if (!alive(mine)) return;
    mine.folderText = "";
    setView(view);
    announce("폴더를 더했어요");
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.folderError = e.message || "폴더를 더하지 못했어요";
  } finally {
    if (alive(mine)) {
      mine.busy.delete("add");
      paintAll();
      focusKey(mine.folderError ? "folder-input" : "folder-add");
    }
  }
}

async function removeFolder(folder) {
  const res = await act(`rm:${folder}`, "POST", "/api/footage/folders/remove", { path: F.opts.path, folder });
  if (!res) return;
  setView(res);
  paintAll();
  announce("폴더 연결을 끊었어요");
  focusKey("folder-input");
}

function focusKey(key) {
  const el = F && F.root.querySelector(`[data-fkey="${CSS.escape(key)}"]`);
  if (el) el.focus({ preventScroll: true });
}

// ---- processing job --------------------------------------------------------------------

function processSlot() {
  if (!F.view) return [];
  const v = F.view;
  const out = [];
  if (isActive(F.job)) out.push(progressCard(F.job));
  else {
    const pending = v.folders.reduce((n, f) => n + (f.pending || 0), 0);
    const start = h("button", {
      type: "button", class: "btn btn-primary", "data-fkey": "process",
      disabled: !v.folders.length || !!F.starting,
    }, F.starting ? "시작하는 중…" : "새 파일 처리하고 맞추기");
    start.addEventListener("click", startProcess);
    out.push(h("div", { class: "btn-row" }, start,
      v.folders.length ? h("span", { class: "muted hint" }, pending ? `새 파일 ${pending}개` : "새 파일 없음 · 다시 맞추기만 해요") : null),
    h("p", { class: "muted hint" }, LONG_NOTE));
    if (F.jobError) out.push(h("p", { class: "error", role: "alert" }, F.jobError));
    const last = F.lastJob;
    if (last && !isActive(last)) out.push(lastJobNote(last));
  }
  if (v.stt_missing) out.push(h("p", { class: "fc-note", role: "note" }, STT_NOTE));
  return out;
}

function lastJobNote(job) {
  const s = job.summary || {};
  const failed = (job.items || []).filter((i) => i.id !== "match" && i.state === "failed");
  let text;
  if (job.status === "done") text = `지난 처리: 새로 ${s.processed || 0}개 · 이미 처리 ${s.cached || 0}개${s.failed ? ` · 못 읽음 ${s.failed}개` : ""}`;
  else if (job.status === "stopped") text = "멈췄어요. 끝난 파일은 다음에 건너뛰어요";
  else text = `처리가 중단됐어요: ${job.message || "알 수 없는 오류"}`;
  const box = h("div", { class: "fc-last" }, h("p", { class: job.status === "failed" ? "error" : "muted" }, text));
  if (failed.length) {
    box.append(h("ul", { class: "fc-fail-list" }, ...failed.map((i) => h("li", {}, h("span", { class: "fc-file" }, i.title || i.id), ` — ${i.message || "실패"}`))));
  }
  return box;
}

function progressCard(job) {
  const now = nowSec();
  const p = jobProgress(job, now) || { files: 0, done: 0, current: null, matching: false, fraction: 0, remaining: null };
  F.ringMax = monotonicFraction(F.ringMax, p.fraction);
  const pct = F.ringMax === null ? null : Math.min(99, Math.round(F.ringMax * 100));
  const ring = h("div", { class: "prog-ring", role: "progressbar", "aria-label": "촬영 영상 처리 진행률",
    "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": pct === null ? false : String(pct) },
  h("span", {}, pct === null ? "…" : `${pct}%`));
  ring.style.setProperty("--p", String(pct || 0));
  let remain;
  if (p.matching) remain = "맞추는 중";
  else if (p.remaining !== null) remain = `${fmtRemain(p.remaining, true)} 남음 · ${fmtClock(now + p.remaining)}쯤 끝`;
  else remain = "남은 시간 계산 중";
  const cur = p.current;
  const nowLine = p.matching ? MATCH_NOTE : cur ? `지금: ${cur.title || cur.id} — ${cur.message || "준비 중"}` : "다음 파일을 준비하는 중";
  const stopping = !!F.stopping || job.status === "stopped";
  const stop = h("button", {
    type: "button", class: "btn", "data-fkey": "stop", "aria-disabled": String(stopping || p.matching),
  }, stopping ? "멈추는 중…" : "멈추기");
  stop.addEventListener("click", () => { if (!F.stopping && !p.matching) stopJob(); });
  const foot = h("div", { class: "prog-foot" }, stop);
  if (stopping) foot.append(h("p", { class: "muted hint", role: "status" }, STOP_NOTE));
  return h("section", { class: "card job-item prog-card is-running fc-prog", "aria-label": "촬영 영상 처리 중" },
    h("div", { class: "item-line" }, h("span", { class: "item-title" }, "촬영 영상 처리 중"),
      h("span", { class: "state state-running" }, `파일 ${p.done}/${p.files}`)),
    h("div", { class: "prog-hero" }, ring, h("div", { class: "prog-text" },
      h("p", { class: "prog-remain" }, remain), h("p", { class: "prog-now" }, nowLine))),
    foot);
}

function adoptJob(job, announceEnd = true) {
  const mine = F;
  const was = isActive(mine.job);
  if (!mine.job || mine.job.id !== job.id) mine.ringMax = null;
  if (typeof job.now === "number") mine.offset = job.now - Date.now() / 1000;
  mine.job = job;
  if (isActive(job)) { schedulePoll(); return; }
  mine.lastJob = job;
  mine.job = null;
  mine.stopping = false;
  if (was && announceEnd) {
    if (job.status === "done") {
      const c = (job.summary || {}).counts;
      announce(c ? `처리를 마쳤어요 — 찍음 ${c.filmed} · 확인 필요 ${c.review} · 남은 컷 ${c.missing}` : "처리를 마쳤어요");
    } else if (job.status === "stopped") announce("처리를 멈췄어요");
    else announce(`처리 실패: ${job.message || ""}`);
    load();
  }
}

async function startProcess() {
  const mine = F;
  if (mine.starting || isActive(mine.job)) return;
  mine.starting = true;
  mine.jobError = "";
  paint("process", processSlot);
  try {
    const res = await mine.ctx.api("POST", "/api/footage/process", { path: mine.opts.path });
    if (!alive(mine)) return;
    mine.pollErrors = 0;
    adoptJob(res.job);
    announce("처리를 시작했어요");
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.jobError = e.message || "시작하지 못했어요";
  } finally {
    if (alive(mine)) {
      mine.starting = false;
      paintAll();
      focusKey(isActive(mine.job) ? "stop" : "process");
    }
  }
}

function schedulePoll() {
  const mine = F;
  if (mine.timer) clearTimeout(mine.timer);
  mine.timer = setTimeout(async () => {
    mine.timer = null;
    if (!alive(mine) || !mine.job) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${enc(mine.job.id)}`);
      if (!alive(mine)) return;
      mine.pollErrors = 0;
      adoptJob(data.job);
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.job = null;
        mine.jobError = "작업을 찾을 수 없어요. 서버가 다시 켜졌다면 다시 처리해 주세요";
        load();
        return;
      }
      mine.pollErrors = (mine.pollErrors || 0) + 1;
      if (isActive(mine.job)) schedulePoll();
    }
    if (alive(mine)) { paintHead(); if (mine.open) paint("process", processSlot); }
  }, pollDelay(mine.pollErrors || 0));
}

async function stopJob() {
  const mine = F;
  if (!mine.job || mine.stopping) return;
  mine.stopping = true;
  paint("process", processSlot);
  announce(STOP_NOTE);
  try {
    await mine.ctx.api("POST", `/api/jobs/${enc(mine.job.id)}/cancel`, {});
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error !== "not_running") { mine.stopping = false; mine.ctx.toast(e.message, "error"); paint("process", processSlot); }
  }
}

// ---- cuts: summary, filter, rows, review cards --------------------------------------------

function cutsSlot() {
  const v = F.view;
  if (!v || !v.folders.length) return [];
  const matched = !!v.matched_at || v.cuts.some((c) => c.user);
  const out = [h("h3", { class: "fc-h" }, "컷별 판정")];
  if (!matched) {
    out.push(h("p", { class: "muted hint" }, "아직 맞추지 않았어요. 위의 ‘새 파일 처리하고 맞추기’를 누르면 컷마다 찍었는지 알려 드려요"));
    return out;
  }
  const counts = { all: v.cuts.length, remaining: v.summary.missing, review: v.summary.review };
  const group = h("div", { class: "fc-filter", role: "group", "aria-label": "컷 보기" });
  for (const [key, label] of FILTERS) {
    const b = h("button", { type: "button", class: "fc-filter-btn", "aria-pressed": String(F.filter === key), "data-fkey": `filter:${key}` },
      `${label} ${counts[key]}`);
    b.addEventListener("click", () => { F.filter = key; paint("cuts", cutsSlot); });
    group.append(b);
  }
  out.push(h("p", { class: "fc-summary-line" }, summaryLine(v)), group);
  const shown = filterCuts(v.cuts, F.filter);
  if (!shown.length) {
    out.push(h("p", { class: "muted fc-empty" }, F.filter === "review" ? "확인할 컷이 없어요" : "남은 컷이 없어요 — 다 찍었어요"));
    return out;
  }
  out.push(h("ol", { class: "fc-cuts", "aria-label": "컷별 판정 목록" }, ...shown.map(cutItem)));
  return out;
}

function badge(c) {
  return h("span", { class: `v-badge v-${kindGroup(c.kind)}${c.kind === "not_filmed" ? " v-not" : ""}` },
    h("span", { class: "v-dot", "aria-hidden": "true" }), verdictLabel(c));
}

function missingTag(item) {
  return item && item.missing_source ? h("span", { class: "badge fc-missing" }, MISSING_SOURCE) : null;
}

function cutItem(c) {
  const cc = contiCut(c.id);
  const data = F.opts.getConti();
  const sec = cc && data && data.fm.script[cc.section] ? sectionLabelName(data.fm.script[cc.section].label) : "";
  const line = cc ? cc.line || "(대사 없음)" : "";
  const jump = h("button", { type: "button", class: "fc-n", "data-fkey": `jump:${c.id}`, "aria-label": `컷 ${c.n} 편집기에서 보기` }, `#${c.n}`);
  jump.addEventListener("click", () => F.opts.onSelectCut(c.id));
  const top = h("div", { class: "fc-row-top" }, jump, sec ? h("span", { class: "tag" }, sec) : null,
    h("span", { class: "fc-line" + (cc && cc.line ? "" : " muted") }, line), badge(c));
  const li = h("li", { class: `fc-row is-${kindGroup(c.kind)}` }, top);
  if (c.choice) {
    li.append(h("p", { class: "fc-choice" }, h("span", { class: "muted" }, "고른 구간 "),
      h("span", { class: "fc-file" }, c.choice.name || "알 수 없는 파일"), ` ${fmtRange(c.choice.start, c.choice.end)} `, missingTag(c.choice)));
  }
  if (c.reason) li.append(h("p", { class: "muted fc-reason" }, c.reason));
  const actions = h("div", { class: "btn-row fc-actions" });
  if (c.choice && !c.choice.missing_source) actions.append(playButton(`choice:${c.id}`));
  if (c.kind !== "not_filmed") {
    const key = `nf:${c.id}`;
    actions.append(btn("못 찍음으로", { "data-fkey": key, "aria-disabled": busy(key), "aria-label": `컷 ${c.n} 못 찍음으로` },
      () => verdict(key, "/api/footage/not-filmed", { cut: c.id }, `컷 ${c.n}을 못 찍음으로 바꿨어요`)));
  }
  if (c.user) {
    const key = `reset:${c.id}`;
    actions.append(btn("AI 판정으로 되돌리기", { "data-fkey": key, "aria-disabled": busy(key), "aria-label": `컷 ${c.n} AI 판정으로 되돌리기` },
      () => verdict(key, "/api/footage/reset", { cut: c.id }, `컷 ${c.n}을 AI 판정으로 되돌렸어요`)));
  }
  if (actions.childElementCount) top.append(actions);
  if (c.choice && F.playing.has(`choice:${c.id}`)) li.append(player(`choice:${c.id}`, c.choice));
  const cands = c.candidates || [];
  if (cands.length) {
    const list = h("ul", { class: "fc-cands", "aria-label": `컷 ${c.n} 후보` }, ...cands.map((x, i) => candItem(c, x, i)));
    if (c.kind === "review") li.append(h("div", { class: "fc-card" }, h("p", { class: "fc-card-title" }, `후보 ${cands.length}개 — 맞는 구간을 고르세요`), list));
    else {
      const det = h("details", { class: "fc-more" }, h("summary", {}, `후보 ${cands.length}개 보기`), list);
      if (F.expanded.has(c.id)) det.open = true;
      det.addEventListener("toggle", () => { if (det.open) F.expanded.add(c.id); else F.expanded.delete(c.id); });
      li.append(det);
    }
  }
  return li;
}

function strip(item, label) {
  const frames = (item.frames || []).slice(0, 4);
  if (!frames.length) return h("div", { class: "fc-strip is-empty", "aria-hidden": "true" });
  return h("div", { class: "fc-strip", role: "img", "aria-label": label },
    ...frames.map((rel) => h("img", { src: frameUrl(item.fid, rel), alt: "", loading: "lazy" })));
}

function candItem(c, x, i) {
  const key = `cand:${c.id}:${i}`;
  const chosen = c.choice && c.choice.fid === x.fid && c.choice.start === x.start && c.choice.end === x.end;
  const name = x.name || "알 수 없는 파일";
  const meta = h("p", { class: "fc-meta" }, h("span", { class: "fc-file" }, name), ` ${fmtRange(x.start, x.end)}`,
    x.score ? h("span", { class: `badge fc-score fc-score-${x.score}` }, scoreText(x.score)) : null, missingTag(x));
  const body = h("div", { class: "fc-cand-body" }, meta);
  if (x.text) body.append(h("p", { class: "fc-speech" }, h("span", { class: "muted" }, "받아쓴 말 "), `“${x.text}”`));
  if (x.why) body.append(h("p", { class: "muted fc-why" }, x.why));
  const actions = h("div", { class: "btn-row" });
  if (chosen) actions.append(h("span", { class: "badge fc-chosen" }, "고른 구간"));
  else {
    actions.append(btn("이걸로", { "data-fkey": `pick:${key}`, "aria-disabled": busy(`pick:${key}`), "aria-label": `컷 ${c.n}: ${name} ${fmtRange(x.start, x.end)} 이걸로` },
      () => verdict(`pick:${key}`, "/api/footage/choose", { cut: c.id, fid: x.fid, start: x.start, end: x.end }, `컷 ${c.n}에 이 구간을 골랐어요`)));
  }
  if (!x.missing_source) actions.append(playButton(key));
  body.append(actions);
  const li = h("li", { class: "fc-cand" }, strip(x, `${name} ${fmtRange(x.start, x.end)} 장면 사진`), body);
  if (F.playing.has(key)) li.append(player(key, x));
  return li;
}

async function verdict(key, route, body, done) {
  const res = await act(key, "POST", route, { path: F.opts.path, ...body });
  if (!res) return;
  setView(res);
  paintAll();
  announce(done);
}

// ---- playback ------------------------------------------------------------------------------

function playButton(key) {
  const on = F.playing.has(key);
  const b = h("button", { type: "button", class: "btn btn-sm", "aria-expanded": String(on), "data-fkey": `play:${key}` }, on ? "재생 닫기" : "재생");
  b.addEventListener("click", () => {
    if (F.playing.has(key)) { F.playing.delete(key); F.players.delete(key); } else F.playing.add(key);
    paintAll();
    if (F.playing.has(key)) {
      const v = F.root.querySelector(`[data-play="${CSS.escape(key)}"] video`);
      if (v) v.focus({ preventScroll: true });
    }
  });
  return b;
}

// Inline <video> for a range (#t=start,end). When the browser cannot decode it (HEVC in Chrome) the photos stay.
function player(key, item) {
  const sig = `${item.fid}@${item.start}-${item.end}`;
  const cached = F.players.get(key);
  if (cached && cached.sig === sig) return cached.box;
  const box = h("div", { class: "fc-player", "data-play": key });
  F.players.set(key, { sig, box });
  if (F.unplayable.has(key)) {
    box.append(strip(item, "장면 사진"), h("p", { class: "muted hint" }, UNPLAYABLE));
    return box;
  }
  const video = h("video", {
    class: "fc-video", controls: true, preload: "metadata", playsinline: true,
    src: mediaUrl(item.fid, F.opts.path, item.start, item.end), "aria-label": `${item.name || "영상"} ${fmtRange(item.start, item.end)} 재생`,
  });
  const mine = F;
  video.addEventListener("error", () => {
    if (!alive(mine)) return;
    mine.unplayable.add(key);
    const had = box.contains(document.activeElement);
    box.replaceChildren(strip(item, "장면 사진"), h("p", { class: "muted hint", role: "status" }, UNPLAYABLE));
    if (had) focusKey(`play:${key}`);
  });
  box.append(video);
  return box;
}

// ---- new-cut suggestions ---------------------------------------------------------------------

function suggestSlot() {
  const v = F.view;
  if (!v || !v.suggestions.length) return [];
  const data = F.opts.getConti();
  const cuts = data ? data.cuts : [];
  const out = [h("h3", { class: "fc-h" }, `새 컷 후보 ${v.suggestions.length}개`),
    h("p", { class: "muted hint" }, "콘티에 없지만 쓸 만한 장면이에요. 넣으면 그 컷은 이 구간으로 ‘찍음 · 직접’이 돼요")];
  const ul = h("ul", { class: "fc-suggests" });
  for (const s of v.suggestions) {
    const k = s.key;
    if (!F.after.has(k) || !cuts.some((c) => c.id === F.after.get(k))) F.after.set(k, suggestAfter(v.cuts, s));
    const selId = `after-${CSS.escape(k).replace(/[^\w-]/g, "_")}`;
    const select = h("select", { id: selId, class: "fc-select", "data-fkey": `after:${k}` },
      ...cuts.map((c) => h("option", { value: c.id, selected: c.id === F.after.get(k) }, `#${c.n} ${(c.line || "(대사 없음)").slice(0, 24)}`)));
    select.addEventListener("change", () => F.after.set(k, select.value));
    const name = s.name || "알 수 없는 파일";
    const body = h("div", { class: "fc-cand-body" },
      h("p", { class: "fc-meta" }, h("span", { class: "fc-file" }, name), ` ${fmtRange(s.start, s.end)}`, missingTag(s)));
    if (s.text) body.append(h("p", { class: "fc-speech" }, h("span", { class: "muted" }, "받아쓴 말 "), `“${s.text}”`));
    const dl = h("dl", { class: "fc-proposal" });
    if (s.line) dl.append(h("dt", {}, "대사"), h("dd", {}, s.line));
    if (s.guide) dl.append(h("dt", {}, "가이드"), h("dd", {}, s.guide));
    if (s.size) dl.append(h("dt", {}, "샷 크기"), h("dd", {}, s.size));
    if (dl.childElementCount) body.append(dl);
    const insKey = `ins:${k}`;
    const hideKey = `hide:${k}`;
    const insert = btn("이 컷 뒤에 넣기", { "data-fkey": insKey, disabled: !cuts.length, "aria-disabled": busy(insKey) },
      () => insertSuggestion(s));
    const hide = btn("숨기기", { "data-fkey": hideKey, "aria-disabled": busy(hideKey) }, () => hideSuggestion(s));
    const actions = h("div", { class: "fc-insert" },
      h("div", { class: "field" }, h("label", { for: selId }, "넣을 자리"), select),
      h("div", { class: "btn-row" }, insert, hide, s.missing_source ? null : playButton(`sug:${k}`)));
    body.append(actions);
    const li = h("li", { class: "fc-cand fc-suggest" }, strip(s, `${name} ${fmtRange(s.start, s.end)} 장면 사진`), body);
    if (F.playing.has(`sug:${k}`)) li.append(player(`sug:${k}`, s));
    ul.append(li);
  }
  out.push(ul);
  return out;
}

async function hideSuggestion(s) {
  const res = await act(`hide:${s.key}`, "POST", "/api/footage/suggestions/hide", { path: F.opts.path, key: s.key });
  if (!res) return;
  setView(res);
  paintAll();
  announce("새 컷 후보를 숨겼어요");
}

async function insertSuggestion(s) {
  const mine = F;
  const key = `ins:${s.key}`;
  if (mine.busy.has(key)) return;
  if (mine.opts.getEditorBusy()) { mine.ctx.toast(EDITOR_BUSY, "error"); return; }
  const after = mine.after.get(s.key);
  mine.busy.add(key);
  markBusy(key, true);
  try {
    const res = await mine.ctx.api("POST", "/api/footage/suggestions/insert",
      { path: mine.opts.path, key: s.key, after_cut: after, hash: mine.opts.getHash() });
    if (!alive(mine)) return;
    setView(res.footage);
    announce("새 컷을 넣었어요");
    mine.ctx.toast("새 컷을 콘티에 넣고 이 구간으로 정했어요", "info");
    await mine.opts.onContiChanged({ cutId: res.cut_id });
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error === "cut_not_linked") {
      mine.ctx.toast(e.message, "error");
      await mine.opts.onContiChanged({ cutId: e.data && e.data.cut_id });
      await load();
    } else if (e.error === "conti_changed") {
      mine.ctx.toast("콘티가 그사이 바뀌었어요. 다시 불러왔으니 한 번 더 눌러 주세요", "error");
      await mine.opts.onContiChanged({});
    } else {
      mine.ctx.toast(e.message, "error");
    }
  } finally {
    if (alive(mine)) { mine.busy.delete(key); paintAll(); }
  }
}

// ---- outputs ---------------------------------------------------------------------------------

function outputsSlot() {
  const v = F.view;
  if (!v || !v.folders.length) return [];
  const o = v.outputs || {};
  const clipsBusy = isActive(F.clipsJob) || !!F.clipsStarting;
  const remaining = btn("폰용 남은 컷 만들기", { class: "btn", "data-fkey": "out:remaining", "aria-disabled": busy("out:remaining") }, () => writeOut("remaining"));
  const csv = btn("CSV 받기", { class: "btn", "data-fkey": "out:csv", "aria-disabled": busy("out:csv") }, () => writeOut("csv"));
  const clips = btn("컷 파일 만들기", { class: "btn", "data-fkey": "out:clips", disabled: clipsBusy || isActive(F.job) }, startClips);
  const out = [h("h3", { class: "fc-h" }, "편집 준비"),
    h("p", { class: "muted hint" }, "폰용 남은 컷은 한 파일(HTML)이라 카톡·드라이브로 폰에 옮겨 현장에서 보면 돼요"),
    h("div", { class: "btn-row" }, remaining, csv, clips)];
  const links = h("ul", { class: "fc-links" });
  if (o.remaining_html) links.append(h("li", {}, "폰용 남은 컷: ", h("a", { href: filesUrl(o.remaining_html), target: "_blank", rel: "noopener", "data-fkey": "link:remaining" }, o.remaining_html.split("/").pop())));
  if (o.csv) links.append(h("li", {}, "위치 목록: ", h("a", { href: filesUrl(o.csv), download: o.csv.split("/").pop(), "data-fkey": "link:csv" }, o.csv.split("/").pop())));
  if (o.clips_dir) links.append(h("li", {}, "컷 파일 폴더: ", h("code", {}, o.clips_dir)));
  if (links.childElementCount) out.push(links);
  if (F.clipsError) out.push(h("p", { class: "error", role: "alert" }, F.clipsError));
  if (F.clipsJob) out.push(clipsBox(F.clipsJob));
  return out;
}

async function writeOut(kind) {
  const res = await act(`out:${kind}`, "POST", OUT_ROUTES[kind], { path: F.opts.path });
  if (!res) return;
  setView(res.footage);
  paintAll();
  announce(kind === "csv" ? "CSV를 만들었어요" : "폰용 남은 컷을 만들었어요");
  focusKey(`link:${kind}`);
}

function clipsBox(job) {
  const items = job.items || [];
  const box = h("div", { class: "fc-clips", role: "status", "aria-live": "polite" });
  if (isActive(job)) {
    const done = items.filter((i) => ["done", "skipped", "failed"].includes(i.state)).length;
    const cur = items.find((i) => i.state === "running");
    const stop = h("button", { type: "button", class: "btn btn-sm", "data-fkey": "clips-stop", "aria-disabled": String(!!F.clipsStopping) },
      F.clipsStopping ? "멈추는 중…" : "멈추기");
    stop.addEventListener("click", stopClips);
    box.append(h("p", { class: "fc-clips-line" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      `컷 파일 만드는 중 ${done}/${items.length}${cur ? ` — 지금: ${cur.title}` : ""}`, stop));
    if (F.clipsStopping) box.append(h("p", { class: "muted hint" }, "지금 컷이 끝나면 멈춰요"));
  } else if (job.status === "stopped") box.append(h("p", { class: "muted" }, "컷 파일 만들기를 멈췄어요"));
  else if (job.status === "failed") box.append(h("p", { class: "error" }, job.message || "컷 파일 만들기가 중단됐어요"));
  const finished = items.filter((i) => ["done", "skipped", "failed"].includes(i.state));
  if (finished.length) {
    // Summary first; files that need a look (re-encoded, skipped, failed) stay visible, the rest folds away.
    const made = finished.filter((i) => i.state === "done");
    const notable = finished.filter((i) => i.state !== "done" || (i.preview && i.preview.reencoded));
    const parts = [`컷 파일 ${made.length}개`];
    const reenc = made.filter((i) => i.preview && i.preview.reencoded).length;
    const skipped = finished.filter((i) => i.state === "skipped").length;
    const failed = finished.filter((i) => i.state === "failed").length;
    if (reenc) parts.push(`다시 압축함 ${reenc}개`);
    if (skipped) parts.push(`건너뜀 ${skipped}개`);
    if (failed) parts.push(`실패 ${failed}개`);
    box.append(h("p", { class: "fc-clips-sum" }, parts.join(" · ")), h("p", { class: "muted hint" }, KEYFRAME_NOTE));
    if (notable.length) box.append(h("ul", { class: "fc-clip-list" }, ...notable.map(clipItem)));
    if (made.length) {
      const det = h("details", { class: "fc-more" }, h("summary", {}, `만든 파일 ${made.length}개 보기`),
        h("ul", { class: "fc-clip-list" }, ...made.map(clipItem)));
      if (F.clipsOpen) det.open = true;
      det.addEventListener("toggle", () => { F.clipsOpen = det.open; });
      box.append(det);
    }
  }
  return box;
}

function clipItem(i) {
  const p = i.preview;
  const li = h("li", { class: `fc-clip is-${i.state}` });
  if (i.state === "done" && p && p.url) li.append(h("a", { href: p.url, target: "_blank", rel: "noopener" }, p.file.split("/").pop()));
  else li.append(h("span", { class: "fc-file" }, i.title || i.id));
  if (i.state === "done" && p && p.reencoded) li.append(h("span", { class: "badge fc-reenc" }, "다시 압축함"));
  else {
    const note = clipNote(i);
    if (note) li.append(h("span", { class: i.state === "failed" ? "error fc-clip-note" : "muted fc-clip-note" }, note));
  }
  return li;
}

function adoptClips(job, announceEnd = true) {
  const mine = F;
  const was = isActive(mine.clipsJob);
  mine.clipsJob = job;
  if (isActive(job)) { scheduleClipsPoll(); return; }
  mine.clipsStopping = false;
  if (was && announceEnd) {
    const s = job.summary || {};
    announce(job.status === "done" ? `컷 파일 ${s.made || 0}개를 만들었어요` : "컷 파일 만들기가 끝났어요");
    load();
  }
}

async function startClips() {
  const mine = F;
  if (mine.clipsStarting || isActive(mine.clipsJob)) return;
  mine.clipsStarting = true;
  mine.clipsError = "";
  paint("outputs", outputsSlot);
  try {
    const res = await mine.ctx.api("POST", "/api/footage/clips", { path: mine.opts.path });
    if (!alive(mine)) return;
    mine.clipsErrors = 0;
    adoptClips(res.job);
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    mine.clipsError = e.message || "시작하지 못했어요";
  } finally {
    if (alive(mine)) { mine.clipsStarting = false; paint("outputs", outputsSlot); }
  }
}

function scheduleClipsPoll() {
  const mine = F;
  if (mine.clipsTimer) clearTimeout(mine.clipsTimer);
  mine.clipsTimer = setTimeout(async () => {
    mine.clipsTimer = null;
    if (!alive(mine) || !mine.clipsJob) return;
    try {
      const data = await mine.ctx.api("GET", `/api/jobs/${enc(mine.clipsJob.id)}`);
      if (!alive(mine)) return;
      mine.clipsErrors = 0;
      adoptClips(data.job);
    } catch (e) {
      if (!alive(mine) || e.error === "aborted") return;
      if (e.error === "job_not_found" || e.status === 404) {
        mine.clipsJob = null;
        mine.clipsError = "작업을 찾을 수 없어요. 서버가 다시 켜졌다면 다시 만들어 주세요";
        paint("outputs", outputsSlot);
        return;
      }
      mine.clipsErrors = (mine.clipsErrors || 0) + 1;
      if (isActive(mine.clipsJob)) scheduleClipsPoll();
    }
    if (alive(mine) && mine.open) paint("outputs", outputsSlot);
  }, pollDelay(mine.clipsErrors || 0));
}

async function stopClips() {
  const mine = F;
  if (!mine.clipsJob || mine.clipsStopping) return;
  mine.clipsStopping = true;
  paint("outputs", outputsSlot);
  try {
    await mine.ctx.api("POST", `/api/jobs/${enc(mine.clipsJob.id)}/cancel`, {});
  } catch (e) {
    if (!alive(mine) || e.error === "aborted") return;
    if (e.error !== "not_running") { mine.clipsStopping = false; mine.ctx.toast(e.message, "error"); paint("outputs", outputsSlot); }
  }
}

// ---- component contract ------------------------------------------------------------------------

function stopTimers() {
  if (F && F.timer) { clearTimeout(F.timer); F.timer = null; }
  if (F && F.clipsTimer) { clearTimeout(F.clipsTimer); F.clipsTimer = null; }
}

export function mountFootageArea(ctx, opts) {
  stopTimers();
  const live = h("div", { class: "sr-only", "aria-live": "polite", "aria-atomic": "true" });
  const head = h("div", { class: "footage-head" });
  const slots = {};
  for (const name of ["folders", "process", "cuts", "suggest", "outputs"]) slots[name] = h("div", { class: `fc-slot fc-${name}` });
  const body = h("div", { class: "footage-body", id: "footage-body", hidden: true }, ...Object.values(slots));
  const root = h("section", { class: "footage card", "aria-label": "촬영 영상" }, live, head, body);
  F = {
    ctx, opts, root, head, body, slots, live, open: false, view: null, error: null,
    filter: "all", folderText: "", folderError: "", busy: new Set(), playing: new Set(), players: new Map(), unplayable: new Set(),
    expanded: new Set(), after: new Map(), job: null, lastJob: null, jobError: "", starting: false, stopping: false,
    timer: null, pollErrors: 0, offset: 0, ringMax: null,
    clipsJob: null, clipsTimer: null, clipsErrors: 0, clipsStarting: false, clipsStopping: false, clipsError: "",
  };
  paintAll();
  load();
  const mine = F;
  return {
    el: root,
    // The conti changed (cut edit, reload, restore): numbers and lines follow it.
    refresh: () => { if (alive(mine)) load(); },
    // Repaint with the current conti (lines, sections) without asking the server.
    repaint: () => { if (alive(mine)) paintAll(); },
    unmount: () => { if (F === mine) { stopTimers(); F = null; } },
  };
}
