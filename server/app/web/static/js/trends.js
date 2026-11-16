// Public trends page: four Chart.js line charts from /api/public/trends, each with a text summary, a legend that
// repeats colour + dash + marker, direct end-of-line labels, and the same numbers as a table.
// Safety: API data (station names!) only reaches the DOM through textContent / setAttribute on elements made with
// createElement; Chart.js draws it on a canvas as plain text.
import {
  RANGES, buildDaySeries, buildSameAge, errorMessage, fmtDay, fmtInt, fmtPct, getJson, hasAnyValue,
  parseDays, parseStation, seriesStyle, spreadLabels, summaryText, tableModel,
} from "./public-lib.js";

const Chart = globalThis.Chart;
const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const make = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const svgEl = (tag, attrs) => {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

const CHARTS = [
  { key: "yellow", name: "อัตราใบเหลือง", kind: "day", build: (t) => buildDaySeries(t.series, "yellow_rate"), emptyHint: "ยังไม่มีข้อมูลใบเหลืองในช่วงนี้ ลองเลือกช่วงที่ยาวขึ้น" },
  { key: "brown", name: "อัตราจุดน้ำตาล", kind: "day", build: (t) => buildDaySeries(t.series, "brown_rate"), emptyHint: "ยังไม่มีข้อมูลจุดน้ำตาลในช่วงนี้ ลองเลือกช่วงที่ยาวขึ้น" },
  { key: "sameage", name: "อัตราผิดปกติเทียบล็อตที่อายุเท่ากัน", kind: "age", build: (t) => buildSameAge(t.same_age), emptyHint: "ยังไม่มีล็อตที่มีข้อมูลพอจะเทียบกันในช่วงนี้" },
  { key: "rejected", name: "อัตราภาพที่ถูกปฏิเสธ", kind: "day", build: (t) => buildDaySeries(t.series, "rejected_rate"), emptyHint: "ยังไม่มีข้อมูลภาพในช่วงนี้ ลองเลือกช่วงที่ยาวขึ้น" },
];
const MARKS = { // marker outlines for the legend, centred on (0,0); same shapes Chart.js uses for pointStyle
  circle: "M-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0",
  rect: "M-3.5 -3.5h7v7h-7z",
  triangle: "M0 -4.5L4.5 3.5H-4.5Z",
  rectRot: "M0 -5L5 0L0 5L-5 0Z",
  crossRot: "M-3.5 -3.5L3.5 3.5M3.5 -3.5L-3.5 3.5",
  star: "M0 -5L1.5 -1.6L5 -1.5L2.3 0.8L3.1 4.4L0 2.4L-3.1 4.4L-2.3 0.8L-5 -1.5L-1.5 -1.6Z",
};
const NARROW_PX = 520; // below this the plot is too small for end labels: the legend carries the names

const q = new URLSearchParams(location.search);
const state = { days: parseDays(q.get("days")), stationId: parseStation(q.get("station")), trends: null, stationsLoaded: false, charts: new Map(), ctl: null, seq: 0 };

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function theme() {
  return {
    text: cssVar("--color-muted-foreground"), fg: cssVar("--color-foreground"), grid: cssVar("--divider"),
    tipBg: cssVar("--color-background"), tipBorder: cssVar("--color-border"),
    series: Array.from({ length: 6 }, (_, i) => cssVar(`--chart-${i + 1}`)),
  };
}

// Chart.defaults: set once. Colours depend on the theme, so they are passed per chart (see draw()).
function setDefaults() {
  Chart.defaults.responsive = true;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.font.family = cssVar("--font-sans");
  Chart.defaults.font.size = 12;
  Chart.defaults.animation = reduceMotion.matches ? false : { duration: 450 };
}

const colorOf = (style, colors) => colors.series[Number(style.colorVar.slice(-1)) - 1];
const EDGE_PX = 14; // a series gets a direct label only if its line reaches (almost) the right edge of the plot
const fitText = (ctx, text, maxPx) => {
  const chars = Array.from(text);
  if (ctx.measureText(text).width <= maxPx) return text;
  while (chars.length > 1 && ctx.measureText(`${chars.join("")}…`).width > maxPx) chars.pop();
  return `${chars.join("")}…`;
};

// Direct labels at the end of each line (pushed apart so they never overlap).
const endLabels = {
  id: "endLabels",
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts.enabled) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.font = `600 11px ${Chart.defaults.font.family}`;
    const items = [];
    chart.data.datasets.forEach((ds, i) => {
      const k = ds.data.findLastIndex((v) => v != null);
      const pt = k >= 0 ? chart.getDatasetMeta(i).data[k] : null;
      if (pt && chartArea.right - pt.x <= EDGE_PX) items.push({ x: pt.x, y: pt.y, text: fitText(ctx, ds.label, opts.maxWidth), color: ds.borderColor });
    });
    const ys = spreadLabels(items.map((it) => it.y), 15, chartArea.top + 6, chartArea.bottom - 4);
    ctx.textBaseline = "middle";
    items.forEach((it, i) => {
      ctx.fillStyle = opts.color;
      ctx.fillText(it.text, it.x + 10, ys[i]);
      ctx.strokeStyle = it.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(it.x + 2, it.y);
      ctx.lineTo(it.x + 7, ys[i]);
      ctx.stroke();
    });
    ctx.restore();
  },
};
const LABEL_PAD = 124;

