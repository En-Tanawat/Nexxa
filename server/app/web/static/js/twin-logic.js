// Pure logic for the greenhouse twin (3D scene in twin.js, SVG schematic in twin-fallback.js).
// No DOM, no three.js here, so everything is unit-tested in Node (tests/js/twin_logic.test.mjs).
import { fmtDateTime, fmtInt, fmtPct, relativeTime, statusInfo } from "./public-lib.js";

// ---- sizes (world units, roughly metres) -----------------------------------------------------------
export const BED_W = 2.6; // planting bed length (x)
export const BED_D = 0.95; // planting bed width (z)
export const PITCH_X = 3.5; // centre-to-centre spacing used for the fallback grid
export const PITCH_Z = 2.3;
export const MIN_FLOOR_W = 10;
export const MIN_FLOOR_D = 7;
const MARGIN = 1.6; // floor beyond the outermost bed
export const PLANTS_PER_BED = 16; // 8 along the bed x 2 across
export const MAX_NAME = 120; // characters kept from a station / crop name
export const MAX_STATIONS = 200; // far above any real greenhouse; keeps a hostile payload from building a huge scene
export const HARVEST_DAYS = 45; // same assumed cycle the dashboard uses
export const MIN_SCALE = 0.22;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const coord = (v) => (isNum(v) && Math.abs(v) <= 1e12 ? v : null);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// ---- growth ------------------------------------------------------------------------------------------
/** age_days -> plant scale. 0 = nothing planted (no batch); a seedling starts at MIN_SCALE and eases to 1 at harvest. */
export function plantScale(age) {
  if (!isNum(age) || age < 0) return 0;
  const t = Math.min(1, age / HARVEST_DAYS);
  return MIN_SCALE + (1 - MIN_SCALE) * t * t * (3 - 2 * t); // smoothstep: slow start, fast middle, flat at harvest
}

/** Exponential ease of `cur` towards `target`; frame-rate independent, never overshoots, snaps when close. */
export function approach(cur, target, dt, rate) {
  const next = cur + (target - cur) * (1 - Math.exp(-rate * dt));
  return Math.abs(target - next) < 0.002 ? target : next;
}

// ---- status -> how it looks --------------------------------------------------------------------------
const ICON_FOR = { normal: "ok", yellow: "warn", brown: "danger", review: "eye", unknown: "neutral" };
const CIRCLE = "M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20Z";
/** Lucide-style 24px stroke icons (same drawings as the page's icon macros), as SVG path data. */
export const ICON_PATHS = {
  ok: [CIRCLE, "m9 12 2 2 4-4"],
  warn: ["m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3", "M12 9v4", "M12 17h.01"],
  danger: [CIRCLE, "M12 8v4", "M12 16h.01"],
  eye: ["M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0", "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6Z"],
  neutral: [CIRCLE, "M8 12h8"],
  home: ["M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8", "M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
};

/** status -> { status, label (Thai), icon id, mark token (icon/chip colour), plant token (foliage colour) }. */
export function statusVisual(status) {
  const key = typeof status === "string" && Object.hasOwn(ICON_FOR, status) ? status : "unknown";
  return { status: key, label: statusInfo(key).text, icon: ICON_FOR[key], mark: `--twin-mark-${key}`, plant: `--twin-plant-${key}` };
}

// ---- input hygiene ------------------------------------------------------------------------------------
/** Collapses whitespace and removes control / zero-width / bidi-override characters (they could reorder the label). */
export function cleanText(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/\s+/g, " ")
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g, "")
    .trim();
}

/** The twin payload, trusted for nothing: drops malformed / duplicate stations, coerces every field. */
export function normalizeData(data) {
  const raw = Array.isArray(data?.stations) ? data.stations : [];
  const seen = new Set();
  const stations = [];
  for (const s of raw) {
    if (stations.length >= MAX_STATIONS) break;
    if (!s || typeof s !== "object" || Array.isArray(s)) continue;
    const id = typeof s.id === "string" ? s.id.trim() : s.id;
    if (!(isNum(id) || (typeof id === "string" && id)) || seen.has(id)) continue;
    seen.add(id);
    stations.push({
      id,
      name: truncateLabel(typeof s.name === "string" || isNum(s.name) ? s.name : "", MAX_NAME) || `สถานี ${id}`, // a hostile 5 MB name must not reach the DOM
      crop: typeof s.crop === "string" && cleanText(s.crop) ? truncateLabel(s.crop, MAX_NAME) : null,
      map_x: coord(s.map_x),
      map_y: coord(s.map_y),
      route_order: isNum(s.route_order) ? s.route_order : null,
      status: statusVisual(s.status).status,
      age_days: isNum(s.age_days) && s.age_days >= 0 ? s.age_days : null,
      last_seen_at: typeof s.last_seen_at === "string" ? s.last_seen_at : null,
      confidence: isNum(s.confidence) && s.confidence >= 0 && s.confidence <= 1 ? s.confidence : null,
    });
  }
  const hx = coord(data?.home?.map_x);
  const hy = coord(data?.home?.map_y);
  return {
    stations,
    home: hx !== null && hy !== null ? { map_x: hx, map_y: hy } : null,
    updated_at: typeof data?.updated_at === "string" ? data.updated_at : null,
    demo: Boolean(data?.demo),
  };
}

