// 레퍼런스 추가 화면의 시간 계산. 순수 함수만 두고 import는 쓰지 않는다(node 테스트용).

const FRACTION_MIN = 0.1; // 이 미만이면 진행률로 남은 시간을 추정하지 않는다
// 단계의 마지막 가중 구간(progress.py PHASE_WEIGHTS와 맞춘다). 이 구간에서는 그 구간의 시계와 진행률로
// 남은 시간을 잰다: 앞 구간 가중치의 오차가 남은 시간에 섞이지 않는다.
const LAST_PHASES = ["camera"];

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

// 마지막 가중 구간이면 그 구간 기준 남은 시간, 아니면 null.
function phaseRemaining(row, end) {
  if (!LAST_PHASES.includes(row.phase)) return null;
  const pf = num(row.phase_fraction);
  const ps = num(row.phase_started_at);
  if (pf === null || ps === null || pf < FRACTION_MIN) return null;
  const p = Math.min(1, pf);
  return Math.max(0, ((Math.max(0, end - ps)) * (1 - p)) / p);
}

// -> { elapsed, expected, fraction, over, remaining, byFraction }
//    fraction: 막대값 0..1(계산할 수 없으면 null). byFraction: 남은 시간을 측정 진행률로 셌는지.
//    예상을 넘겨도 측정 진행률이 있으면 그 값으로 남은 시간을 센다. 측정 불가 구간만 넘으면 0.
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
    const byPhase = measured ? phaseRemaining(row, end) : null;
    const byFraction = byPhase !== null || (measured && f >= FRACTION_MIN);
    let remaining = null;
    if (byPhase !== null) remaining = byPhase;
    else if (byFraction) remaining = Math.max(0, (elapsed * (1 - f)) / f);
    else if (over) remaining = 0;
    else if (expected !== null) remaining = Math.max(0, expected - elapsed);
    return { elapsed, expected, fraction, over, remaining, byFraction };
  }
  if (state === "done" || state === "failed") {
    // 같은 줄의 앞 회차(자막 받기 -> 음성 인식)를 합쳐 보인다. 기록은 서버에서 따로 남는다.
    const prior = Math.max(0, num(row.prior_elapsed) || 0);
    const priorExp = Math.max(0, num(row.prior_expected) || 0);
    const elapsed = num(row.elapsed) !== null ? Math.max(0, row.elapsed) + prior : null;
    const exp = expected !== null ? expected + priorExp : null;
    return { elapsed, expected: exp, fraction: state === "done" ? 1 : null, over: false, remaining: 0, byFraction: false };
  }
  if (state === "skipped") return { elapsed: null, expected, fraction: null, over: false, remaining: 0, byFraction: false };
  // pending
  return { elapsed: 0, expected, fraction: 0, over: false, remaining: expected, byFraction: false };
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

// 영상 전체 진행(시간 기준): 걸린 시간 / (걸린 시간 + 남은 시간). 남은 시간을 모르면 fraction은 null.
// overdue: 측정 진행률 없이 예상을 넘긴 단계가 진행 중(그 단계의 남은 시간을 0으로 셌으니 숫자를 믿으면 안 된다).
// -> { elapsed, remaining, total, fraction, overdue } (timing이 없으면 null)
export function itemProgress(timing, nowSec) {
  const steps = timing && timing.steps;
  if (!steps) return null;
  let elapsed = 0;
  let overdue = false;
  for (const row of Object.values(steps)) {
    if (row.state === "done" || row.state === "failed" || row.state === "running") {
      const v = rowView(row, nowSec);
      elapsed += num(v.elapsed) || 0;
      if (row.state === "running" && v.over && !v.byFraction) overdue = true;
    }
  }
  const remaining = itemRemaining(timing, nowSec);
  if (remaining === null) return { elapsed, remaining: null, total: null, fraction: null, overdue };
  const total = elapsed + remaining;
  return { elapsed, remaining, total, fraction: total > 0 ? Math.min(1, elapsed / total) : null, overdue };
}

const STEP_FLOOR = 0.14; // 단계 칸의 최소 몫: 이름과 시간이 들어갈 만큼

// 단계 칸 폭(합 1): 단계별 시간(끝난 단계는 걸린 시간, 진행 중은 걸린+남은, 대기는 예상)에 비례하고
// 모든 칸이 STEP_FLOOR 이상이다. 시간을 하나라도 모르면 똑같이 나눈다.
export function stepWeights(timing, nowSec) {
  const steps = (timing && timing.steps) || {};
  const keys = Object.keys(steps);
  if (keys.length === 0) return {};
  const equal = Object.fromEntries(keys.map((k) => [k, 1 / keys.length]));
  const times = {};
  for (const k of keys) {
    const row = steps[k];
    const v = rowView(row, nowSec);
    let t;
    if (row.state === "skipped") t = 0;
    else if (row.state === "running") t = v.remaining === null ? num(v.expected) : (num(v.elapsed) || 0) + v.remaining;
    else if (row.state === "done" || row.state === "failed") t = num(v.elapsed);
    else t = num(v.expected);
    if (t === null) return equal;
    times[k] = Math.max(0, t);
  }
  const sum = Object.values(times).reduce((a, b) => a + b, 0);
  if (sum <= 0 || keys.length * STEP_FLOOR >= 1) return equal;
  const spread = 1 - keys.length * STEP_FLOOR;
  return Object.fromEntries(keys.map((k) => [k, STEP_FLOOR + spread * (times[k] / sum)]));
}