function draw(def, model, colors, instant) {
  const canvas = $(`${def.key}-canvas`);
  Chart.getChart(canvas)?.destroy();
  const labels = def.kind === "age" ? model.ages : model.days;
  const narrow = $(`${def.key}-box`).clientWidth < NARROW_PX;
  const step = Math.max(1, Math.ceil(labels.length / 12)); // sparse markers keep long ranges readable
  const max = Math.max(0, ...model.datasets.flatMap((d) => d.data.filter((v) => v != null)));
  const digits = max < 0.1 ? 1 : 0;
  const datasets = model.datasets.map((d) => {
    const st = seriesStyle(d.styleIndex);
    const color = colorOf(st, colors);
    return {
      label: d.label, data: d.data, borderColor: color, backgroundColor: color, borderWidth: labels.length > 60 ? 2 : 2.5, borderDash: st.dash,
      pointStyle: st.point, pointBorderColor: color, pointBackgroundColor: color, pointBorderWidth: 2,
      pointRadius: (c) => (labels.length <= 14 || c.dataIndex % step === 0 || c.dataIndex === labels.length - 1 ? 3.5 : 0),
      pointHoverRadius: 6, tension: 0, spanGaps: true,
    };
  });
  const chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: [endLabels],
    options: {
      animation: instant ? false : undefined,
      layout: { padding: { right: narrow ? 6 : LABEL_PAD, top: 6 } },
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          grid: { display: false }, border: { color: colors.grid },
          ticks: { color: colors.text, maxRotation: 0, autoSkip: true, maxTicksLimit: narrow ? 5 : 8, callback(v) { const l = this.getLabelForValue(v); return def.kind === "age" ? fmtInt(l) : fmtDay(l); } },
          title: def.kind === "age" ? { display: true, text: "อายุนับจากวันปลูก (วัน)", color: colors.text } : { display: false },
        },
        y: {
          beginAtZero: true, suggestedMax: Math.max(0.1, max * 1.12), border: { display: false }, grid: { color: colors.grid },
          ticks: { color: colors.text, maxTicksLimit: 6, callback: (v) => fmtPct(v, digits) },
        },
      },
      plugins: {
        legend: { display: false },
        endLabels: { enabled: !narrow, color: colors.fg, maxWidth: LABEL_PAD - 14 },
        tooltip: {
          backgroundColor: colors.tipBg, borderColor: colors.tipBorder, borderWidth: 1, titleColor: colors.fg, bodyColor: colors.fg,
          padding: 10, boxPadding: 4, usePointStyle: true,
          filter: (item) => item.parsed.y != null,
          callbacks: {
            title: (items) => (def.kind === "age" ? `อายุ ${fmtInt(Number(items[0].label))} วัน` : fmtDay(items[0].label)),
            label: (c) => `${c.dataset.label}: ${fmtPct(c.parsed.y)}`,
          },
        },
      },
    },
  });
  state.charts.set(def.key, { chart, narrow });
}

function legend(def, model, colors) {
  $(`${def.key}-legend`).replaceChildren(...model.datasets.map((d) => {
    const st = seriesStyle(d.styleIndex);
    const color = colorOf(st, colors);
    const svg = svgEl("svg", { width: 40, height: 14, viewBox: "0 0 40 14", "aria-hidden": "true", focusable: "false" });
    svg.append(
      svgEl("line", { x1: 1, y1: 7, x2: 39, y2: 7, stroke: color, "stroke-width": 2.5, "stroke-dasharray": st.dash.join(" ") || "none", "stroke-linecap": "butt" }),
      svgEl("path", { d: MARKS[st.point], transform: "translate(20 7)", fill: st.point === "crossRot" ? "none" : color, stroke: color, "stroke-width": 2 }),
    );
    const li = make("li");
    li.append(svg, make("span", "", d.label));
    return li;
  }));
}

function table(def, model) {
  const t = tableModel(model, def.kind);
  const tbl = make("table", "table");
  tbl.append(make("caption", "sr-only", `ตารางข้อมูล: ${def.name}`));
  const head = make("tr");
  for (const h of t.head) {
    const th = make("th", head.children.length ? "num" : "", h);
    th.scope = "col";
    head.append(th);
  }
  const thead = make("thead");
  thead.append(head);
  const body = make("tbody");
  for (const r of t.rows) {
    const tr = make("tr");
    r.forEach((cell, i) => tr.append(make("td", i ? "num" : "", cell)));
    body.append(tr);
  }
  tbl.append(thead, body);
  $(`${def.key}-table`).replaceChildren(tbl);
}

