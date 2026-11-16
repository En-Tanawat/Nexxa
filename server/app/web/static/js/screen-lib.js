// Pure helpers for the robot screen (/screen): wording, the one comparison line, which result the hero shows.
// No DOM here, so tests/js/screen_lib.test.mjs can run it in Node. Only ใบเหลือง / จุดน้ำตาล are ever worded: there is
// no label at all for a clean result's "normal" class.
const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export const STATE_TEXT = {
  idle: "พร้อมทำงาน",
  moving: "กำลังเดินทาง",
  inspecting: "กำลังถ่ายภาพ",
  analyzing: "กำลังวิเคราะห์",
  summarizing: "กำลังสรุปผล...",
  returning: "กลับฐาน",
  docked: "จอดแล้ว",
  error: "ขัดข้อง",
  offline: "ออฟไลน์",
};
export const LEVEL_TEXT = { ok: "ไม่พบโรค", warn: "ควรตรวจ", alert: "ผิดปกติ" };
export const LEVEL_MARK = { ok: "✓", warn: "!", alert: "✕" }; // pills: a second cue besides colour
export const LOW_BATTERY = 20;

export function stateText(robot) {
  if (!robot || !robot.online) return STATE_TEXT.offline;
  return STATE_TEXT[robot.state] ?? robot.state_label ?? "–";
}

export const stationLabel = (name) => (/^สถานี/.test(String(name ?? "")) ? String(name) : `สถานี ${name ?? "–"}`);

const num = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

/** now vs a reference count: arrow + word + "ref→now (+d)". null when either side is unknown. Up is worse. */
export function deltaInfo(now, ref) {
  if (!isNum(now) || !isNum(ref)) return null;
  const d = Math.round((now - ref) * 10) / 10;
  const span = `${num(ref)}→${num(now)}`;
  if (d === 0) return { dir: "flat", arrow: "●", word: "เท่าเดิม", text: span };
  return d > 0
    ? { dir: "up", arrow: "▲", word: "เพิ่มขึ้น", text: `${span}(+${num(d)})` }
    : { dir: "down", arrow: "▼", word: "ลดลง", text: `${span}(${num(d)})` };
}

// short row labels (under a "เทียบ" header) so a row fits one line on a phone; `full` is what a screen reader hears
export const COMPARE_ROWS = [["yesterday", "เมื่อวาน", "เทียบเมื่อวาน"], ["avg7", "เฉลี่ย 7 วัน", "เทียบค่าเฉลี่ย 7 วัน"], ["previous_visit", "รอบก่อน", "เทียบรอบตรวจก่อน"]];

/** The three comparison rows for a summary (station or round): change in ใบเหลือง and จุดน้ำตาล; missing = no reference. */
export function compareRows(s) {
  return COMPARE_ROWS.map(([key, label, full]) => {
    const ref = s?.compare?.[key];
    return { key, label, full, missing: !ref, yellow: ref ? deltaInfo(s.yellow, ref.yellow) : null, brown: ref ? deltaInfo(s.brown, ref.brown) : null };
  });
}

const TREND = {
  increase: { dir: "up", arrow: "▲", text: "แนวโน้มเพิ่มขึ้น" },
  decrease: { dir: "down", arrow: "▼", text: "แนวโน้มลดลง" },
  flat: { dir: "flat", arrow: "●", text: "แนวโน้มทรงตัว" },
};
export const trendInfo = (s) => ({ ...(TREND[s?.trend] ?? { dir: "none", arrow: "", text: "ข้อมูลแนวโน้มไม่พอ" }) });

// ---- lot history chart: pure geometry (screen.js turns it into SVG) ---------------------------------------------
export const CHART = { w: 340, h: 118 }; // default size; screen.js passes the real one so text stays 18px
const PAD = { top: 26, bottom: 26, side: 2 }; // room for the y label row and the x label row
export const RANGES = [["lot", "ทั้งล็อต"], ["30", "30 วัน"], ["14", "14 วัน"], ["7", "7 วัน"]];
export const DEFAULT_RANGE = "lot";
export const HISTORY_REFRESH_MS = 5 * 60 * 1000;
const DAY_MS = 86400000;
const dayMs = (iso) => Date.parse(`${iso}T00:00:00Z`);
const isoOf = (ms) => new Date(ms).toISOString().slice(0, 10);

/** Which history to show: the selected station's, or all stations (round view, nothing selected, or the "ทุกสถานี" chip). */
export function histScope(view, chartAll) {
  return !chartAll && view?.kind === "station" ? view.station.station_id : "all";
}
export const histKey = (scope, range) => `${scope}|${range}`;
export function histUrl(scope, range) {
  const q = new URLSearchParams({ range: RANGES.some(([k]) => k === range) ? range : DEFAULT_RANGE });
  if (isNum(scope)) q.set("station_id", String(scope));
  return `/api/public/screen/history?${q}`;
}
/** Fetch now? Never while hidden; yes when the key changed or the data is older than the refresh time. */
export const histDue = (cached, key, nowMs, hidden = false) => !hidden && (!cached || cached.key !== key || nowMs - cached.at >= HISTORY_REFRESH_MS);

