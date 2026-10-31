// Edition-aware copy: the local edition (`ytc ui` on the user's PC) and the hosted server edition share these
// screens, but some sentences only make sense on a PC (작업 폴더, 터미널, "Claude에게 말하세요", 서버 끄기 ...).
// Every such sentence lives here as { local, server }. `local` is the exact text the local edition always showed
// (tests/test_copy_js.py pins it); `server` is the hosted wording, or null when the server edition shows nothing.
// The edition comes from GET /api/features once at start (app.js -> setEdition). Until then it is "local".
// Pure module: no DOM (node tests load this file).

let current = "local";

export function setEdition(name) {
  current = name === "server" ? "server" : "local";
}

export function edition() {
  return current;
}

export function isServer() {
  return current === "server";
}

const TRASH_SERVER = "지운 것은 화면에서 되살릴 수 없어요. 지우기 전에 한 번 더 확인해 주세요.";
const PROFILE_SERVER = "프로필을 만들려면 관리자에게 문의하세요.";

export const COPY = {
  // home: no category yet / first visit (no work space yet)
  "home.noCategory": {
    local: "Claude에게 “처음 설정해줘”라고 말하면 첫 카테고리를 함께 만들어요.",
    server: "이 서버에서는 카테고리를 화면에서 만들 수 없어요. 관리자에게 카테고리를 만들어 달라고 요청하세요.",
  },
  "home.init.title": { local: "아직 작업 폴더가 아닙니다", server: "아직 내 작업 공간이 없습니다" },
  "home.init.lead": {
    local: "지금 연 폴더를 작업 폴더로 만들면 레퍼런스와 기획안이 여기에 저장돼요.",
    server: "작업 공간을 만들면 레퍼런스와 기획안이 여기에 저장돼요.",
  },
  "home.init.button": { local: "이 폴더를 yt-coach 작업 폴더로 만들기", server: "내 작업 공간 만들기" },
  "home.init.done": { local: "작업 폴더를 만들었습니다", server: "작업 공간을 만들었습니다" },
  // no category (sidebar, report, and the pages that need one)
  "menu.noCategory": { local: "카테고리를 먼저 만들어요", server: "카테고리가 아직 없어요" },
  "report.noCategory": { local: "카테고리를 먼저 만들어 주세요.", server: "카테고리가 아직 없습니다. 관리자에게 문의하세요." },
  "page.noCategory": {
    local: "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요.",
    server: "카테고리가 없습니다. 카테고리를 만들려면 관리자에게 문의하세요.",
  },
  // add (레퍼런스 넣기)
  "add.unsaved": { local: "저장하지 않은 결과는 UI 서버를 끄면 사라져요", server: "저장하지 않은 결과는 서버가 다시 시작되면 사라져요" },
  "add.noCategory.after": {
    local: "에서 작업 폴더와 카테고리를 만들어 주세요.",
    server: "에서 카테고리를 확인해 주세요. 카테고리가 없으면 관리자에게 문의하세요.",
  },
  // refs
  "refs.noCategory": {
    local: "카테고리가 아직 없습니다. 홈에서 작업 폴더와 카테고리를 먼저 만드세요.",
    server: "카테고리가 아직 없습니다. 이 서버에서는 카테고리를 화면에서 만들 수 없으니 관리자에게 문의하세요.",
  },
  "refs.trashHint": {
    local: "기법 모음에서도 이 영상 출처를 뺍니다. 다른 카테고리에 같은 영상이 있으면 분석 데이터는 그대로 둡니다. 잘못 지웠으면 Claude에게 “휴지통에서 되살려줘”라고 말하세요.",
    server: `기법 모음에서도 이 영상 출처를 뺍니다. 다른 카테고리에 같은 영상이 있으면 분석 데이터는 그대로 둡니다. ${TRASH_SERVER}`,
  },
  // draft / conti trash confirm
  "trash.restoreHint": { local: "잘못 지웠으면 Claude에게 “휴지통에서 되살려줘”라고 말하세요.", server: TRASH_SERVER },
  // conti editor: the conti changed somewhere else
  "conti.changedElsewhere": {
    local: "다른 곳(편집기·터미널)에서 이 콘티 파일이 바뀌어 저장하지 않았습니다. 다시 불러온 뒤 고쳐 주세요. 입력해 둔 내용은 남아 있습니다.",
    server: "다른 창이나 기기에서 이 콘티가 바뀌어 저장하지 않았습니다. 다시 불러온 뒤 고쳐 주세요. 입력해 둔 내용은 남아 있습니다.",
  },
  // the caption export box summary: the moving-caption video is off on the server edition
  "conti.exportSummary": { local: " CapCut용 SRT · 움직이는 자막 영상", server: " CapCut용 SRT" },
  "conti.changedButton": { local: "파일이 밖에서 바뀌었습니다 — 다시 불러오기", server: "다른 곳에서 바뀌었어요 — 다시 불러오기" },
  "conti.changedToast": {
    local: "파일이 밖에서 바뀌었습니다. 먼저 다시 불러오세요",
    server: "다른 곳에서 콘티가 바뀌었어요. 먼저 다시 불러와 주세요",
  },
  "conti.artBlocked": {
    local: "Claude를 쓸 수 없어 장면 그림을 다시 그리지 못했어요",
    server: "AI를 지금 쓸 수 없어 장면 그림을 다시 그리지 못했어요. 계속되면 관리자에게 문의하세요",
  },
  // profile missing (mission, motif, report)
  "mission.profileNote": {
    local: "프로필을 만들면 미션이 맞춤으로 나옵니다. Claude에게 “처음 설정해줘”라고 말하면 함께 만들어요",
    server: `프로필이 있으면 미션이 맞춤으로 나와요. ${PROFILE_SERVER}`,
  },
  "motif.profileNote": {
    local: "프로필이 없어 카드가 일반적으로 나옵니다 — Claude에게 “처음 설정해줘”라고 말하면 만들 수 있어요",
    server: `프로필이 없어 카드가 일반적으로 나와요. ${PROFILE_SERVER}`,
  },
  "report.noProfile": {
    local: "프로필이 없어 제안을 반영할 수 없습니다. Claude에게 “처음 설정해줘”라고 말하면 프로필을 먼저 만들어요",
    server: `프로필이 없어 제안을 반영할 수 없어요. ${PROFILE_SERVER}`,
  },
  // motif: the technique catalog cleanup only exists as a chat request to Claude Code
  "motif.catalogHint": {
    local: "기법 모음 정리(같은 기법 합치기)는 Claude에게 “기법 모음 정리해줘”라고 말하면 돼요",
    server: null,
  },
  // help chatbot empty state
  "help.chatEmpty": {
    local: "이 화면에 대해 궁금한 점을 물어보세요. 답은 도움말과 지금 작업 폴더 상태를 바탕으로 만들어집니다.",
    server: "이 화면에 대해 궁금한 점을 물어보세요. 답은 도움말과 지금 내 작업 상태를 바탕으로 만들어집니다.",
  },
};

// The text for `key` in the current edition (null: the server edition shows nothing there).
export function t(key) {
  const entry = COPY[key];
  if (!entry) throw new Error(`unknown copy key: ${key}`);
  return current === "server" ? entry.server : entry.local;
}
