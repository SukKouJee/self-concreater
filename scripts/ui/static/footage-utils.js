// 촬영 영상 영역의 순수 함수. import는 쓰지 않는다(node 테스트용).

// 판정 kind(서버 footage_match) → 화면 글자. 배지는 색만이 아니라 이 글자를 함께 보인다.
export const KIND_TEXT = {
  auto: "찍음",
  pinned: "찍음",
  review: "확인 필요",
  missing: "남은 컷",
  not_filmed: "못 찍음",
};

const SCORE_TEXT = { high: "확신 높음", medium: "확신 중간", low: "확신 낮음" };
const PROGRESS_MIN = 0.05; // 이 미만이면 남은 시간을 추정하지 않는다

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// 판정 묶음: filmed(찍음) | review(확인 필요) | remaining(남은 컷 + 못 찍음).
export function kindGroup(kind) {
  if (kind === "auto" || kind === "pinned") return "filmed";
  if (kind === "review") return "review";
  return "remaining";
}

// 배지 글자. 사용자가 정한 판정은 "· 직접"을 붙인다.
export function verdictLabel(cut) {
  const text = KIND_TEXT[cut && cut.kind] || KIND_TEXT.missing;
  return cut && cut.user ? `${text} · 직접` : text;
}

export function summaryLine(state) {
  const s = state && state.summary;
  if (!s) return "";
  return `찍음 ${s.filmed || 0} · 확인 필요 ${s.review || 0} · 남은 컷 ${s.missing || 0}`;
}

// f: "all" | "remaining" | "review" (모르는 값은 all).
export function filterCuts(cuts, f) {
  const list = Array.isArray(cuts) ? cuts : [];
  if (f === "remaining" || f === "review") return list.filter((c) => kindGroup(c.kind) === f);
  return list.slice();
}

function clock(total) {
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = String(total % 60).padStart(2, "0");
  return hh > 0 ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

export function fmtTime(sec) {
  const n = num(sec);
  return clock(n === null ? 0 : Math.max(0, Math.round(n)));
}

// 시작은 내림, 끝은 올림: 보이는 구간이 실제 구간을 다 덮는다.
export function fmtRange(start, end) {
  const a = Math.max(0, Math.floor(num(start) || 0));
  const b = Math.max(a, Math.ceil(num(end) || 0));
  return `${clock(a)}–${clock(b)}`;
}

export function scoreText(score) {
  return SCORE_TEXT[score] || "";
}

// 원본 재생 주소(파일 지문 id). #t=시작,끝으로 그 구간만 재생한다.
export function mediaUrl(fid, contiPath, start, end) {
  return `/api/footage/media/${encodeURIComponent(fid)}?path=${encodeURIComponent(contiPath)}#t=${num(start) || 0},${num(end) || 0}`;
}

export function frameUrl(fid, rel) {
  return `/api/footage/frame/${encodeURIComponent(fid)}/${String(rel).split("/").map(encodeURIComponent).join("/")}`;
}

export function folderLine(folder) {
  if (folder.missing) return "폴더를 찾을 수 없어요 — 드라이브가 빠졌거나 옮겨졌어요";
  if (!folder.files) return "영상 파일 없음";
  const parts = [`영상 ${folder.files}개`];
  if (folder.processed) parts.push(`처리함 ${folder.processed}개(${fmtTime(folder.duration_sec)})`);
  if (folder.pending) parts.push(`새 파일 ${folder.pending}개`);
  if (folder.unreadable) parts.push(`읽을 수 없음 ${folder.unreadable}개`);
  return parts.join(" · ");
}

const TERMINAL = new Set(["done", "failed", "skipped", "saved"]);

// 처리 작업 진행. 항목은 파일마다 하나 + 마지막 "match"(맞추기).
// 캐시로 건너뛴 파일(done인데 시작한 적 없음)은 이번 일의 몫에서 뺀다.
// -> { files, done, current, matching, fraction, remaining(초|null) }
export function jobProgress(job, nowSec) {
  if (!job || !Array.isArray(job.items)) return null;
  const files = job.items.filter((i) => i.id !== "match");
  const match = job.items.find((i) => i.id === "match");
  const work = files.filter((i) => !(i.state === "done" && num(i.started_at) === null));
  let sum = 0;
  for (const i of work) {
    if (TERMINAL.has(i.state)) sum += 1;
    else if (i.state === "running") sum += Math.min(1, Math.max(0, num(i.fraction) || 0));
  }
  const fraction = work.length ? sum / work.length : 1;
  const matching = !!match && match.state === "running";
  const starts = work.map((i) => num(i.started_at)).filter((v) => v !== null);
  let remaining = null;
  if (!matching && starts.length && fraction >= PROGRESS_MIN && fraction < 1) {
    const elapsed = Math.max(0, (num(nowSec) || 0) - Math.min(...starts));
    remaining = (elapsed * (1 - fraction)) / fraction;
  }
  return {
    files: files.length,
    done: files.filter((i) => TERMINAL.has(i.state)).length,
    current: files.find((i) => i.state === "running") || null,
    matching,
    fraction,
    remaining,
  };
}

// 컷 파일 결과 한 줄의 덧말.
export function clipNote(item) {
  const msg = (item && item.message) || "";
  if (item && item.state === "skipped") return msg ? `건너뜀: ${msg}` : "건너뜀";
  if (item && item.state === "failed") return msg ? `실패: ${msg}` : "실패";
  return msg;
}

// 새 컷 후보를 넣을 자리 기본값: 같은 파일에서 그 장면보다 앞에 고른 구간이 있는 컷 중 가장 늦은 것, 없으면 마지막 컷.
export function suggestAfter(cuts, s) {
  const list = Array.isArray(cuts) ? cuts : [];
  if (!list.length) return null;
  let best = null;
  for (const c of list) {
    const ch = c.choice;
    if (!ch || ch.fid !== s.fid || !(num(ch.start) <= num(s.start))) continue;
    if (best === null || ch.start >= best.choice.start) best = c;
  }
  return (best || list[list.length - 1]).id;
}
