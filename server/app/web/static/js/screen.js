// Robot screen (/screen): polls ONE small endpoint (/api/public/screen) every 4 s while the tab is visible, backs off on
// errors, keeps the display awake. The lot's history (/api/public/screen/history) is fetched only when the station or the
// range changes and then at most every 5 minutes. Landscape first: left = state + result, right = comparison + trend.
// Safety: everything from the API (station names, summary text) goes in with textContent / attributes. Never HTML strings.
import { fmtDateTime, fmtDay, fmtInt, fmtPct, getJson } from "./public-lib.js";
import { initRobotCam } from "./robot-cam.js";
import {
  CHART, DEFAULT_RANGE, HISTORY_REFRESH_MS, LEVEL_MARK, LEVEL_TEXT, LOW_BATTERY, compareRows, histDue, histKey, histLabel, histModel,
  histScope, histUrl, lotShort, lotText, pickView, stateText, stationLabel, summaryIds, trendInfo,
} from "./screen-lib.js";

const API = "/api/public/screen";
const BASE_MS = 4000;
const MAX_MS = 30000;
const NEW_MS = 6000; // how long a new result stays highlighted
const SVG = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const make = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const svg = (tag, attrs = {}, text) => {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  if (text !== undefined) el.textContent = text;
  return el;
};
const setText = (el, text) => {
  if (el.textContent !== text) el.textContent = text; // an unchanged value must not re-announce to screen readers
};
const safe = (fn) => { try { return fn(); } catch { return null; } };

let data = null;
let selected = null; // "round" | station id | null = automatic
let chartAll = false; // the "ทุกสถานี" chip: chart over every station whatever the hero shows
let range = DEFAULT_RANGE;
let hist = null; // { key, at, data } of the last history fetch
let histBusy = false;
let seen = null; // summary ids already shown (null until the first load: nothing is "new" then)
let failures = 0;
let timer = null;
let inflight = false;
let lastSig = "";
let newTimer = null;
let cam = null;
let camOn = new URLSearchParams(location.search).get("cam") === "1";
let detailOpen = false;
let currentView = { kind: "none" };

// ---- top strip -----------------------------------------------------------------------------------
function renderStrip(robot) {
  const online = Boolean(robot?.online);
  $("scr-state").dataset.state = !online ? "offline" : robot.state === "error" ? "error" : robot.state === "summarizing" ? "busy" : "ok";
  setText($("scr-state-text"), stateText(robot));
  const pct = online && Number.isFinite(robot.battery_pct) ? robot.battery_pct : null;
  $("scr-batt").dataset.low = pct !== null && pct < LOW_BATTERY ? "1" : "0";
  setText($("scr-batt-text"), pct === null ? "–" : `${fmtInt(pct)}%`);
}

// ---- hero, tiles ----------------------------------------------------------------------------------
function setTile(name, n, sides) {
  $(`scr-${name}`).dataset.on = n > 0 ? "1" : "0";
  setText($(`scr-${name}-n`), fmtInt(n));
  $(`scr-${name}-sides`).replaceChildren(...(n > 0 ? sides : []).map((s) => make("li", "scr-side", s))); // sides only when something was found
}

function addFact(dl, label, value) {
  const row = make("div");
  row.append(make("dt", "", label), make("dd", "", value));
  dl.append(row);
}

function fillDetail(view) {
  const s = view.summary;
  const dl = $("scr-facts");
  dl.replaceChildren();
  if (view.kind === "round") {
    addFact(dl, "ตรวจแล้ว", `${fmtInt(s.stations_inspected)} จาก ${fmtInt(s.stations_expected)} สถานี`);
    if (s.rising?.length) addFact(dl, "แนวโน้มเพิ่มขึ้น", s.rising.join(", "));
  } else {
    addFact(dl, "ตรวจได้", `${fmtInt(s.present)} จาก 4 ด้าน`);
    addFact(dl, "แนวโน้ม 14 วัน", s.trend_label);
  }
  addFact(dl, "อัตราผิดปกติ", fmtPct(s.abnormal_rate));
  $("scr-text").replaceChildren(...String(s.text ?? "").split("\n").filter((l) => l.trim()).map((l) => make("p", "", l)));
  setText($("scr-src"), `${s.source === "ai" ? "สรุปโดย AI" : "สรุปอัตโนมัติ"} · ${fmtDateTime(s.updated_at)}`);
}

// ---- comparison rows and the trend word ---------------------------------------------------------------
function cell(info) {
  const el = make("span", `scr-cell scr-cell--${info.dir}`);
  el.append(make("span", "scr-cell__arrow", info.arrow), make("span", "scr-cell__text", info.text));
  el.setAttribute("aria-label", `${info.word} ${info.text}`);
  return el;
}

