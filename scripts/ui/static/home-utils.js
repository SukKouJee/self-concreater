// Pure helpers for the home screen (no DOM) so node tests can load them directly. It only imports the pure
// video-utils.js (relative path).
import { stages, videoHref, videoId } from "./video-utils.js";

// Per-video steps (GET /api/status videos[].steps keys, in order).
export const STEP_NAMES = {
  draft: "기획안", polish: "다듬기", conti: "콘티", filmed: "촬영함", captions: "자막 영상", diagnose: "진단",
};

// Rows shown before the "N개 더 보기" button.
export const VISIBLE_VIDEOS = 3;

// True when `path` is one of the rows folded behind "N개 더 보기".
export function isFolded(videos, path) {
  return videos.slice(VISIBLE_VIDEOS).some((v) => v.path === path);
}

// Action for a video's next step: a link, the in-place filmed toggle, or nothing (video complete).
export function nextButton(step, path) {
  if (step === "conti") return { text: "콘티 만들기", href: videoHref(videoId(path), "conti") };
  if (step === "filmed") return { text: "촬영함 체크", action: "film" };
  if (step === "diagnose") return { text: "진단하기", href: videoHref(videoId(path), "diagnose") };
  return null;
}

// Dot state of one video step. Done wins over optional; only the video's next (required) step is "next".
export function stepState(step, nextKey) {
  if (step.done) return "done";
  if (step.key === nextKey) return "next";
  return step.optional ? "optional" : "todo";
}

const STEP_SR = { done: "완료", next: "다음 단계", todo: "아직", optional: "선택, 아직" };
export function stepSr(state) {
  return STEP_SR[state] || "";
}

// "10월 25일 · 15분 목표" from the draft date (YYYY-MM-DD) and target length; missing parts are left out.
export function videoMeta(v) {
  const parts = [];
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v.date || "");
  if (m) parts.push(`${Number(m[2])}월 ${Number(m[3])}일`);
  const target = targetText(v.target_sec);
  if (target) parts.push(target);
  return parts.join(" · ");
}

// Target length: whole seconds under a minute ("15초 목표"), whole minutes from 60 up (90 -> "2분 목표").
export function targetText(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) return "";
  const s = Math.max(1, Math.round(sec));
  return s < 60 ? `${s}초 목표` : `${Math.round(s / 60)}분 목표`;
}

// Recent-output label without file names: the video title for a draft or its conti page (titles: draft path ->
// title), "<category> 리포트 · M월 D일" for a report, else the file name without date prefix, extension or dashes.
export function recentName(item, titles) {
  const path = String(item.path || "");
  const draft = path.replace(/-conti\.html$/i, ".md");
  if (titles && titles[draft]) return titles[draft];
  const file = path.split("/").pop().replace(/\.(md|html)$/i, "").replace(/-conti$/i, "");
  const rep = /^(.+)-(\d{4})-(\d{2})-(\d{2})$/.exec(file);
  if (item.kind === "report" && rep) return `${rep[1]} 리포트 · ${Number(rep[3])}월 ${Number(rep[4])}일`;
  return file.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/-/g, " ");
}

// Home sections in screen order (spec 4.1). Phone: the dark next card, today's mission, then three videos.
export const HOME_ORDER = {
  desktop: ["next", "videos", "mission", "recent"],
  phone: ["next", "mission", "videos"],
};

const NEXT_TITLE = { conti: "콘티 만들 차례예요", filmed: "촬영할 차례예요", diagnose: "진단할 차례예요" };
const NEXT_WHY = {
  conti: "기획안을 촬영용 콘티로 바꿔요",
  filmed: "촬영 모드로 장면을 하나씩 찍어요",
  diagnose: "찍은 영상을 아키타입과 비교해 고칠 점을 찾아요",
};
// Category steps (status.py _STEPS menus). A new video starts from a motif card; 콘티·진단 live in each video.
const CATEGORY_ACTION = {
  add: { label: "레퍼런스 모으기", href: "#/add" },
  typify: { label: "유형 나누기", href: "#/typify" },
  draft: { label: "새 영상 시작", href: "#/motif" },
  conti: { label: "내 영상 보기", href: "#/videos" },
  diagnose: { label: "내 영상 보기", href: "#/videos" },
};
const NEW_VIDEO = { label: "새 영상 시작", href: "#/motif" };

function videoAction(step, path) {
  const id = videoId(path);
  if (step === "filmed") return { label: "촬영 모드 시작", href: `${videoHref(id)}/shoot` };
  if (step === "conti") return { label: "콘티 만들기", href: videoHref(id, "conti") };
  if (step === "diagnose") return { label: "진단하기", href: videoHref(id, "diagnose") };
  return { label: "영상 열기", href: videoHref(id) };
}