function setBox(def, boxState, message) {
  $(`${def.key}-box`).dataset.state = boxState;
  if (message !== undefined) $(`${def.key}-msg`).textContent = message;
}

function renderAll(instant = false) {
  const data = state.trends;
  if (!data) return;
  const colors = theme();
  for (const def of CHARTS) {
    const model = def.build(data);
    $(`${def.key}-summary`).textContent = summaryText(model, def.kind, def.name);
    if (!hasAnyValue(model)) {
      state.charts.get(def.key)?.chart.destroy();
      state.charts.delete(def.key);
      $(`${def.key}-legend`).replaceChildren();
      $(`${def.key}-table`).replaceChildren();
      setBox(def, "empty", def.emptyHint);
      continue;
    }
    legend(def, model, colors);
    table(def, model);
    draw(def, model, colors, instant);
    setBox(def, "ready");
  }
}

function fillStations(list) {
  const select = $("station-filter");
  const keep = state.stationId == null ? "" : String(state.stationId);
  select.replaceChildren(...[["", "ทุกสถานี"], ...list.map((s) => [String(s.id), s.name])].map(([v, label]) => {
    const o = make("option", "", label);
    o.value = v;
    return o;
  }));
  select.value = [...select.options].some((o) => o.value === keep) ? keep : "";
  state.stationsLoaded = list.length > 0;
}

function syncUrl() {
  const p = new URLSearchParams();
  if (state.days !== 30) p.set("days", String(state.days));
  if (state.stationId != null) p.set("station", String(state.stationId));
  history.replaceState(null, "", String(p) ? `?${p}` : location.pathname);
}

const status = (text) => { $("trends-status").textContent = text; };

async function load() {
  state.ctl?.abort();
  const ctl = (state.ctl = new AbortController());
  const mine = ++state.seq;
  $("trends-error").hidden = true;
  status("กำลังโหลดกราฟ…");
  const url = `/api/public/trends?days=${state.days}${state.stationId == null ? "" : `&station_id=${state.stationId}`}`;
  try {
    const data = await getJson(url, { signal: ctl.signal });
    if (mine !== state.seq) return;
    state.trends = data;
    if (!state.stationsLoaded && state.stationId == null) {
      fillStations(data.series.map((s) => ({ id: s.station_id, name: s.station || `สถานี ${s.station_id}` })));
      state.stationsLoaded = false; // a later twin answer may still add stations without data in this range
    }
    renderAll();
    status(`แสดงข้อมูล ${fmtInt(state.days)} วันล่าสุด`);
  } catch (e) {
    if (e?.aborted || mine !== state.seq) return;
    $("trends-error-text").textContent = errorMessage(e);
    $("trends-error").hidden = false;
    status("โหลดกราฟไม่สำเร็จ");
    if (!state.trends) for (const def of CHARTS) { setBox(def, "empty", "โหลดกราฟไม่สำเร็จ"); $(`${def.key}-summary`).textContent = `${def.name}: โหลดข้อมูลไม่สำเร็จ`; }
  }
}

function selectRange(days) {
  state.days = days;
  for (const b of document.querySelectorAll("[data-days]")) b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days));
  syncUrl();
  load();
}

function init() {
  if (!Chart) {
    $("trends-error-text").textContent = "โหลดไลบรารีกราฟไม่สำเร็จ ลองรีเฟรชหน้านี้";
    $("trends-error").hidden = false;
    status("โหลดกราฟไม่สำเร็จ");
    return;
  }
  setDefaults();
  reduceMotion.addEventListener("change", setDefaults);
  for (const b of document.querySelectorAll("[data-days]")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days));
    b.addEventListener("click", () => selectRange(Number(b.dataset.days)));
  }
  $("station-filter").addEventListener("change", (e) => {
    state.stationId = parseStation(e.target.value);
    syncUrl();
    load();
  });
  $("trends-retry").addEventListener("click", () => load());
  // Charts follow the theme toggle without a reload: redraw (no animation) when <html data-theme> changes.
  new MutationObserver(() => renderAll(true)).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  // Crossing the narrow/wide threshold (rotating a phone, resizing) switches end labels on/off.
  let frame = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const flipped = CHARTS.some((d) => state.charts.has(d.key) && ($(`${d.key}-box`).clientWidth < NARROW_PX) !== state.charts.get(d.key).narrow);
      if (flipped) renderAll(true);
    });
  });
  load();
  // The station filter lists every active station, not only those with data in the range (one twin request per visit).
  getJson("/api/public/twin").then((twin) => fillStations(twin.stations)).catch(() => {});
}

init();