function renderRows(s) {
  const rows = compareRows(s).map((r) => {
    const li = make("li", "scr-row");
    li.setAttribute("aria-label", r.full);
    li.append(make("span", "scr-row__label", r.label));
    if (r.missing) li.append(make("span", "scr-row__none", "ยังไม่มีข้อมูลเทียบ"));
    else li.append(cell(r.yellow), cell(r.brown));
    return li;
  });
  $("scr-rows").replaceChildren($("scr-rows").firstElementChild, ...rows);
  const t = trendInfo(s);
  $("scr-trend").dataset.dir = t.dir;
  setText($("scr-trend-arrow"), t.arrow);
  setText($("scr-trend-text"), t.text);
}

// ---- lot chart --------------------------------------------------------------------------------------
function chartSize() {
  const box = $("scr-chart-box");
  const w = Math.round(box.clientWidth);
  const h = Math.round(box.clientHeight);
  return w >= 120 && h >= 70 ? { w, h } : CHART;
}

function renderChart() {
  const key = histKey(histScope(currentView, chartAll), range);
  const h = hist && hist.key === key ? hist.data : null;
  const dims = chartSize();
  const chart = $("scr-chart");
  chart.setAttribute("viewBox", `0 0 ${dims.w} ${dims.h}`);
  const g = $("scr-chart-g");
  const m = histModel(h, dims);
  const s = currentView.summary;
  chart.setAttribute("aria-label", h ? histLabel(s, m, h) : "กำลังโหลดแนวโน้ม");
  const lot = lotText(h);
  $("scr-lot").hidden = !lot;
  setText($("scr-lot"), lot);
  renderSub();
  if (!m) {
    g.replaceChildren(svg("text", { class: "scr-chart__note", x: dims.w / 2, y: dims.h / 2, "text-anchor": "middle" }, h ? "ยังไม่มีข้อมูล" : "กำลังโหลด"));
    return;
  }
  const out = [svg("line", { class: "scr-axis", x1: m.left, x2: m.right, y1: m.bottom, y2: m.bottom })];
  out.push(svg("text", { class: "scr-chart__label", x: m.left, y: 18 }, `สูงสุด ${m.max}`));
  if (m.refY !== null) {
    out.push(svg("line", { class: "scr-ref", x1: m.left + 88, x2: m.left + 112, y1: 13, y2: 13, "stroke-dasharray": "6 4" }));
    // the trend word sits at the right of this row: on a narrow chart the legend is the short word
    out.push(svg("text", { class: "scr-chart__label", x: m.left + 118, y: 18 }, m.bucket === 7 || dims.w < 430 ? "เฉลี่ย" : "เฉลี่ย 7 วัน"));
    out.push(svg("line", { class: "scr-ref", x1: m.left, x2: m.right, y1: m.refY, y2: m.refY, "stroke-dasharray": "6 4" }));
  }
  for (const r of m.rects) {
    out.push(svg("rect", {
      class: `scr-bar scr-bar--${r.kind}${r.now ? " scr-bar--now" : ""}`, x: r.x, y: r.y, width: r.w, height: r.h,
      ...(r.kind === "brown" ? { fill: "url(#scr-hatch)" } : {}),
    }));
  }
  if (m.harvestX !== null) {
    out.push(svg("line", { class: "scr-harvest", x1: m.harvestX, x2: m.harvestX, y1: 24, y2: m.bottom }));
    out.push(svg("text", { class: "scr-chart__label", x: m.harvestX - 6, y: 40, "text-anchor": "end" }, "เก็บเกี่ยว"));
  }
  m.labels.forEach((l, i) => {
    const anchor = i === 0 ? "start" : i === m.labels.length - 1 && m.labels.length > 1 ? "end" : "middle";
    out.push(svg("text", { class: "scr-chart__label", x: anchor === "start" ? m.left : anchor === "end" ? m.right : l.x, y: dims.h - 5, "text-anchor": anchor }, fmtDay(l.day)));
  });
  if (m.note) out.push(svg("text", { class: "scr-chart__note", x: dims.w / 2, y: (m.top + m.bottom) / 2, "text-anchor": "middle" }, m.note));
  g.replaceChildren(...out);
}

async function syncHistory() {
  const scope = histScope(currentView, chartAll);
  const key = histKey(scope, range);
  if (currentView.kind === "busy" || histBusy || !histDue(hist, key, Date.now(), document.hidden)) return;
  histBusy = true;
  try {
    hist = { key, at: Date.now(), data: await getJson(histUrl(scope, range)) };
  } catch {
    // keep what is shown (when it is the same key) and try again in ~30 s
    hist = { key, at: Date.now() - HISTORY_REFRESH_MS + 30000, data: hist && hist.key === key ? hist.data : null };
  } finally {
    histBusy = false;
  }
  renderChart();
}