// ---- layout ---------------------------------------------------------------------------------------------
const byRoute = (a, b) => {
  const ra = a.route_order ?? Number.POSITIVE_INFINITY;
  const rb = b.route_order ?? Number.POSITIVE_INFINITY;
  return ra - rb || String(a.id).localeCompare(String(b.id), "en", { numeric: true });
};

/** Snake through a grid (the robot's route runs there and back): returns [col,row] for the i-th of n. */
function snake(i, n, cols = Math.max(1, Math.ceil(Math.sqrt(n * 1.6)))) {
  const row = Math.floor(i / cols);
  const c = i % cols;
  return [row % 2 ? cols - 1 - c : c, row];
}

/**
 * Where every planting row stands. Stations with map_x/map_y keep their relative places (uniformly spread over
 * an area that fits the count, then nudged apart if they would overlap); stations without them queue in a strip in
 * front, in route order; with no coordinates at all everything snakes through a grid in route order.
 * -> { items: [{id, x, z, source: "map"|"route"}], width, depth, home: {x, z} | null }, centred on the origin.
 */
export function layoutStations(stations, home = null) {
  const list = Array.isArray(stations) ? stations : [];
  const mapped = list.filter((s) => coord(s.map_x) !== null && coord(s.map_y) !== null);
  const rest = list.filter((s) => !mapped.includes(s)).sort(byRoute);
  const hx = coord(home?.map_x);
  const hy = coord(home?.map_y);
  const homeMapped = hx !== null && hy !== null && mapped.length > 0;
  let items = [];
  let homePos = null;

  if (mapped.length) {
    const pts = mapped.map((s) => [s.map_x, s.map_y]);
    if (homeMapped) pts.push([hx, hy]);
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const [minX, minY] = [Math.min(...xs), Math.min(...ys)];
    const [spanX, spanY] = [Math.max(...xs) - minX, Math.max(...ys) - minY];
    const n = mapped.length;
    const area = Math.max(n, 2) * PITCH_X * PITCH_Z * 1.7;
    let W;
    let D;
    if (spanX > 0 && spanY > 0) {
      W = Math.sqrt(area * clamp(spanX / spanY, 0.5, 2.5));
      D = area / W;
    } else {
      W = spanX > 0 ? Math.max(PITCH_X * 1.5 * (n - 1), 6) : 0;
      D = spanY > 0 ? Math.max(PITCH_Z * 1.6 * (n - 1), 4) : 0;
    }
    const place = (px, py) => ({ x: spanX > 0 ? ((px - minX) / spanX) * W : 0, z: spanY > 0 ? ((py - minY) / spanY) * D : 0 });
    items = mapped.map((s) => ({ id: s.id, ...place(s.map_x, s.map_y), source: "map" }));
    if (homeMapped) homePos = place(hx, hy);

    // nudge overlapping rows apart (room for the floating label as well as the bed), front-to-back so it is stable
    const placed = [];
    for (const it of [...items].sort((a, b) => a.z - b.z || a.x - b.x)) {
      for (let guard = 0; guard < 4000 && placed.some((p) => Math.abs(p.x - it.x) < BED_W + 0.5 && Math.abs(p.z - it.z) < BED_D + 1); guard++) it.z += 0.4;
      placed.push(it);
    }
    if (rest.length) {
      const xs2 = items.map((i) => i.x);
      const x0 = Math.min(...xs2);
      const cols = clamp(Math.floor((Math.max(...xs2) - x0) / PITCH_X) + 1, 1, rest.length);
      const z0 = Math.max(...items.map((i) => i.z)) + PITCH_Z;
      rest.forEach((s, i) => {
        const [c, r] = snake(i, rest.length, cols);
        items.push({ id: s.id, x: x0 + c * PITCH_X, z: z0 + r * PITCH_Z, source: "route" });
      });
    }
  } else {
    rest.forEach((s, i) => {
      const [c, r] = snake(i, rest.length);
      items.push({ id: s.id, x: c * PITCH_X, z: r * PITCH_Z, source: "route" });
    });
  }

  const all = homePos ? [...items, homePos] : items;
  const [lo, hi] = all.length
    ? [[Math.min(...all.map((i) => i.x)), Math.min(...all.map((i) => i.z))], [Math.max(...all.map((i) => i.x)), Math.max(...all.map((i) => i.z))]]
    : [[0, 0], [0, 0]];
  const [cx, cz] = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2];
  for (const i of all) {
    i.x -= cx;
    i.z -= cz;
  }
  const width = Math.max(MIN_FLOOR_W, hi[0] - lo[0] + BED_W + 2 * MARGIN);
  const depth = Math.max(MIN_FLOOR_D, hi[1] - lo[1] + BED_D + 2 * MARGIN);
  // a home position that cannot be placed on the map waits by the "door" (front-left corner)
  if (!homePos && home && typeof home === "object") homePos = { x: -width / 2 + 1.2, z: depth / 2 - 1 };
  return { items, width, depth, home: homePos };
}