// The "다음 할 일" card from /api/status next_action (desktop and phone): { title, why, action: {label, href},
// film (filming is next: the phone card shows the shoot count), path (the video's draft path or null) }.
export function nextStep(cat) {
  const c = cat || {};
  const videos = Array.isArray(c.videos) ? c.videos : [];
  const na = c.next_action || { kind: "new_draft" };
  if (na.kind === "video" && na.path) {
    const video = videos.find((v) => v.path === na.path);
    const title = na.title || (video && video.title) || videoId(na.path);
    return {
      title: `${title}, ${NEXT_TITLE[na.step] || "이어서 할 차례예요"}`, why: NEXT_WHY[na.step] || "",
      action: videoAction(na.step, na.path), film: na.step === "filmed", path: na.path,
    };
  }
  if (na.kind === "category") {
    const allDone = videos.length > 0;
    return {
      title: na.text || "카테고리 단계를 이어가요",
      why: allDone ? "만들고 있는 영상은 모두 마쳤어요. 카테고리 단계를 이어가요" : "카테고리 단계를 이어가요",
      action: CATEGORY_ACTION[na.menu] || NEW_VIDEO, film: false, path: null,
    };
  }
  return { title: "다음 영상을 시작할 차례예요", why: "모티브 카드를 골라 새 영상을 시작해요", action: NEW_VIDEO, film: false, path: null };
}

// Scenes filmed of a conti: the ids checked in this browser (shoot-utils shootStore) that are still in the conti;
// a draft marked 다 찍음 counts every scene.
export function shootProgress(cuts, ids, filmed) {
  const list = Array.isArray(cuts) ? cuts : [];
  const done = filmed ? list.length : list.filter((c) => c && ids && ids.has(c.id)).length;
  return { done, total: list.length };
}

// Six stage dots of a /api/status video (video-utils stages): done | now | skip | todo.
export function stageDots(video) {
  return stages(null, { steps: (video && video.steps) || [] }).map((s) => s.state);
}

const STAGE_SHORT = { draft: "기획안", polish: "다듬기", conti: "콘티", filmed: "촬영", captions: "편집", diagnose: "진단" };
// "촬영 차례" (the video's next step) or "완료".
export function stageWord(video) {
  const next = video && video.next;
  return next ? `${STAGE_SHORT[next] || next} 차례` : "완료";
}

// Phone list: up to `limit` unfinished videos (status order) and the total for "전체 N편".
export function phoneVideos(videos, limit = 3) {
  const list = Array.isArray(videos) ? videos : [];
  return { shown: list.filter((v) => v && v.next).slice(0, limit), total: list.length };
}

const MISSION_STATUS = { open: "진행 중", submitted: "완료", skipped: "건너뜀" };
// Today's mission card: `today` from GET /api/mission (null = none yet; undefined = that call failed), `card`
// from /api/status mission (exists / status only). -> { title, text, meta, action (button label) }.
export function missionView(today, card) {
  const t = today || null;
  const exists = t ? true : today === null ? false : !!(card && card.exists);
  if (!exists) {
    return { title: "아직 받지 않았어요", text: "받으면 지금 단계에 맞춘 15~30분 미션이 나와요", meta: "", action: "오늘 미션 받기" };
  }
  const status = t ? t.status : card && card.status;
  const meta = [MISSION_STATUS[status] || "", t && t.minutes ? `${t.minutes}분` : ""].filter(Boolean).join(" · ");
  return { title: (t && t.title) || "오늘 미션", text: "", meta, action: "미션 보기" };
}

const RECENT_KIND = { draft: "기획안", conti: "콘티", report: "리포트" };
function monthDay(date) {
  return `${date.getMonth() + 1}월 ${date.getDate()}일`;
}

// One recent output: "영상 제목 · 콘티 · 10월 29일" (no file names) and where it opens: a draft or its conti page
// -> that video's tab, a report -> the report screen.
export function recentLine(item, titles) {
  const path = String(item.path || "");
  const kind = RECENT_KIND[item.kind] || item.kind || "";
  if (item.kind === "report") {
    const m = /([^/]+)-(\d{4})-(\d{2})-(\d{2})\.html$/i.exec(path);
    const when = m ? `${Number(m[3])}월 ${Number(m[4])}일` : monthDay(new Date(item.mtime * 1000));
    return { text: [m ? m[1] : recentName(item, titles), kind, when].join(" · "), href: "#/report" };
  }
  const draft = path.replace(/-conti\.html$/i, ".md");
  const name = recentName(item, titles);
  const href = videoHref(videoId(draft), item.kind === "conti" ? "conti" : "plan");
  return { text: [name, kind, monthDay(new Date(item.mtime * 1000))].join(" · "), href };
}

// The conti file of a draft (status.py: "<stem>-conti.md" beside the draft), for GET /api/conti?path=.
export function contiOf(draft) {
  return String(draft || "").replace(/\.md$/i, "-conti.md");
}
