// Public dashboard: fetches /api/public/{summary,twin} (+ a small trends call for "vs yesterday") every 60 s,
// pauses while the tab is hidden, keeps the last good data on errors.
// Safety: API data (station names!) only goes into the page with textContent / value / setAttribute on cloned
// <template> nodes. Never HTML strings.
import {
  activityFeed, errorMessage, fmtDateTime, fmtInt, fmtPct, fmtTime, freshness, getJson, harvestInfo,
  relativeTime, statusInfo, trendInfo, yesterdayAbnormal,
} from "./public-lib.js";
import { initRobotCam as initCamFrames } from "./robot-cam.js";

const REFRESH_MS = 60_000;
const TICK_MS = 30_000; // re-evaluates "fresh / stale" and "5 minutes ago" without a request
const TRENDS_EVERY_MS = 10 * 60_000; // yesterday's rate barely changes: spare the rate limit
const $ = (id) => document.getElementById(id);
const make = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const clone = (id) => $(id).content.firstElementChild.cloneNode(true);

const state = { summary: null, twin: null, trends: null, trendsAt: 0, fetchedAt: 0, error: null, timer: null, ticker: null, inflight: false };
let twinInstance = null;

async function syncTwin() {
  const root = $("twin-root");
  if (!root) return;
  if (twinInstance) {
    if (state.twin) twinInstance.update(state.twin);
    return;
  }
  try {
    const { mountTwin } = await import("./twin.js");
    twinInstance = mountTwin(root, () => Promise.resolve(state.twin));
  } catch (err) {
    try {
      const { createFallback } = await import("./twin-fallback.js");
      const fb = createFallback(document);
      root.replaceChildren(fb.el);
      twinInstance = {
        update(d) {
          fb.update({ stations: d?.stations ?? [], layout: { width: 12, depth: 8, items: [] }, home: null, selectedId: null });
        },
      };
      if (state.twin) twinInstance.update(state.twin);
    } catch {
      // Keep dashboard alive even if twin scripts fail
    }
  }
}

function badge(info) {
  const el = clone(`tpl-badge-${info.kind}`);
  el.lastElementChild.textContent = info.text;
  return el;
}

function row(id, name, meta, info, side) {
  const li = clone(id);
  li.querySelector(".row__name").textContent = name;
  li.querySelector(".row__meta").textContent = meta;
  li.querySelector("[data-slot=badge]").replaceWith(badge(info));
  const time = li.querySelector(".row__time");
  if (time) time.textContent = side ?? "";
  return li;
}

function fill(listId, emptyId, rows) {
  $(listId).replaceChildren(...rows);
  $(emptyId).hidden = rows.length > 0;
}

function setMeta(id, ...nodes) {
  $(id).replaceChildren(...nodes);
}

function renderKpis(summary, yesterday) {
  $("kpi-stations").textContent = fmtInt(summary.stations_active);
  $("kpi-captures").textContent = fmtInt(summary.captures_today);
  $("kpi-abnormal").textContent = fmtPct(summary.abnormal_rate_today);
  $("kpi-review").textContent = fmtInt(summary.review_pending);
  $("kpi-harvest").textContent = fmtInt(summary.lots_near_harvest.length);
  for (const section of document.querySelectorAll(".kpi.is-loading")) section.classList.remove("is-loading");

  if (summary.abnormal_rate_today == null) {
    setMeta("kpi-abnormal-meta", make("span", "kpi__note", "ยังไม่มีภาพที่วิเคราะห์ได้วันนี้"));
  } else {
    const t = trendInfo(summary.abnormal_rate_today, yesterday);
    const el = clone(`tpl-trend-${t.dir}`);
    el.classList.add(`trend--${t.tone}`);
    el.lastElementChild.textContent = t.text;
    setMeta("kpi-abnormal-meta", el, make("span", "kpi__note", yesterday == null ? "" : `เมื่อวาน ${fmtPct(yesterday)}`));
  }
  const cycle = summary.harvest_cycle_days_assumed;
  const note = `คำนวณจากรอบเก็บเกี่ยว ${fmtInt(cycle)} วัน (ค่าสมมติ)`;
  setMeta("kpi-harvest-meta", make("span", "", note));
  $("lot-note").textContent = `กำหนดเก็บเกี่ยวคำนวณจากรอบ ${fmtInt(cycle)} วันนับจากวันปลูก (ค่าสมมติ ไม่ใช่วันที่ผู้ปลูกกำหนด)`;
}

