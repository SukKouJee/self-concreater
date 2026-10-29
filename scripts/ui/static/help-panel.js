// Help side panel (every screen): [이 화면] tab = docs/help/<page>.md, [챗봇] tab = Claude answers grounded in the
// workspace status. Markdown goes through renderMarkdown, everything else is text nodes.
//
//   createHelpPanel(root, getCategory) -> { setPage(page), onCategory(name), toggle(open?) }
//   Expects the toggle #help-toggle (inside the header help menu, app.js), the menu button #help-button (focus
//   returns there: the toggle is hidden once the menu closes) and the scrim #help-scrim (index.html).
//
// Stale-answer rule: `epoch` is bumped on every page change, cancel and new question. Any async result that was
// started under an older epoch is dropped (and its job cancelled), so a late answer never lands in another screen.
import { api } from "/static/api.js";
import { h, fill } from "/static/dom.js";
import { renderMarkdown } from "/static/md.js";
import { helpPage, trimHistory, pollDelay } from "/static/help-utils.js";

// helpPage / trimHistory live in help-utils.js (no imports, importable from node); re-exported here.
export { helpPage, trimHistory };

const OPEN_KEY = "ytc.helpOpen";
const MAX_QUESTION = 500;
const MAX_POLL_ERRORS = 5;
const OVERLAY_QUERY = "(max-width: 1099px)";
const SHORTCUTS = ["이 화면은?", "사용 예", "해보면 어떻게 돼?"];
const TABS = [["screen", "이 화면"], ["chat", "챗봇"]];

function readOpen() {
  try { return localStorage.getItem(OPEN_KEY) === "1"; } catch { return false; }
}
function storeOpen(v) {
  try { localStorage.setItem(OPEN_KEY, v ? "1" : "0"); } catch { /* storage unavailable */ }
}

