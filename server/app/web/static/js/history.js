// Round summaries: /history (list) and /history/{id} (round card + one card per station).
// Safety: summary text can come from an AI and station names from admins, so everything is put in the page with
// textContent / setAttribute on cloned <template> nodes. Never HTML strings.
import { errorMessage, fmtDateTime, fmtInt, fmtPct, getJson } from "./public-lib.js";

const $ = (id) => document.getElementById(id);
const make = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const clone = (id) => $(id).content.firstElementChild.cloneNode(true);

const LABEL_KIND = { "ปกติ": "ok", "ใบเหลือง": "warn", "จุดน้ำตาล": "danger" };
const STATUS = { done: ["ok", "เสร็จสิ้น"], open: ["info", "กำลังตรวจ"], demo: ["neutral", "ข้อมูลตัวอย่าง"] };
const TREND = { increase: ["up", "bad"], decrease: ["down", "good"], flat: ["flat", "neutral"], insufficient: ["none", "neutral"] };
const ppFmt = new Intl.NumberFormat("th-TH", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const isNum = (v) => typeof v === "number" && Number.isFinite(v);

function badge(kind, text) {
  const el = clone(`tpl-badge-${kind}`);
  el.lastElementChild.textContent = text;
  return el;
}

function trendEl(dir, tone, text) {
  const el = clone(`tpl-trend-${dir}`);
  el.classList.add(`trend--${tone}`);
  el.lastElementChild.textContent = text;
  return el;
}

/** Change in percentage points of the abnormal rate; up is worse. */
function changeEl(pp) {
  if (!isNum(pp)) return trendEl("none", "neutral", "เทียบไม่ได้");
  if (Math.abs(pp) < 0.05) return trendEl("flat", "neutral", "เท่าเดิม");
  return pp > 0
    ? trendEl("up", "bad", `สูงขึ้น ${ppFmt.format(pp)} จุด`)
    : trendEl("down", "good", `ต่ำลง ${ppFmt.format(-pp)} จุด`);
}

function paragraphs(el, text) {
  const lines = String(text ?? "").split("\n").filter((l) => l.trim());
  el.replaceChildren(...(lines.length ? lines : ["AI กำลังสรุป ระบบจะลองใหม่เองอัตโนมัติ"]).map((l) => make("p", "", l)));
}

function sourceLine(el, summary) {
  const ai = summary.source === "ai";
  el.replaceChildren(badge(ai ? "info" : "neutral", ai ? "สรุปโดย AI" : "รอ AI สรุป"), make("span", "", `อัปเดต ${fmtDateTime(summary.updated_at)}`));
}

function addFact(dl, label, ...nodes) {
  const row = make("div");
  const dd = make("dd");
  dd.append(...nodes);
  row.append(make("dt", "", label), dd);
  dl.append(row);
}

function showError(err) {
  $("error-banner").hidden = false;
  $("error-text").textContent = err?.status === 404 ? "ไม่พบรอบตรวจนี้ อาจถูกลบไปแล้ว" : errorMessage(err);
}

// ---- list ------------------------------------------------------------------------------------------
async function loadList() {
  const list = $("round-list");
  try {
    const data = await getJson("/api/public/rounds?limit=20");
    $("error-banner").hidden = true;
    $("demo-banner").hidden = !data.demo;
    $("history-empty").hidden = data.rounds.length > 0;
    list.hidden = data.rounds.length === 0;
    list.replaceChildren(...data.rounds.map((r) => {
      const li = clone("tpl-round");
      li.querySelector("a").setAttribute("href", `/history/${Number(r.id)}`);
      li.querySelector(".round-item__title").textContent = `รอบที่ ${fmtInt(r.id)}`;
      li.querySelector(".round-item__meta").textContent = `${fmtDateTime(r.started_at)} · ${fmtInt(r.stations)} สถานี`;
      li.querySelector(".round-item__head").textContent = r.headline ?? "ยังไม่มีสรุปของรอบนี้";
      const [kind, text] = STATUS[r.status] ?? ["neutral", r.status];
      li.querySelector("[data-slot=badge]").replaceWith(badge(kind, text));
      return li;
    }));
  } catch (err) {
    showError(err);
  }
}

// ---- detail ------------------------------------------------------------------------------------------
function comparisonFacts(dl, f) {
  addFact(dl, "อัตราผิดปกติรอบนี้", fmtPct(f.abnormal_rate));
  const b = f.baseline;
  if (b.rate == null) addFact(dl, "เทียบ 7 วันก่อนหน้า", "ไม่มีข้อมูลย้อนหลัง");
  else addFact(dl, "เทียบ 7 วันก่อนหน้า", `ก่อนหน้า ${fmtPct(b.rate)}`, changeEl(b.delta_pp));
}

function stationCard(s) {
  const card = clone("tpl-station-card");
  const f = s.facts;
  const q = (name) => card.querySelector(`[data-f=${name}]`);
  q("name").textContent = s.name;
  if (!s.summary || !f) {
    q("meta").textContent = "ยังไม่มีสรุป";
    q("src").hidden = true;
    q("facts").hidden = true;
    paragraphs(q("text"), "ยังไม่มีสรุปของสถานีนี้ อาจกำลังสร้างอยู่ ลองโหลดหน้านี้ใหม่ภายหลัง");
    return card;
  }
  q("meta").textContent = `ถ่ายได้ ${fmtInt(f.present.length)} จาก ${fmtInt(f.expected ?? 3)} รูป`;
  sourceLine(q("src"), s.summary);
  const dl = q("facts");
  comparisonFacts(dl, f);
  const [dir, tone] = TREND[f.trend.direction] ?? TREND.insufficient;
  addFact(dl, "แนวโน้ม 14 วัน", trendEl(dir, tone, f.trend.label));
  const p = f.previous_visit;
  if (!p) addFact(dl, "เทียบการตรวจครั้งก่อน", "ยังไม่มีรอบก่อนหน้า");
  else if (p.abnormal_rate == null) addFact(dl, "เทียบการตรวจครั้งก่อน", `รอบที่ ${fmtInt(p.round_id)} ไม่มีผลวิเคราะห์`);
  else addFact(dl, "เทียบการตรวจครั้งก่อน", `รอบที่ ${fmtInt(p.round_id)} · ${fmtPct(p.abnormal_rate)}`, changeEl(p.delta_pp));
  if (f.batch) addFact(dl, "อายุล็อตที่ปลูกอยู่", `${fmtInt(f.batch.age_days)} วัน`);
  paragraphs(q("text"), s.summary.text);
  return card;
}

function renderRound(d) {
  $("demo-banner").hidden = !d.demo;
  const [, statusText] = STATUS[d.status] ?? [null, d.status];
  $("round-meta").textContent = `เริ่ม ${fmtDateTime(d.started_at)} · ${statusText}`;
  const dl = $("round-facts");
  dl.replaceChildren();
  if (d.summary && d.facts) {
    const f = d.facts;
    sourceLine($("round-src"), d.summary);
    paragraphs($("round-text"), d.summary.text);
    addFact(dl, "สถานีที่ตรวจ", `${fmtInt(f.stations_inspected)} จาก ${fmtInt(f.stations_expected)}`);
    comparisonFacts(dl, f);
    if (f.worst) addFact(dl, "สถานีที่ผิดปกติสูงสุด", `${f.worst.station} · ${fmtPct(f.worst.abnormal_rate)}`);
    if (f.duration_min != null) addFact(dl, "ใช้เวลา", `${fmtInt(f.duration_min)} นาที`);
  } else {
    $("round-src").replaceChildren();
    paragraphs($("round-text"), "ยังไม่มีสรุปของทั้งรอบ รอบนี้อาจยังตรวจไม่เสร็จ");
  }
  $("station-cards").replaceChildren(...d.stations.map(stationCard));
}

async function loadDetail() {
  try {
    renderRound(await getJson(`/api/public/rounds/${encodeURIComponent($("round-detail").dataset.roundId)}`));
    $("error-banner").hidden = true;
  } catch (err) {
    showError(err);
    $("round-text").replaceChildren(make("p", "list-empty", "ไม่สามารถแสดงสรุปได้"));
  }
}

const load = $("round-detail") ? loadDetail : loadList;
$("retry-btn").addEventListener("click", load);
load();
