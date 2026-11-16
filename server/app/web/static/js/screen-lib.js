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

/** One short line (<= 25 characters) from the summary's own numbers: change in the abnormal rate in percentage points. */
export function compareInfo(s) {
  const vs7 = isNum(s?.vs_baseline_pp);
  const pp = vs7 ? s.vs_baseline_pp : isNum(s?.vs_previous_pp) ? s.vs_previous_pp : null;
  if (pp === null) return { dir: "none", arrow: "", text: "ยังไม่มีข้อมูลเทียบ" };
  const ref = vs7 ? "7 วันก่อน" : "รอบก่อน";
  if (Math.abs(pp) < 0.05) return { dir: "flat", arrow: "●", text: "ทรงตัว" };
  return pp > 0 ? { dir: "up", arrow: "▲", text: `สูงกว่า ${ref}` } : { dir: "down", arrow: "▼", text: `ต่ำกว่า ${ref}` };
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