export function createHelpPanel(root, getCategory) {
  const scrim = document.getElementById("help-scrim");
  const toggleBtn = document.getElementById("help-toggle");
  const media = typeof window.matchMedia === "function" ? window.matchMedia(OVERLAY_QUERY) : null;
  const isOverlay = () => !!media && media.matches;

  const S = {
    open: false, tab: "screen", page: null, category: getCategory() || null, loadedPage: null, screenSeq: 0,
    conv: [], epoch: 0, jobId: null, pending: false, timer: null, pollErrors: 0, lastQuestion: "",
  };

  // ---- skeleton ------------------------------------------------------------------------------

  const titleEl = h("span", { class: "help-title muted small" });
  const closeBtn = h("button", { type: "button", class: "btn help-close", "aria-label": "도움말 닫기" }, "닫기");
  const tablist = h("div", { class: "help-tabs", role: "tablist", "aria-label": "도움말 보기" });
  const screenBody = h("div", { class: "help-body" });
  const screenPanel = h("div", {
    class: "help-pane", role: "tabpanel", id: "help-pane-screen", "aria-labelledby": "help-tab-screen", tabindex: "0",
  }, screenBody);

  const emptyHint = h("p", { class: "muted small help-empty" },
    "이 화면에 대해 궁금한 점을 물어보세요. 답은 도움말과 지금 작업 폴더 상태를 바탕으로 만들어집니다.");
  const log = h("div", { class: "help-log", "aria-live": "polite", "aria-label": "대화" });
  const status = h("div", { class: "help-status", role: "status" });
  const input = h("textarea", {
    id: "help-input", class: "help-input", rows: "3", maxlength: String(MAX_QUESTION), "aria-label": "질문",
    "aria-describedby": "help-input-hint", placeholder: "궁금한 점을 적어 주세요",
  });
  const sendBtn = h("button", { type: "submit", class: "btn btn-primary" }, "보내기");
  const shortcutBtns = SHORTCUTS.map((text) => {
    const b = h("button", { type: "button", class: "btn help-shortcut" }, text);
    b.addEventListener("click", () => ask(text));
    return b;
  });
  const form = h("form", { class: "help-form" },
    input,
    h("div", { class: "btn-row help-send-row" }, sendBtn,
      h("span", { class: "muted small", id: "help-input-hint" }, "Enter로 보내기, Shift+Enter로 줄바꿈")));
  const chatPanel = h("div", {
    class: "help-pane help-chat", role: "tabpanel", id: "help-pane-chat", "aria-labelledby": "help-tab-chat",
  }, h("div", { class: "btn-row help-shortcuts" }, ...shortcutBtns), emptyHint, log, status, form);

  const panes = { screen: screenPanel, chat: chatPanel };
  const tabBtns = TABS.map(([key, label]) => {
    const b = h("button", {
      type: "button", class: "help-tab", role: "tab", id: `help-tab-${key}`, "aria-controls": `help-pane-${key}`, "data-tab": key,
    }, label);
    b.addEventListener("click", () => selectTab(key));
    b.addEventListener("keydown", (ev) => {
      const i = TABS.findIndex(([k]) => k === key);
      let next = null;
      if (ev.key === "ArrowRight") next = TABS[(i + 1) % TABS.length][0];
      else if (ev.key === "ArrowLeft") next = TABS[(i + TABS.length - 1) % TABS.length][0];
      else if (ev.key === "Home") next = TABS[0][0];
      else if (ev.key === "End") next = TABS[TABS.length - 1][0];
      if (next) { ev.preventDefault(); selectTab(next, true); }
    });
    return b;
  });
  tablist.append(...tabBtns);

  fill(root,
    h("div", { class: "help-head" }, h("h2", { id: "help-heading" }, "도움말"), titleEl, closeBtn),
    tablist, screenPanel, chatPanel);
  root.setAttribute("aria-labelledby", "help-heading");

  // ---- open / close / tabs -------------------------------------------------------------------

  function syncOpenUi() {
    root.hidden = !S.open;
    document.body.classList.toggle("help-open", S.open);
    if (scrim) scrim.hidden = !(S.open && isOverlay());
    if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(S.open));
  }

  function setOpen(open, { remember = true, returnFocus = false } = {}) {
    S.open = open;
    if (remember) storeOpen(open);
    syncOpenUi();
    if (open) {
      ensureScreen();
      if (isOverlay()) {
        const sel = tabBtns.find((b) => b.dataset.tab === S.tab);
        if (sel) sel.focus();
      }
    } else if (returnFocus) {
      const back = document.getElementById("help-button") || toggleBtn;
      if (back) back.focus();
    }
  }

  function selectTab(key, focusTab = false) {
    S.tab = key;
    for (const b of tabBtns) {
      const on = b.dataset.tab === key;
      b.setAttribute("aria-selected", String(on));
      b.setAttribute("tabindex", on ? "0" : "-1");
      if (on && focusTab) b.focus();
    }
    for (const [k, pane] of Object.entries(panes)) pane.hidden = k !== key;
  }

  closeBtn.addEventListener("click", () => setOpen(false, { returnFocus: true }));
  if (scrim) scrim.addEventListener("click", () => setOpen(false, { returnFocus: true }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.open && isOverlay()) setOpen(false, { returnFocus: true });
  });
  if (media) media.addEventListener("change", syncOpenUi);
  // On a narrow screen the panel covers the page, so following a link inside it closes the panel.
  root.addEventListener("click", (e) => {
    const a = e.target instanceof Element ? e.target.closest("a[href^=\"#/\"]") : null;
    if (a && isOverlay()) setOpen(false, { remember: false });
  });

  // ---- [이 화면] ------------------------------------------------------------------------------

  function askButton() {
    const b = h("button", { type: "button", class: "btn help-ask" }, "챗봇에게 묻기");
    b.addEventListener("click", () => { selectTab("chat"); input.focus(); });
    return h("div", { class: "help-ask-row" }, b);
  }

  function ensureScreen() {
    if (S.open && S.page && S.loadedPage !== S.page) loadScreen();
  }

  async function loadScreen() {
    const page = S.page;
    const seq = ++S.screenSeq;
    S.loadedPage = page;
    titleEl.textContent = "";
    fill(screenBody, h("p", { class: "muted", role: "status" }, "불러오는 중…"));
    try {
      const d = await api("GET", `/api/help/${encodeURIComponent(page)}`);
      if (seq !== S.screenSeq) return;
      titleEl.textContent = d.title ? `· ${d.title}` : "";
      fill(screenBody,
        h("div", { class: "md help-md" }, renderMarkdown(String(d.markdown || ""), { routeLinks: true, dropTitle: String(d.title || "") })),
        askButton());
    } catch (e) {
      if (seq !== S.screenSeq) return;
      if (e && e.error === "help_not_found") {
        fill(screenBody, h("p", { class: "muted" }, "이 화면 도움말은 아직 없습니다"), askButton());
        return;
      }
      S.loadedPage = null;                       // allow a retry
      const retry = h("button", { type: "button", class: "btn" }, "다시 시도");
      retry.addEventListener("click", ensureScreen);
      fill(screenBody, h("p", { class: "error", role: "alert" }, (e && e.message) || "도움말을 불러오지 못했습니다"),
        h("div", { class: "btn-row" }, retry), askButton());
    }
  }

  // ---- [챗봇] ---------------------------------------------------------------------------------

  function addMsg(msg) {
    if (msg.role === "user" || msg.role === "assistant") S.conv.push({ role: msg.role, text: msg.text });
    emptyHint.hidden = true;
    let el;
    if (msg.role === "user") {
      el = h("div", { class: "help-msg help-user" }, h("span", { class: "sr-only" }, "내 질문: "), msg.text);
    } else if (msg.role === "assistant") {
      el = h("div", { class: "help-msg help-bot" }, h("span", { class: "sr-only" }, "답: "),
        h("div", { class: "md" }, renderMarkdown(msg.text, { routeLinks: true })));
    } else if (msg.role === "error") {
      el = h("div", { class: "help-msg help-err", role: "alert" }, msg.text);
    } else {
      el = h("div", { class: "help-msg help-note muted small" }, msg.text);
    }
    log.append(el);
    log.scrollTop = log.scrollHeight;
  }

  function setPending(on) {
    S.pending = on;
    sendBtn.disabled = on;
    for (const b of shortcutBtns) b.disabled = on;
    if (!on) { fill(status); return; }
    const cancel = h("button", { type: "button", class: "btn" }, "취소");
    cancel.addEventListener("click", cancelAsk);
    fill(status,
      h("progress", { class: "job-progress", "aria-label": "답 만드는 중" }),
      h("span", {}, "생각하는 중…"), cancel);
  }

  function stopTimer() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
  }

  function cancelJob(id) {
    api("POST", `/api/jobs/${encodeURIComponent(id)}/cancel`).catch(() => { /* already finished */ });
  }

  function cancelAsk() {
    if (!S.pending) return;
    S.epoch += 1;
    stopTimer();
    const id = S.jobId;
    S.jobId = null;
    if (id) cancelJob(id);                      // before the id is known, the POST handler cancels it (epoch mismatch)
    setPending(false);
    addMsg({ role: "note", text: "질문을 취소했습니다" });
    if (!input.value) input.value = S.lastQuestion;
    input.focus();
  }

  function finishError(text) {
    S.jobId = null;
    setPending(false);
    addMsg({ role: "error", text });
  }

  function handleJob(job, epoch) {
    if (epoch !== S.epoch) return;
    if (job.status === "running" || job.status === "awaiting") {
      schedulePoll(epoch);
      return;
    }
    const item = job.items && job.items[0];
    const answer = item && item.state === "done" && item.preview ? item.preview.answer : null;
    if (typeof answer === "string" && answer.trim()) {
      S.jobId = null;
      setPending(false);
      addMsg({ role: "assistant", text: answer });
      return;
    }
    finishError((item && item.message) || job.message || "답을 만들지 못했습니다. 다시 한 번 물어봐 주세요");
  }

  function schedulePoll(epoch) {
    stopTimer();
    S.timer = setTimeout(async () => {
      S.timer = null;
      if (epoch !== S.epoch || !S.jobId) return;
      try {
        const data = await api("GET", `/api/jobs/${encodeURIComponent(S.jobId)}`);
        if (epoch !== S.epoch) return;
        S.pollErrors = 0;
        handleJob(data.job, epoch);
      } catch (e) {
        if (epoch !== S.epoch) return;
        S.pollErrors += 1;
        const gone = e && (e.error === "job_not_found" || e.status === 404);
        if (gone || S.pollErrors >= MAX_POLL_ERRORS) {
          const id = S.jobId;
          S.epoch += 1;
          if (id && !gone) cancelJob(id);
          finishError(gone ? "작업을 찾을 수 없습니다. 서버가 다시 켜졌다면 다시 물어봐 주세요"
            : "진행 상황을 가져오지 못했습니다. 잠시 뒤 다시 물어봐 주세요");
          return;
        }
        schedulePoll(epoch);
      }
    }, pollDelay(S.pollErrors));
  }

  async function ask(raw) {
    const q = String(raw).trim().slice(0, MAX_QUESTION);
    if (!q || S.pending) return;
    const history = trimHistory(S.conv);
    const body = { page: S.page, question: q, history };
    const category = getCategory();
    if (category) body.category = category;
    S.lastQuestion = q;
    input.value = "";
    addMsg({ role: "user", text: q });
    const epoch = ++S.epoch;
    S.pollErrors = 0;
    setPending(true);
    try {
      const res = await api("POST", "/api/help/chat", body);
      if (epoch !== S.epoch) {                  // page changed or cancelled while the request was in flight
        if (res && res.job && res.job.id) cancelJob(res.job.id);
        return;
      }
      S.jobId = res.job.id;
      handleJob(res.job, epoch);
    } catch (e) {
      if (epoch !== S.epoch) return;
      // The question never reached Claude: give it back so the user can resend without retyping.
      if (!input.value) input.value = q;
      // Cancel is cooperative on the server: the previous job stays active until its Claude call ends.
      finishError(e && e.error === "job_running"
        ? "앞 질문을 정리하는 중입니다. 잠시 뒤 다시 보내 주세요"
        : (e && e.message) || "질문을 보내지 못했습니다");
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", (e) => {
    // isComposing / keyCode 229: the Enter that confirms a Korean IME composition must not send.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      ask(input.value);
    }
  });

  function resetChat() {
    S.epoch += 1;
    stopTimer();
    const id = S.jobId;
    S.jobId = null;
    if (id) cancelJob(id);
    S.conv = [];
    S.lastQuestion = "";
    setPending(false);
    fill(log);
    emptyHint.hidden = false;
    input.value = "";
  }

  // ---- public ----------------------------------------------------------------------------------

  function setPage(page) {
    if (!page || page === S.page) return;
    S.page = page;
    resetChat();
    ensureScreen();
  }

  // The answer is grounded in the category's situation, so a category switch starts a fresh chat (like setPage).
  function onCategory(name) {
    const next = name || null;
    if (next === S.category) return;
    S.category = next;
    resetChat();
  }

  function toggle(open) {
    const next = typeof open === "boolean" ? open : !S.open;
    setOpen(next, { returnFocus: !next && isOverlay() });
  }

  selectTab("screen");
  setPending(false);
  S.open = readOpen() && !isOverlay();            // a remembered "open" does not cover the page on a narrow screen
  syncOpenUi();

  return { setPage, onCategory, toggle };
}
