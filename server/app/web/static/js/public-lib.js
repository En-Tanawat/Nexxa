// Pure helpers for the public dashboard and trends pages: formatting, status/trend wording, chart data models.
// No DOM access here, so every function is unit-tested in Node (tests/js/public_lib.test.mjs).
const LOCALE = "th-TH";
const TZ = "Asia/Bangkok";
const DASH = "–";
const STALE_AFTER_MS = 3 * 60 * 1000; // the page refreshes every 60 s, so 3 missed refreshes = stale
export const RANGES = [7, 30, 100];
export const DEFAULT_DAYS = 30;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const parseTime = (iso) => (typeof iso === "string" ? Date.parse(iso) : Number.NaN);

// ---- formatting (Intl, th-TH) ----------------------------------------------------------------
const intFmt = new Intl.NumberFormat(LOCALE);
export const fmtInt = (n) => (isNum(n) ? intFmt.format(n) : DASH);

const pctFmt = {};
export function fmtPct(rate, digits = 1) {
  if (!isNum(rate)) return DASH;
  pctFmt[digits] ??= new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${pctFmt[digits].format(rate * 100)}%`;
}

const dayFmt = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" });
export function fmtDay(isoDay) {
  const t = Date.parse(`${isoDay}T00:00:00Z`);
  return Number.isNaN(t) ? DASH : dayFmt.format(t);
}

const timeFmt = new Intl.DateTimeFormat(LOCALE, { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ });
const dateTimeFmt = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ });
export function fmtTime(iso) {
  const t = parseTime(iso);
  return Number.isNaN(t) ? DASH : timeFmt.format(t);
}
export function fmtDateTime(iso) {
  const t = parseTime(iso);
  return Number.isNaN(t) ? DASH : dateTimeFmt.format(t).replace(" เวลา ", " ");
}

// ---- wording ------------------------------------------------------------------------------------
const STATUS = {
  normal: { kind: "ok", text: "ปกติ" },
  yellow: { kind: "warn", text: "ใบเหลือง" },
  brown: { kind: "danger", text: "จุดน้ำตาล" },
  review: { kind: "info", text: "รอตรวจทาน" },
};
const NO_DATA = { kind: "neutral", text: "ยังไม่มีข้อมูล" };
export const statusInfo = (status) => ({ ...(Object.hasOwn(STATUS, status) ? STATUS[status] : NO_DATA) });

/** Abnormal rate vs yesterday. Up is worse (tone "bad"). `eps` is a rate: 0.005 = half a percentage point. */
export function trendInfo(today, yesterday, eps = 0.005) {
  if (!isNum(today) || !isNum(yesterday)) return { dir: "none", tone: "neutral", pts: null, text: "เทียบกับเมื่อวานไม่ได้" };
  const delta = today - yesterday;
  const pts = Math.abs(delta) * 100;
  if (Math.abs(delta) < eps) return { dir: "flat", tone: "neutral", pts, text: "ทรงตัวเท่าเมื่อวาน" };
  const n = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(pts);
  return delta > 0
    ? { dir: "up", tone: "bad", pts, text: `สูงขึ้น ${n} จุดจากเมื่อวาน` }
    : { dir: "down", tone: "good", pts, text: `ลดลง ${n} จุดจากเมื่อวาน` };
}

/** The day before `updatedAtIso` (the server's "today", UTC) from the trends payload: abnormal = yellow + brown,
 *  averaged over stations weighted by accepted photos (n x (1 - rejected_rate)); null when there is nothing to compare. */
export function yesterdayAbnormal(trends, updatedAtIso) {
  const t = parseTime(updatedAtIso);
  if (Number.isNaN(t) || !Array.isArray(trends?.series)) return null;
  const y = new Date(t - 86400000).toISOString().slice(0, 10);
  let sum = 0;
  let weight = 0;
  for (const s of trends.series) {
    for (const p of s?.points ?? []) {
      if (p.day !== y || (p.yellow_rate == null && p.brown_rate == null)) continue;
      const w = (p.n ?? 0) * (1 - (p.rejected_rate ?? 0));
      sum += w * ((p.yellow_rate ?? 0) + (p.brown_rate ?? 0));
      weight += w;
    }
  }
  return weight > 0 ? sum / weight : null;
}

export function freshness(updatedAtIso, nowMs, staleMs = STALE_AFTER_MS) {
  const t = parseTime(updatedAtIso);
  if (Number.isNaN(t)) return "unknown";
  return nowMs - t <= staleMs ? "fresh" : "stale";
}

export function relativeTime(iso, nowMs) {
  const t = parseTime(iso);
  if (Number.isNaN(t)) return "ยังไม่เคยตรวจ";
  const s = Math.max(0, (nowMs - t) / 1000);
  if (s < 45) return "เมื่อสักครู่";
  if (s < 3600) return `${fmtInt(Math.floor(s / 60))} นาทีที่แล้ว`;
  if (s < 86400) return `${fmtInt(Math.floor(s / 3600))} ชั่วโมงที่แล้ว`;
  return `${fmtInt(Math.floor(s / 86400))} วันที่แล้ว`;
}

export function harvestInfo(daysLeft) {
  if (daysLeft < 0) return { kind: "danger", text: `เกินกำหนด ${fmtInt(-daysLeft)} วัน` };
  if (daysLeft === 0) return { kind: "warn", text: "ครบกำหนดวันนี้" };
  return { kind: daysLeft <= 2 ? "warn" : "info", text: `อีก ${fmtInt(daysLeft)} วัน` };
}

/** Thai message for a failed fetch (see getJson); never echoes a response body. */
export function errorMessage(err) {
  if (err?.offline) return "ไม่มีการเชื่อมต่ออินเทอร์เน็ต กำลังแสดงข้อมูลล่าสุดที่โหลดไว้";
  if (err?.timeout) return "เซิร์ฟเวอร์ตอบกลับนานเกินไป กำลังแสดงข้อมูลล่าสุดที่โหลดไว้";
  if (err?.status === 429) {
    const wait = isNum(err.retryAfter) && err.retryAfter > 0 ? ` ระบบจะลองใหม่ใน ${fmtInt(err.retryAfter)} วินาที` : "";
    return `ขอข้อมูลถี่เกินไป โปรดรอสักครู่${wait}`;
  }
  if (err?.status >= 500) return `เซิร์ฟเวอร์ขัดข้องชั่วคราว (รหัส ${err.status}) กำลังแสดงข้อมูลล่าสุดที่โหลดไว้`;
  return "โหลดข้อมูลไม่สำเร็จ ลองใหม่อีกครั้ง";
}

/** GET JSON with a timeout. Rejects with { status, retryAfter } | { offline } | { timeout } (plain objects). */
export async function getJson(url, { signal, timeoutMs = 10000, fetchImpl = globalThis.fetch } = {}) {
  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeoutMs);
  try {
    const r = await fetchImpl(url, { signal: ctl.signal, headers: { Accept: "application/json" } });
    if (!r.ok) throw { status: r.status, retryAfter: Number.parseInt(r.headers?.get?.("Retry-After") ?? "", 10) || null };
    return await r.json();
  } catch (e) {
    if (e && typeof e === "object" && "status" in e) throw e;
    if (signal?.aborted) throw { aborted: true };
    if (timedOut) throw { timeout: true };
    if (globalThis.navigator?.onLine === false || e instanceof TypeError) throw { offline: true };
    throw { status: 0 };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

// ---- query string ---------------------------------------------------------------------------------
export const parseDays = (v) => (RANGES.includes(Number(v)) && /^\d+$/.test(String(v)) ? Number(v) : DEFAULT_DAYS);
export function parseStation(v) {
  if (typeof v !== "string" || !/^[1-9]\d{0,9}$/.test(v)) return null;
  const n = Number(v);
  return n <= 2147483647 ? n : null;
}

// ---- chart models ---------------------------------------------------------------------------------
// A series is told apart by colour AND dash AND marker. The 6 colours are --chart-1..6 (nexxa.css, both themes);
// from the 7th series the dash/marker pairing shifts by one so combinations stay unique up to 36 series.
const LINE_STYLES = [
  { dash: [], point: "circle" },
  { dash: [9, 5], point: "rect" },
  { dash: [2, 5], point: "triangle" },
  { dash: [14, 5, 2, 5], point: "rectRot" },
  { dash: [5, 3], point: "crossRot" },
  { dash: [1, 4], point: "star" },
];
export function seriesStyle(i) {
  const k = ((i % 6) + Math.floor(i / 6)) % 6;
  return { colorVar: `--chart-${(i % 6) + 1}`, dash: [...LINE_STYLES[k].dash], point: LINE_STYLES[k].point };
}

function pointValue(p, field) {
  if (field === "abnormal") return p.yellow_rate == null && p.brown_rate == null ? null : (p.yellow_rate ?? 0) + (p.brown_rate ?? 0);
  const v = p[field];
  return isNum(v) ? v : null;
}

/** series: the API's trends.series. field: a point key, or "abnormal" (= yellow_rate + brown_rate). */
export function buildDaySeries(series, field) {
  if (!Array.isArray(series) || !series.length) return { days: [], datasets: [] };
  const days = [...new Set(series.flatMap((s) => (s?.points ?? []).map((p) => p.day)))].sort();
  const index = new Map(days.map((d, i) => [d, i]));
  const datasets = series.map((s, i) => {
    const data = days.map(() => null);
    for (const p of s?.points ?? []) data[index.get(p.day)] = pointValue(p, field);
    return { label: s.station || `สถานี ${s.station_id}`, stationId: s.station_id, styleIndex: i, data };
  });
  return { days, datasets };
}

export function buildSameAge(sameAge) {
  if (!Array.isArray(sameAge) || !sameAge.length) return { ages: [], datasets: [] };
  const ages = sameAge.map((a) => a.age_day).sort((a, b) => a - b);
  const index = new Map(ages.map((a, i) => [a, i]));
  const byLabel = new Map();
  for (const a of [...sameAge].sort((x, y) => x.age_day - y.age_day)) {
    for (const b of a.batches ?? []) {
      if (!byLabel.has(b.batch_label)) byLabel.set(b.batch_label, ages.map(() => null));
      byLabel.get(b.batch_label)[index.get(a.age_day)] = isNum(b.abnormal_rate) ? b.abnormal_rate : null;
    }
  }
  return { ages, datasets: [...byLabel].map(([label, data], i) => ({ label, styleIndex: i, data })) };
}

export const hasAnyValue = (model) => (model?.datasets ?? []).some((d) => d.data.some((v) => v != null));

const axisOf = (model, kind) => (kind === "age" ? model.ages : model.days);
const axisText = (v, kind) => (kind === "age" ? fmtInt(v) : fmtDay(v));

/** Rows for the accessible data table; same numbers as the chart, formatted for people. */
export function tableModel(model, kind) {
  return {
    head: [kind === "age" ? "อายุ (วัน)" : "วันที่", ...model.datasets.map((d) => d.label)],
    rows: axisOf(model, kind).map((x, i) => [axisText(x, kind), ...model.datasets.map((d) => fmtPct(d.data[i]))]),
  };
}

/** One or two sentences for screen readers and for people who skip the chart. */
export function summaryText(model, kind, metricName) {
  if (!hasAnyValue(model)) return `${metricName}: ยังไม่มีข้อมูลในช่วงนี้`;
  const xs = axisOf(model, kind);
  let best = { v: -1 };
  for (const d of model.datasets) d.data.forEach((v, i) => { if (v != null && v > best.v) best = { v, label: d.label, x: xs[i] }; });
  const where = kind === "age" ? `เมื่ออายุ ${fmtInt(best.x)} วัน` : `เมื่อ ${fmtDay(best.x)}`;
  const latest = model.datasets
    .map((d) => {
      const i = d.data.findLastIndex((v) => v != null);
      return i < 0 ? null : `${d.label} ${fmtPct(d.data[i])}`;
    })
    .filter(Boolean).slice(0, 6).join(", ");
  return `${metricName}: สูงสุด ${fmtPct(best.v)} ที่ ${best.label} ${where} · ค่าล่าสุด ${latest}`;
}

/** Vertical positions for end-of-line labels: at least `gap` apart, inside [lo, hi]; same order as the input. */
export function spreadLabels(ys, gap, lo, hi) {
  const order = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b]);
  const pos = order.map((i) => ys[i]);
  for (let k = 1; k < pos.length; k++) pos[k] = Math.max(pos[k], pos[k - 1] + gap);
  if (pos.length) pos[pos.length - 1] = Math.min(pos[pos.length - 1], hi);
  for (let k = pos.length - 2; k >= 0; k--) pos[k] = Math.min(pos[k], pos[k + 1] - gap);
  if (pos.length) pos[0] = Math.max(pos[0], lo);
  const out = [];
  order.forEach((i, k) => { out[i] = pos[k]; });
  return out;
}