function renderLots(summary) {
  const cycle = summary.harvest_cycle_days_assumed || 1;
  fill("lot-list", "lot-empty", summary.lots_near_harvest.map((lot) => {
    const li = row("tpl-lot", lot.station, `${lot.crop ?? "ไม่ระบุผัก"} · อายุ ${fmtInt(lot.age_days)} วัน`, harvestInfo(lot.harvest_in_days));
    const bar = li.querySelector("progress");
    bar.value = Math.max(0, Math.min(1, lot.age_days / cycle));
    bar.setAttribute("aria-label", `อายุ ${lot.age_days} จาก ${cycle} วัน`);
    return li;
  }));
}

function renderStations(twin, now) {
  const stations = twin.stations;
  $("stations-sub").textContent = stations.length ? `${fmtInt(stations.length)} สถานี · สถานะจากภาพล่าสุด` : "สถานะจากภาพล่าสุดของแต่ละสถานี";
  fill("station-list", "station-empty", stations.map((s) => row(
    "tpl-station", s.name,
    `${s.crop ?? "ไม่ระบุผัก"} · ${s.age_days == null ? "ยังไม่มีล็อตที่ปลูกอยู่" : `อายุ ${fmtInt(s.age_days)} วัน`}`,
    statusInfo(s.status), relativeTime(s.last_seen_at, now),
  )));
  fill("activity-list", "activity-empty", activityFeed(stations, now).map((a) => row("tpl-activity", a.name, a.when, a.badge)));
}

function renderPill(now) {
  const pill = $("fresh-pill");
  const updated = state.summary?.updated_at ?? state.twin?.updated_at ?? null;
  let kind = "loading";
  let label = "กำลังโหลดข้อมูล";
  if (updated) {
    kind = freshness(updated, now);
    label = kind === "fresh" ? "ข้อมูลล่าสุด" : "ข้อมูลเก่า";
  } else if (state.error) {
    kind = "error";
    label = "เชื่อมต่อไม่ได้";
  }
  pill.dataset.state = kind;
  pill.className = `badge live-pill badge--${{ fresh: "ok", stale: "warn", error: "danger" }[kind] ?? "neutral"}`;
  $("fresh-label").textContent = label;
  $("fresh-time").hidden = !updated;
  if (updated) {
    const time = $("updated-at");
    time.textContent = fmtTime(updated);
    time.setAttribute("datetime", updated);
    time.setAttribute("title", fmtDateTime(updated));
  }
}

function render() {
  const now = Date.now();
  const { summary, twin } = state;
  renderPill(now);
  const banner = $("error-banner");
  banner.hidden = !state.error;
  if (state.error) $("error-text").textContent = errorMessage(state.error);
  $("demo-banner").hidden = !(summary?.demo || twin?.demo);
  if (!summary && !twin) return; // nothing loaded yet: keep the reserved skeleton
  const empty = (summary?.stations_active ?? 0) === 0 && (twin?.stations.length ?? 0) === 0;
  $("dashboard-empty").hidden = !empty;
  $("kpi-grid").hidden = empty;
  $("dashboard-main").hidden = empty;
  if (summary) {
    renderKpis(summary, yesterdayAbnormal(state.trends, summary.updated_at));
    renderLots(summary);
  }
  if (twin) {
    renderStations(twin, now);
    syncTwin();
  }
}

function schedule(ms) {
  clearTimeout(state.timer);
  state.timer = document.hidden ? null : setTimeout(refresh, ms);
}

