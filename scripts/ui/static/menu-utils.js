// Pure helpers and definitions for the shell menus (no DOM) so node tests can load them. It only imports the
// pure video-utils.js (relative path: node loads it from disk, the browser from /static).
import { parseVideoHash, videoHref, videoId } from "./video-utils.js";

// Desktop sidebar (spec 2절): 영감 / 내 영상 / 성장. `count` names the number shown beside an item (app.js fills
// it from existing APIs); the 내 영상 group lists the category's unfinished videos above its items.
export const SIDEBAR = [
  {
    group: "영감",
    items: [
      { label: "레퍼런스", href: "#/refs", count: "ref" },
      { label: "유형", href: "#/typify", count: "archetypes" },
      { label: "모티브", href: "#/motif", count: "motifs" },
    ],
  },
  // "+ 새 영상 시작" starts from a motif card (spec 4.3). `action`: a start link, never marked current (the 모티브 item
  // above is the current one on #/motif).
  { group: "내 영상", videos: true, items: [{ label: "+ 새 영상 시작", href: "#/motif", action: true }] },
  {
    group: "성장",
    items: [
      { label: "오늘 미션", href: "#/mission" },
      { label: "주간 리포트", href: "#/report" },
    ],
  },
];

// Phone (<= 640px) bottom tabs. `icon` is a key of app.js ICONS.
export const PHONE_TABS = [
  { key: "home", label: "홈", href: "#/home", icon: "home" },
  { key: "inspire", label: "영감", href: "#/refs", icon: "spark" },
  { key: "videos", label: "내 영상", href: "#/videos", icon: "film" },
  { key: "mission", label: "미션", href: "#/mission", icon: "target" },
];

// Menus whose detail screens live under "<route>/..." (e.g. #/refs/<id>) stay current on those screens.
const PREFIX_ROUTES = ["#/refs", "#/conti", "#/draft", "#/draft/new", "#/diagnose", "#/report", "#/guide", "#/video"];

// Routes that open another menu item's screen (#/add is the add area of the 레퍼런스 screen).
const ALIASES = { "#/refs": ["#/add"] };

export function isActive(href, hash) {
  const current = hash || "#/home";
  return current === href || (ALIASES[href] || []).includes(current)
    || (PREFIX_ROUTES.includes(href) && current.startsWith(href + "/"));
}

// First hash segment ("#/conti/x" -> "conti", "#/refs?tab=library" -> "refs"); "" for no hash.
function head(hash) {
  return (hash || "").split("?")[0].replace(/^#\/?/, "").split("/")[0];
}

const TAB_OF = {
  "": "home", home: "home",
  refs: "inspire", add: "inspire", library: "inspire", typify: "inspire", motif: "inspire",
  draft: "videos", conti: "videos", diagnose: "videos", videos: "videos", video: "videos",
  mission: "mission", report: "mission",
  guide: null,
};

// Bottom tab lit for a route. Unknown routes open the home screen (app.js router), so they light 홈.
export function phoneTab(hash) {
  const key = head(hash);
  return key in TAB_OF ? TAB_OF[key] : "home";
}

// Second-level links shown under the header. 레퍼런스/공용 라이브러리 are the tabs of the 레퍼런스 screen (spec 2:
// the library is a tab inside 레퍼런스), so desktop shows them only there; phones (no sidebar) show the whole group.
const SUBNAV = {
  inspire: [
    { label: "레퍼런스", href: "#/refs", phoneOnly: false, refsTab: true },
    { label: "공용 라이브러리", href: "#/refs?tab=library", phoneOnly: false, refsTab: true },
    { label: "유형", href: "#/typify", phoneOnly: true },
    { label: "모티브", href: "#/motif", phoneOnly: true },
  ],
  mission: [
    { label: "오늘 미션", href: "#/mission", phoneOnly: true },
    { label: "주간 리포트", href: "#/report", phoneOnly: true },
  ],
};

export function subnavItems(hash) {
  const items = SUBNAV[phoneTab(hash)] || [];
  const onRefs = ["refs", "library"].includes(head(hash));
  return items.map(({ refsTab, ...it }) => ({
    ...it, phoneOnly: it.phoneOnly || (!!refsTab && !onRefs), current: isActive(it.href, hash),
  }));
}

// Where a sidebar video row goes: the video workspace of that draft (draft path "drafts/x.md").
export function videoLinks(path) {
  return { view: videoHref(videoId(path)) };
}

// The row is current on every screen of its workspace (any tab, the shoot mode).
export function isVideoActive(path, hash) {
  const v = parseVideoHash(hash);
  return !!v && v.mode !== "list" && v.id === videoId(path);
}

// Short stage names for the sidebar (status.py VIDEO_STEPS keys; 촬영함 -> 촬영, 자막 영상 -> 편집).
const STAGE = { draft: "기획안", polish: "다듬기", conti: "콘티", filmed: "촬영", captions: "편집", diagnose: "진단" };

// Progress of one /api/status video: share of required steps done (optional steps do not count) and the name of
// its next step ("완료" when none is left).
export function videoProgress(video) {
  const required = (video.steps || []).filter((s) => !s.optional);
  const done = required.filter((s) => s.done).length;
  return {
    pct: required.length ? Math.round((done / required.length) * 100) : 0,
    stage: video.next ? (STAGE[video.next] || video.next) : "완료",
  };
}

// Sidebar video rows: unfinished videos (status order: most progress first) up to `limit`, plus the total count
// for the "전체 N편" link.
export function sidebarVideos(videos, limit) {
  const list = Array.isArray(videos) ? videos : [];
  return { shown: list.filter((v) => v.next !== null && v.next !== undefined).slice(0, limit), total: list.length };
}
