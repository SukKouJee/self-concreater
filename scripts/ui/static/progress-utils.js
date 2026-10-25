// 레퍼런스 추가 화면의 시간 계산. 순수 함수만 두고 import는 쓰지 않는다(node 테스트용).

const FRACTION_MIN = 0.1; // 이 미만이면 진행률로 남은 시간을 추정하지 않는다

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function fmtDur(sec) {
  const n = num(sec);
  const s = n === null ? 0 : Math.max(0, Math.round(n));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return hh > 0 ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

export function fmtClock(epochSec, tzOffsetMin) {
  const off = tzOffsetMin === undefined ? -new Date().getTimezoneOffset() : tzOffsetMin;
  const mins = Math.floor((Number(epochSec) || 0) / 60) + off;
  const day = ((mins % 1440) + 1440) % 1440;
  return `${String(Math.floor(day / 60)).padStart(2, "0")}:${String(day % 60).padStart(2, "0")}`;
}

// -> { elapsed, expected, fraction, over, remaining }  (fraction: 막대값 0..1, 계산할 수 없으면 null)
export function rowView(row, nowSec) {
  const expected = num(row && row.expected);
  const state = row && row.state;
  if (state === "running") {
    const startedAt = num(row.started_at);
    const end = num(row.paused_at) !== null ? row.paused_at : nowSec;
    const elapsed = startedAt === null ? 0 : Math.max(0, end - startedAt);
    const f = num(row.fraction);
    const measured = f !== null;
    const fraction = measured ? Math.min(1, Math.max(0, f)) : (expected ? Math.min(1, elapsed / expected) : null);
    const over = expected !== null && elapsed > expected;
    let remaining = null;
    if (over) remaining = 0;
    else if (measured && f >= FRACTION_MIN) remaining = Math.max(0, (elapsed * (1 - f)) / f);
    else if (expected !== null) remaining = Math.max(0, expected - elapsed);
    return { elapsed, expected, fraction, over, remaining };
  }
  if (state === "done" || state === "failed") {
    const elapsed = num(row.elapsed) !== null ? Math.max(0, row.elapsed) : null;
    return { elapsed, expected, fraction: state === "done" ? 1 : null, over: false, remaining: 0 };
  }
  if (state === "skipped") return { elapsed: null, expected, fraction: null, over: false, remaining: 0 };
  // pending
  return { elapsed: 0, expected, fraction: 0, over: false, remaining: expected };
}

// 항목 전체 남은 시간(초). 하나라도 모르면 null.
export function itemRemaining(timing, nowSec) {
  const steps = timing && timing.steps;
  if (!steps) return null;
  let total = 0;
  for (const row of Object.values(steps)) {
    if (row.state !== "running" && row.state !== "pending") continue;
    const r = rowView(row, nowSec).remaining;
    if (r === null) return null;
    total += r;
  }
  return total;
}

// 남은 시간 말투. fine=true면 10초 단위까지("약 5분 30초"), 아니면 분 단위("약 3분").
export function fmtRemain(sec, fine) {
  const n = num(sec);
  const s = n === null ? 0 : Math.max(0, Math.round(n));
  if (s < 60) return `약 ${s}초`;
  if (fine) {
    let m = Math.floor(s / 60);
    let r = Math.round((s % 60) / 10) * 10;
    if (r === 60) { m += 1; r = 0; }
    return r ? `약 ${m}분 ${r}초` : `약 ${m}분`;
  }
  const m = Math.round(s / 60);
  return m >= 60 ? `약 ${Math.floor(m / 60)}시간${m % 60 ? ` ${m % 60}분` : ""}` : `약 ${m}분`;
}

// 스크린 리더용 길이("5분 2초").
export function spokenDur(sec) {
  const n = num(sec);
  const s = n === null ? 0 : Math.max(0, Math.round(n));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return [hh ? `${hh}시간` : "", mm ? `${mm}분` : "", ss || (!hh && !mm) ? `${ss}초` : ""].filter(Boolean).join(" ");
}

// 막대 값은 줄어들지 않는다: 이전 최대값과 현재 값 중 큰 쪽(둘 다 null이면 null).
export function monotonicFraction(prevMax, current) {
  const a = num(prevMax);
  const b = num(current);
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}