/** h = the /screen/history payload. Bars are 1 per day, or 1 per 7 days when h.bucket is 7; null = no data that slot. */
export function histModel(h, dims = CHART) {
  const n = Array.isArray(h?.y) ? h.y.length : 0;
  if (!n || !Array.isArray(h.b) || h.b.length !== n || Number.isNaN(dayMs(h.start))) return null;
  const bucket = h.bucket === 7 ? 7 : 1;
  const slots = h.y.map((y, i) => (y == null && h.b[i] == null ? null : { yellow: y ?? 0, brown: h.b[i] ?? 0 }));
  const last = n - 1;
  const start = dayMs(h.start);
  const dayOf = (i) => isoOf(start + i * bucket * DAY_MS);
  const known = slots.flatMap((p, i) => (p ? [i] : []));
  const prior = known.filter((i) => i < last).slice(-7); // reference: mean of the 7 data points before the last slot
  const ref = prior.length ? prior.reduce((a, i) => a + slots[i].yellow + slots[i].brown, 0) / prior.length : null;
  const max = Math.max(1, Math.ceil(Math.max(ref ?? 0, ...slots.map((p) => (p ? p.yellow + p.brown : 0)))));
  const top = PAD.top;
  const bottom = Math.max(top + 20, dims.h - PAD.bottom);
  const left = PAD.side;
  const right = dims.w - PAD.side;
  const sw = (right - left) / n;
  const bw = Math.max(1.5, sw * 0.7);
  const k = (bottom - top) / max;
  const xOf = (i) => left + i * sw + (sw - bw) / 2;
  const rects = [];
  slots.forEach((p, i) => {
    if (!p) return;
    const now = i === last && !h.harvested;
    if (p.yellow > 0) rects.push({ kind: "yellow", x: xOf(i), y: bottom - p.yellow * k, w: bw, h: p.yellow * k, now });
    if (p.brown > 0) rects.push({ kind: "brown", x: xOf(i), y: bottom - (p.yellow + p.brown) * k, w: bw, h: p.brown * k, now });
    if (!p.yellow && !p.brown) rects.push({ kind: "zero", x: xOf(i), y: bottom - 3, w: bw, h: 3, now });
  });
  const idx = [...new Set(n === 1 ? [0] : [0, Math.floor(last / 2), last])];
  return {
    n, bucket, max, top, bottom, left, right, rects, points: known.length, ref, refY: ref === null ? null : bottom - ref * k,
    labels: idx.map((i) => ({ x: xOf(i) + bw / 2, day: dayOf(i) })),
    harvestX: h.harvested ? xOf(last) + bw / 2 : null,
    note: known.length === 1 ? "เพิ่งเริ่มเก็บข้อมูล" : known.length === 0 ? "ยังไม่มีข้อมูล" : null,
    totals: { yellow: slots.reduce((a, p) => a + (p ? p.yellow : 0), 0), brown: slots.reduce((a, p) => a + (p ? p.brown : 0), 0) },
  };
}

/** Full lot-age text (detail panel): "วันที่ 23 ของล็อต · เก็บเกี่ยว ~วันที่ 45 (ค่าสมมติ)"; "" with no lot (all stations, no batch). */
export function lotText(h) {
  if (!h?.batch || !isNum(h.age0)) return "";
  const age = h.age0 + h.days - 1;
  if (h.harvested) return `เก็บเกี่ยวแล้ว ล็อตอายุ ${age} วัน`;
  return `วันที่ ${age} ของล็อต · เก็บเกี่ยว ~วันที่ ${h.batch.cycle_days} (ค่าสมมติ)`;
}

/** The same in a few words for the hero ("~" marks the assumed cycle length). */
export function lotShort(h) {
  if (!h?.batch || !isNum(h.age0)) return "";
  const age = h.age0 + h.days - 1;
  return h.harvested ? "เก็บเกี่ยวแล้ว" : `ล็อตวันที่ ${age} · เก็บ ~${h.batch.cycle_days}`;
}

/** Text alternative for the chart (role="img" aria-label). */
export function histLabel(s, model, h) {
  if (!model) return "ยังไม่มีข้อมูลแนวโน้ม";
  const per = model.bucket === 7 ? "รายสัปดาห์" : "รายวัน";
  return `กราฟ${per} ${model.points} จุดที่มีข้อมูล รวมใบเหลือง ${model.totals.yellow} จุดน้ำตาล ${model.totals.brown} ` +
    `สูงสุด ${model.max} ${trendInfo(s).text}${h?.harvested ? " เก็บเกี่ยวแล้ว" : ""}`;
}

/** What the hero shows. data = /api/public/screen; selected = "round" | a station id | null (automatic).
 *  Automatic: the round result once there is one, else the newest station. While the robot summarizes: busy. */
export function pickView(data, selected) {
  if (data?.robot?.online && data.robot.state === "summarizing") return { kind: "busy" };
  const stations = Array.isArray(data?.stations) ? data.stations : [];
  const byId = (id) => stations.find((s) => s.station_id === id && s.summary);
  if (selected === "round" && data.round) return { kind: "round", summary: data.round };
  if (isNum(selected) && byId(selected)) return { kind: "station", station: byId(selected), summary: byId(selected).summary };
  if (data?.round) return { kind: "round", summary: data.round };
  const st = byId(data?.latest);
  return st ? { kind: "station", station: st, summary: st.summary } : { kind: "none" };
}

/** Every summary id in the payload: a new one means "a new result arrived". */
export function summaryIds(data) {
  const ids = (Array.isArray(data?.stations) ? data.stations : []).flatMap((s) => (s.summary ? [`s${s.summary.id}`] : []));
  return data?.round ? [...ids, `r${data.round.id}`] : ids;
}