// ---- main paint -----------------------------------------------------------------------------------
/** Hero's small line: the worst station (round) or the lot age (station, once the history is in). */
function renderSub() {
  const view = currentView;
  const h = hist && hist.key === histKey(histScope(view, chartAll), range) ? hist.data : null;
  const text = view.kind === "round" && view.summary.worst ? `สูงสุด: ${stationLabel(view.summary.worst.station)}` : view.kind === "station" ? lotShort(h) : "";
  const sub = $("scr-sub");
  sub.hidden = !text;
  if (text) setText(sub, text);
  sub.title = view.kind === "station" ? lotText(h) : "";
}

function renderMain(view) {
  const busy = view.kind === "busy";
  const has = view.kind === "station" || view.kind === "round";
  $("scr-busy").hidden = !busy;
  $("scr-hero").hidden = busy;
  $("scr-tiles").hidden = !has;
  $("scr-cmp").hidden = !has;
  $("scr-detail-btn").hidden = !has;
  if (!has) {
    detailOpen = false;
    syncPanels();
    const hero = $("scr-hero");
    hero.dataset.level = "none";
    setText($("scr-cap"), "รอผลตรวจ");
    setText($("scr-level-text"), "รอผลตรวจ");
    $("scr-sub").hidden = true;
    return;
  }
  const s = view.summary;
  $("scr-hero").dataset.level = s.level in LEVEL_TEXT ? s.level : "none";
  setText($("scr-cap"), view.kind === "round" ? "จบรอบ" : stationLabel(view.station.name));
  setText($("scr-level-text"), LEVEL_TEXT[s.level] ?? "รอผลตรวจ");
  renderSub();
  const sides = view.kind === "station" ? [s.yellow_angles, s.brown_angles] : [[], []];
  setTile("yellow", s.yellow, sides[0]);
  setTile("brown", s.brown, sides[1]);
  renderRows(s);
  fillDetail(view);
}

function pill(label, level, pressed, onClick, text) {
  const li = make("li");
  const b = make("button", "scr-pill");
  b.type = "button";
  b.dataset.level = level ?? "none";
  b.setAttribute("aria-pressed", String(pressed));
  b.setAttribute("aria-label", `${label} ${level ? LEVEL_TEXT[level] : "รอตรวจ"}`);
  if (level) b.append(make("span", "", text), make("span", "scr-pill__mark", LEVEL_MARK[level]));
  else {
    b.disabled = true; // nothing to show for a station not visited yet
    b.append(make("span", "", text));
  }
  if (level) b.addEventListener("click", onClick);
  li.append(b);
  return li;
}

function renderPills(view) {
  const ul = $("scr-pills");
  const stations = data.stations ?? [];
  ul.hidden = stations.length === 0;
  const shown = view.kind === "station" ? view.station.station_id : null;
  const items = stations.map((st, i) => pill(stationLabel(st.name), st.summary?.level, shown === st.station_id && !chartAll,
    () => { selected = st.station_id; chartAll = false; paint(true); }, String(st.order || i + 1)));
  if (data.round) items.push(pill("จบรอบ", data.round.level, view.kind === "round", () => { selected = "round"; chartAll = false; paint(true); }, "จบ"));
  const all = make("li");
  const b = make("button", "scr-pill scr-pill--all", "ทุกสถานี");
  b.type = "button";
  b.setAttribute("aria-pressed", String(chartAll));
  b.addEventListener("click", () => { chartAll = !chartAll; paint(true); });
  all.append(b);
  ul.replaceChildren(...items, all);
}

function syncRanges() {
  document.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
}

function syncPanels() {
  $("screen").dataset.panel = camOn ? "cam" : detailOpen ? "detail" : "";
  $("scr-detail").hidden = !detailOpen;
  $("scr-detail-btn").setAttribute("aria-expanded", String(detailOpen));
  $("scr-detail-btn").firstElementChild.textContent = detailOpen ? "ซ่อนรายละเอียด" : "ดูรายละเอียด";
  $("scr-cam").hidden = !camOn;
  $("scr-cam-btn").setAttribute("aria-pressed", String(camOn));
}

/** Repaint when something visible changed (a repaint every 4 s would restart screen-reader announcements). */
function paint(force = false) {
  if (!data) return;
  renderStrip(data.robot);
  const view = pickView(data, selected);
  currentView = view;
  const sig = JSON.stringify([view, selected, chartAll, data.stations.map((s) => [s.station_id, s.summary?.level]), data.round?.level]);
  if (force || sig !== lastSig) {
    lastSig = sig;
    renderMain(view);
    renderPills(view);
    renderChart();
  }
  syncPanels();
  syncCam();
  syncHistory();
}