async function refresh() {
  if (state.inflight) return;
  clearTimeout(state.timer);
  state.inflight = true;
  const wantTrends = Date.now() - state.trendsAt >= TRENDS_EVERY_MS;
  const [s, t, tr] = await Promise.allSettled([
    getJson("/api/public/summary"),
    getJson("/api/public/twin"),
    wantTrends ? getJson("/api/public/trends?days=2") : Promise.resolve(state.trends),
  ]);
  state.inflight = false;
  if (s.status === "fulfilled") state.summary = s.value;
  if (t.status === "fulfilled") state.twin = t.value;
  if (tr.status === "fulfilled") {
    if (wantTrends) state.trendsAt = Date.now();
    state.trends = tr.value;
  }
  const failed = [s, t].find((r) => r.status === "rejected");
  state.error = failed ? failed.reason : null;
  if (!failed) state.fetchedAt = Date.now();
  render();
  const retry = failed?.reason?.status === 429 ? (failed.reason.retryAfter ?? 60) * 1000 : REFRESH_MS;
  schedule(Math.max(retry, 5_000));
}

function startTicker() {
  clearInterval(state.ticker);
  state.ticker = document.hidden ? null : setInterval(render, TICK_MS);
}

function stopTimers() {
  clearTimeout(state.timer);
  clearInterval(state.ticker);
  state.timer = state.ticker = null;
}

// --- Robot Live Camera and Inspection Step Monitor --------------------------
const ROBOT_POLL_MS = 3_000;
let robotTimer = null;
let robotInflight = false;
let robotCam = null;

function scheduleRobot(ms) {
  clearTimeout(robotTimer);
  robotTimer = document.hidden ? null : setTimeout(pollRobot, ms);
}

function stopRobotTimer() {
  clearTimeout(robotTimer);
  robotTimer = null;
}

async function pollRobot() {
  if (robotInflight || document.hidden) return;
  robotInflight = true;
  try {
    const data = await getJson("/api/public/robot");
    renderRobot(data);
  } catch {
    renderRobotOffline();
  } finally {
    robotInflight = false;
    scheduleRobot(ROBOT_POLL_MS);
  }
}

function renderRobotOffline() {
  const badge = $("robot-badge-status");
  const dot = $("robot-status-dot");
  const label = $("robot-status-label");
  if (badge) badge.className = "badge badge--neutral";
  if (dot) dot.className = "robot-pulse-dot robot-pulse-dot--neutral";
  if (label) label.textContent = "ออฟไลน์";

  if (robotCam) robotCam.setStatus({ online: false });

  if ($("robot-chip-angle")) $("robot-chip-angle").hidden = true;
  if ($("robot-chip-station")) $("robot-chip-station").hidden = true;
  if ($("robot-chip-battery")) $("robot-chip-battery").hidden = true;
  if ($("robot-chip-wifi")) $("robot-chip-wifi").hidden = true;

  if ($("robot-current-step")) $("robot-current-step").textContent = "หุ่นยนต์ออฟไลน์ (ขาดการติดต่อเกิน 45 วินาที)";
  if ($("robot-round-pill")) $("robot-round-pill").textContent = "รอบที่: –";
  if ($("robot-station-pill")) $("robot-station-pill").textContent = "สถานี: –";
  if ($("robot-angle-pill")) $("robot-angle-pill").textContent = "มุมกล้อง: –";

  for (let i = 1; i <= 5; i++) {
    const el = $("robot-step-" + i);
    if (el) {
      el.classList.remove("is-done", "is-active");
      el.classList.add("is-pending");
    }
  }
}