// small deterministic RNG so a station's plants never jump between refreshes
function seeded(id) {
  let h = 2166136261;
  for (const ch of String(id)) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return () => {
    h = (h + 0x6d2b79f5) | 0;
    let t = Math.imul(h ^ (h >>> 15), 1 | h);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Local plant positions inside one bed (x along the bed, z across), slightly jittered; size 0.88-1.08, rot in radians. */
export function plantSlots(id) {
  const rnd = seeded(id);
  const slots = [];
  const step = (BED_W - 0.5) / 7;
  for (let row = 0; row < 2; row++) {
    for (let i = 0; i < 8; i++) {
      slots.push({ x: -BED_W / 2 + 0.25 + i * step + (rnd() - 0.5) * 0.12, z: (row ? 0.2 : -0.2) + (rnd() - 0.5) * 0.1, size: 0.88 + rnd() * 0.2, rot: rnd() * Math.PI * 2 });
    }
  }
  return slots;
}

// ---- text -------------------------------------------------------------------------------------------------
const segmenter = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter("th", { granularity: "grapheme" }) : null;
const graphemes = (s) => (segmenter ? Array.from(segmenter.segment(s), (x) => x.segment) : Array.from(s));

/** Single-line label of at most `max` characters (grapheme clusters, so Thai tone marks stay on their letter). */
export function truncateLabel(text, max = 22) {
  const s = cleanText(text);
  const limit = Math.max(2, max);
  const g = graphemes(s.length > limit * 16 ? s.slice(0, limit * 16) : s); // never segment megabytes of hostile input
  if (g.length <= limit && s.length <= limit * 16) return s;
  const head = g.slice(0, limit - 1);
  while (head.length && /^[เแโใไ\s]$/.test(head[head.length - 1])) head.pop(); // a leading vowel with nothing after it
  return `${head.join("")}…`;
}

const ORDER = ["normal", "yellow", "brown", "review", "unknown"];
/** aria-label of the canvas / SVG: how many stations, how many in each state (zero counts are left out). */
export function ariaSummary(stations, { flat = false } = {}) {
  const head = `แผนผัง ${flat ? 2 : 3} มิติของโรงเรือน`;
  const list = Array.isArray(stations) ? stations : [];
  if (!list.length) return `${head}: ยังไม่มีสถานีให้แสดง`;
  const counts = Object.fromEntries(ORDER.map((k) => [k, 0]));
  for (const s of list) counts[statusVisual(s?.status).status] += 1;
  const parts = ORDER.filter((k) => counts[k]).map((k) => `${statusVisual(k).label} ${fmtInt(counts[k])}`);
  return `${head}: ${fmtInt(list.length)} สถานี ${parts.join(" ")} เลือกสถานีจากรายการใต้แผนผังเพื่อดูรายละเอียด`;
}

/** Detail panel content: { title, rows: [{label, value}] }; plain strings only (the caller uses textContent). */
export function stationDetail(s, now = Date.now()) {
  const seen = relativeTime(s.last_seen_at, now);
  return {
    title: s.name,
    rows: [
      { label: "ผัก", value: s.crop ?? "ไม่ระบุผัก" },
      { label: "อายุ", value: s.age_days == null ? "ยังไม่มีล็อตที่ปลูกอยู่" : `${fmtInt(s.age_days)} วัน` },
      { label: "สถานะ", value: statusVisual(s.status).label },
      { label: "ตรวจล่าสุด", value: Number.isNaN(Date.parse(s.last_seen_at)) ? seen : `${seen} (${fmtDateTime(s.last_seen_at)})` },
      { label: "ความมั่นใจ", value: s.confidence == null ? "–" : fmtPct(s.confidence, 0) },
    ],
  };
}

// ---- camera ---------------------------------------------------------------------------------------------------
export const FOV = 40;
/** Start view that fits the whole floor for a viewport of `aspect` (w/h), plus the orbit limits. */
export function framing(width, depth, aspect) {
  const a = isNum(aspect) && aspect > 0.2 ? aspect : 1.6;
  const r = Math.max(1.5, Math.hypot(isNum(width) ? width : 0, isNum(depth) ? depth : 0) / 2);
  const vHalf = (FOV / 2) * (Math.PI / 180);
  const hHalf = Math.atan(Math.tan(vHalf) * a);
  const distance = (r / Math.tan(Math.min(vHalf, hHalf))) * 0.92;
  const polar = 1.0; // from straight down (rad): about 57 degrees, a "from the front-corner, slightly above" view
  const az = 0.5;
  const target = [0, 0.7, 0];
  return {
    target,
    distance,
    position: [target[0] + distance * Math.sin(polar) * Math.sin(az), target[1] + distance * Math.cos(polar), target[2] + distance * Math.sin(polar) * Math.cos(az)],
    minDistance: distance * 0.3,
    maxDistance: distance * 2.4,
    maxPolar: 1.45, // < 90 degrees: the camera can never go under the floor
    far: distance * 12,
  };
}