function announce(view) {
  const live = $("scr-live");
  if (view.kind === "round") live.textContent = `จบรอบ ${LEVEL_TEXT[view.summary.level] ?? ""}`;
  else if (view.kind === "station") live.textContent = `${stationLabel(view.station.name)} ${LEVEL_TEXT[view.summary.level] ?? ""}`;
}

function onData(next) {
  data = next;
  const ids = summaryIds(next);
  const fresh = seen ? ids.filter((id) => !seen.has(id)) : [];
  seen = new Set([...(seen ?? []), ...ids]);
  if (fresh.length) {
    selected = null; // a new result takes the hero
    chartAll = false;
    paint(true);
    const hero = $("scr-hero");
    hero.classList.add("is-new");
    clearTimeout(newTimer);
    newTimer = setTimeout(() => hero.classList.remove("is-new"), NEW_MS);
    announce(currentView);
  } else {
    paint();
  }
}

// ---- polling -------------------------------------------------------------------------------------
function schedule(retryAfter) {
  clearTimeout(timer);
  if (document.hidden) return; // the visibilitychange handler restarts it
  const wait = failures ? Math.min(MAX_MS, BASE_MS * 2 ** failures) : BASE_MS;
  timer = setTimeout(tick, Math.max(wait, (retryAfter ?? 0) * 1000));
}

async function tick() {
  clearTimeout(timer);
  if (document.hidden || inflight) return;
  inflight = true;
  let retryAfter = 0;
  try {
    onData(await getJson(API));
    failures = 0;
    $("scr-error").hidden = true;
  } catch (err) {
    failures += 1;
    retryAfter = err?.retryAfter ?? 0;
    $("scr-error").hidden = false; // the last good data stays on screen
  } finally {
    inflight = false;
    schedule(retryAfter);
  }
}

// ---- keep awake, full screen, camera ---------------------------------------------------------------
let wake = null;
async function keepAwake() {
  if (!("wakeLock" in navigator) || document.hidden || wake) return;
  try {
    wake = await navigator.wakeLock.request("screen");
    wake.addEventListener("release", () => { wake = null; });
  } catch {
    wake = null; // refused (battery saver, no permission): the screen simply may dim
  }
}

function initFullscreen() {
  const btn = $("scr-fs-btn");
  if (!document.fullscreenEnabled || !document.documentElement.requestFullscreen) return; // e.g. iPhone Safari: no button
  btn.hidden = false;
  btn.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
        safe(() => screen.orientation?.lock?.("landscape")?.catch?.(() => {})); // best effort: many browsers refuse
      }
    } catch {
      // refused: nothing to do
    }
  });
  document.addEventListener("fullscreenchange", () => {
    const on = Boolean(document.fullscreenElement);
    btn.setAttribute("aria-pressed", String(on));
    btn.setAttribute("aria-label", on ? "ออกจากเต็มจอ" : "เต็มจอ");
    if (!on) safe(() => screen.orientation?.unlock?.());
  });
}

let camWasOn = false;
function syncCam() {
  if (!camOn && !camWasOn) return;
  camWasOn = camOn;
  cam ??= initRobotCam();
  cam.setStatus(camOn && data ? data.robot : { online: false }); // off: no timer, no requests
}

// ---- wiring --------------------------------------------------------------------------------------
$("scr-retry").addEventListener("click", () => { failures = 0; tick(); });
$("scr-detail-btn").addEventListener("click", () => { detailOpen = !detailOpen; syncPanels(); });
$("scr-cam-btn").addEventListener("click", () => { camOn = !camOn; syncPanels(); syncCam(); });
document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
  range = b.dataset.range;
  syncRanges();
  renderChart();
  syncHistory();
}));
const hint = $("scr-hint");
if (safe(() => sessionStorage.getItem("scr-hint")) === "1") hint.hidden = true;
$("scr-hint-x").addEventListener("click", () => {
  hint.hidden = true;
  safe(() => sessionStorage.setItem("scr-hint", "1"));
});
if ("ResizeObserver" in window) new ResizeObserver(() => data && renderChart()).observe($("scr-chart-box"));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(timer);
  else { keepAwake(); tick(); }
});
window.addEventListener("online", () => { failures = 0; tick(); });
document.addEventListener("pointerdown", keepAwake, { once: true }); // some browsers only grant the lock after a touch
syncRanges();
syncPanels();
initFullscreen();
keepAwake();
tick();