function renderRobot(data) {
  if (!data) return renderRobotOffline();

  const badge = $("robot-badge-status");
  const dot = $("robot-status-dot");
  const label = $("robot-status-label");
  const isOnline = Boolean(data.online);

  if (badge && dot && label) {
    if (!isOnline) {
      badge.className = "badge badge--neutral";
      dot.className = "robot-pulse-dot robot-pulse-dot--neutral";
      label.textContent = "ออฟไลน์";
    } else {
      const stateTone = {
        idle: "ok",
        moving: "info",
        inspecting: "warn",
        analyzing: "info",
        returning: "info",
        docked: "ok",
        error: "danger",
      }[data.state] || "ok";
      badge.className = `badge badge--${stateTone}`;
      dot.className = `robot-pulse-dot robot-pulse-dot--${stateTone}`;
      label.textContent = data.state_label || (isOnline ? "พร้อมใช้งาน" : "ออฟไลน์");
    }
  }

  if (robotCam) robotCam.setStatus(data);

  const angleChip = $("robot-chip-angle");
  const angleText = $("robot-chip-angle-text");
  if (angleChip && angleText) {
    if (data.camera_angle) {
      angleChip.hidden = false;
      angleText.textContent = `มุม: ${data.camera_angle_label || data.camera_angle}`;
    } else {
      angleChip.hidden = true;
    }
  }

  const stationChip = $("robot-chip-station");
  const stationText = $("robot-chip-station-text");
  if (stationChip && stationText) {
    const stName = data.station_name || (data.station_id ? `สถานี ${data.station_id}` : "");
    if (stName) {
      stationChip.hidden = false;
      stationText.textContent = stName;
    } else {
      stationChip.hidden = true;
    }
  }

  const batChip = $("robot-chip-battery");
  const batText = $("robot-chip-battery-text");
  if (batChip && batText) {
    if (data.battery_pct != null) {
      batChip.hidden = false;
      batText.textContent = `${data.battery_pct}%`;
    } else {
      batChip.hidden = true;
    }
  }

  const wifiChip = $("robot-chip-wifi");
  const wifiText = $("robot-chip-wifi-text");
  if (wifiChip && wifiText) {
    if (data.wifi_rssi != null) {
      wifiChip.hidden = false;
      wifiText.textContent = `${data.wifi_rssi} dBm`;
    } else {
      wifiChip.hidden = true;
    }
  }

  if ($("robot-current-step")) {
    $("robot-current-step").textContent = data.step || "-";
  }
  if ($("robot-round-pill")) {
    $("robot-round-pill").textContent = data.round_id ? `รอบที่: ${data.round_id}` : "รอบที่: –";
  }
  if ($("robot-station-pill")) {
    const stName = data.station_name || (data.station_id ? `สถานี ${data.station_id}` : "–");
    $("robot-station-pill").textContent = `สถานี: ${stName}`;
  }
  if ($("robot-angle-pill")) {
    const ang = data.camera_angle ? (data.camera_angle_label || data.camera_angle) : "–";
    $("robot-angle-pill").textContent = `มุมกล้อง: ${ang}`;
  }

  const activeStep = data.step_index || 1;
  for (let i = 1; i <= 5; i++) {
    const el = $("robot-step-" + i);
    if (!el) continue;
    el.classList.remove("is-done", "is-active", "is-pending");
    if (!isOnline) {
      el.classList.add("is-pending");
    } else if (i < activeStep) {
      el.classList.add("is-done");
    } else if (i === activeStep) {
      el.classList.add("is-active");
    } else {
      el.classList.add("is-pending");
    }
  }
}

function initRobotCam() {
  robotCam = initCamFrames();

  const fsBtn = $("robot-cam-fs-btn");
  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      const frame = $("robot-cam-frame");
      if (!frame) return;
      if (!document.fullscreenElement) {
        if (frame.requestFullscreen) frame.requestFullscreen();
        else if (frame.webkitRequestFullscreen) frame.webkitRequestFullscreen();
      } else {
        if (document.exitFullscreen) document.exitFullscreen();
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      }
    });
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopTimers();
    stopRobotTimer();
    return;
  }
  const age = Date.now() - state.fetchedAt;
  if (age >= REFRESH_MS) refresh();
  else schedule(REFRESH_MS - age);
  startTicker();
  render();
  pollRobot();
});
window.addEventListener("online", () => {
  refresh();
  pollRobot();
});
$("retry-btn").addEventListener("click", () => {
  refresh();
  pollRobot();
});

initRobotCam();
refresh();
pollRobot();
startTicker();
