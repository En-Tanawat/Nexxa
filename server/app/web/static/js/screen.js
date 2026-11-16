// Robot screen (/screen): polls ONE small endpoint (/api/public/screen) every 4 s while the tab is visible, backs off
// on errors, keeps the display awake and shows the result at a glance (see screen-lib.js for the wording).
// Safety: everything from the API (station names, summary text) goes in with textContent. Never HTML strings.
import { fmtDateTime, fmtInt, fmtPct, getJson } from "./public-lib.js";
import { initRobotCam } from "./robot-cam.js";
import { LEVEL_MARK, LEVEL_TEXT, LOW_BATTERY, compareInfo, pickView, stateText, stationLabel, summaryIds } from "./screen-lib.js";

const API = "/api/public/screen";
const BASE_MS = 4000;
const MAX_MS = 30000;
const NEW_MS = 6000; // how long a new result stays highlighted
const $ = (id) => document.getElementById(id);
const make = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const setText = (el, text) => {
  if (el.textContent !== text) el.textContent = text; // an unchanged value must not re-announce to screen readers
};

let data = null;
let selected = null; // "round" | station id | null = automatic
let seen = null; // summary ids already shown (null until the first load: nothing is "new" then)
let failures = 0;
let timer = null;
let inflight = false;
let lastSig = "";
let newTimer = null;
let cam = null;
let camOn = new URLSearchParams(location.search).get("cam") === "1";

// ---- top strip ---------------------------------------------------------------------------------
function renderStrip(robot) {
  const online = Boolean(robot?.online);
  const state = $("scr-state");
  state.dataset.state = !online ? "offline" : robot.state === "error" ? "error" : robot.state === "summarizing" ? "busy" : "ok";
  setText($("scr-state-text"), stateText(robot));
  const pct = online && Number.isFinite(robot.battery_pct) ? robot.battery_pct : null;
  $("scr-batt").dataset.low = pct !== null && pct < LOW_BATTERY ? "1" : "0";
  setText($("scr-batt-text"), pct === null ? "–" : `${fmtInt(pct)}%`);
}

// ---- hero, tiles, comparison -----------------------------------------------------------------------
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

function renderMain(view) {
  const busy = view.kind === "busy";
  const has = view.kind === "station" || view.kind === "round";
  $("scr-busy").hidden = !busy;
  $("scr-hero").hidden = busy;
  $("scr-tiles").hidden = !has;
  $("scr-compare").hidden = !has;
  $("scr-detail-btn").hidden = !has;
  if (!has) {
    $("scr-detail-btn").setAttribute("aria-expanded", "false");
    $("scr-detail").hidden = true;
  }
  const hero = $("scr-hero");
  if (!has) {
    hero.dataset.level = "none";
    setText($("scr-cap"), "รอผลตรวจ");
    setText($("scr-level-text"), "รอผลตรวจ");
    $("scr-sub").hidden = true;
    return;
  }
  const s = view.summary;
  hero.dataset.level = s.level in LEVEL_TEXT ? s.level : "none";
  setText($("scr-cap"), view.kind === "round" ? "จบรอบ" : stationLabel(view.station.name));
  setText($("scr-level-text"), LEVEL_TEXT[s.level] ?? "รอผลตรวจ");
  const sub = $("scr-sub");
  sub.hidden = !(view.kind === "round" && s.worst);
  if (!sub.hidden) setText(sub, `สูงสุด: ${stationLabel(s.worst.station)}`);
  const sides = (view.kind === "station" ? [s.yellow_angles, s.brown_angles] : [[], []]);
  setTile("yellow", s.yellow, sides[0]);
  setTile("brown", s.brown, sides[1]);
  const c = compareInfo(s);
  $("scr-compare").dataset.dir = c.dir;
  setText($("scr-arrow"), c.arrow);
  setText($("scr-compare-text"), c.text);
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
  const items = stations.map((st, i) => pill(stationLabel(st.name), st.summary?.level, shown === st.station_id,
    () => { selected = st.station_id; paint(true); }, String(st.order || i + 1)));
  if (data.round) items.push(pill("จบรอบ", data.round.level, view.kind === "round", () => { selected = "round"; paint(true); }, "จบ"));
  ul.replaceChildren(...items);
}

/** Repaint when something visible changed (a repaint every 4 s would restart screen-reader announcements). */
function paint(force = false) {
  if (!data) return;
  renderStrip(data.robot);
  const view = pickView(data, selected);
  const sig = JSON.stringify([view, selected, data.stations.map((s) => [s.station_id, s.summary?.level]), data.round?.level]);
  if (force || sig !== lastSig) {
    lastSig = sig;
    renderMain(view);
    renderPills(view);
  }
  syncCam();
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
    paint(true);
    const hero = $("scr-hero");
    hero.classList.add("is-new");
    clearTimeout(newTimer);
    newTimer = setTimeout(() => hero.classList.remove("is-new"), NEW_MS);
    announce(pickView(data, selected));
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
  btn.addEventListener("click", () => {
    const run = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    run?.catch?.(() => {});
  });
  document.addEventListener("fullscreenchange", () => {
    const on = Boolean(document.fullscreenElement);
    btn.setAttribute("aria-pressed", String(on));
    btn.setAttribute("aria-label", on ? "ออกจากเต็มจอ" : "เต็มจอ");
  });
}

let camWasOn = false;
function syncCam() {
  $("scr-cam").hidden = !camOn;
  $("scr-cam-btn").setAttribute("aria-pressed", String(camOn));
  if (!camOn && !camWasOn) return;
  camWasOn = camOn;
  cam ??= initRobotCam();
  cam.setStatus(camOn && data ? data.robot : { online: false }); // off: no timer, no requests
}

// ---- wiring --------------------------------------------------------------------------------------
$("scr-retry").addEventListener("click", () => { failures = 0; tick(); });
$("scr-detail-btn").addEventListener("click", () => {
  const open = $("scr-detail").hidden;
  $("scr-detail").hidden = !open;
  $("scr-detail-btn").setAttribute("aria-expanded", String(open));
  $("scr-detail-btn").firstElementChild.textContent = open ? "ซ่อนรายละเอียด" : "ดูรายละเอียด";
});
$("scr-cam-btn").addEventListener("click", () => { camOn = !camOn; syncCam(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(timer);
  else { keepAwake(); tick(); }
});
window.addEventListener("online", () => { failures = 0; tick(); });
document.addEventListener("pointerdown", keepAwake, { once: true }); // some browsers only grant the lock after a touch
initFullscreen();
keepAwake();
syncCam();
tick();
