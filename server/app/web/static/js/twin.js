// 3D greenhouse twin for the public dashboard: mountTwin(root, getData, opts?) -> { update(data), dispose() }.
//   * three.js 0.185.1 is vendored (static/vendor/three-0.185.1) and loaded lazily with import(): nothing here is fetched from a third party,
//     an import map or 'unsafe-inline'. If WebGL is missing, fails, or loses its context, the SVG schematic from
//     twin-fallback.js takes over (same stations, same words) and a visible note says why.
//   * Pure decisions (growth curve, status look, layout, text) live in twin-logic.js and are tested in Node.
//   * Safety: API data (station names!) reaches the page with textContent only and the scene as canvas fillText.
//   * Cost model: render ON DEMAND. A frame is drawn only after something changed (data, theme, camera, size, animation) and only
//     while the scene is on screen and the tab is visible; an idle page renders nothing.
import {
  BED_D, BED_W, FOV, ICON_PATHS, PLANTS_PER_BED, approach, ariaSummary, framing, layoutStations, normalizeData, plantScale, plantSlots,
  stationDetail, statusVisual, truncateLabel,
} from "./twin-logic.js";
import { createFallback } from "./twin-fallback.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const COMPACT_ABOVE = 16; // more stations than this: scene labels are icon-only chips (full chip for the selected / hovered one)
const WALL_H = 2.4;
const ROOF_RISE = 0.9;
const NET_CELL = 0.22; // world units per net mesh cell: fine enough to read as insect net
const PLANT_Y = 0.3; // soil surface
const LABEL_Y = 0.85;
const LABEL_PX = 34; // on-screen height of a full scene label, in CSS pixels (constant at any zoom, so it stays legible at 375px)
const GROW_RATE = 4.5; // 1/s, growth animation speed
const FLY_MS = 450;

const T = {
  loading: "กำลังโหลดแผนผัง…",
  noWebgl: "อุปกรณ์หรือเบราว์เซอร์นี้แสดงภาพ 3 มิติไม่ได้ จึงแสดงแผนผัง 2 มิติแทน ข้อมูลเหมือนกันทุกอย่าง",
  noLib: "โหลดตัวแสดงภาพ 3 มิติไม่สำเร็จ จึงแสดงแผนผัง 2 มิติแทน ข้อมูลเหมือนกันทุกอย่าง",
  lost: "การแสดงภาพ 3 มิติหยุดชั่วคราว (หน่วยความจำกราฟิกถูกเรียกคืน) ระหว่างนี้แสดงแผนผัง 2 มิติแทน และจะกลับมาเองเมื่อพร้อม",
  loadFail: "โหลดข้อมูลแผนผังไม่สำเร็จ ลองรีเฟรชหน้านี้อีกครั้ง",
  emptyTitle: "ยังไม่มีสถานีในแผนผัง",
  emptyText: "เมื่อผู้ดูแลเพิ่มสถานีตรวจ แถวผักของแต่ละสถานีจะปรากฏในโรงเรือนนี้โดยอัตโนมัติ",
  listTitle: "เลือกสถานีเพื่อดูรายละเอียด",
  hint: "ลากเพื่อหมุนมุมมอง · ล้อเมาส์หรือหนีบสองนิ้วเพื่อซูม · แตะสถานีหรือป้ายเพื่อดูรายละเอียด",
  hint2d: "แตะสถานีหรือเลือกจากรายการด้านล่างเพื่อดูรายละเอียด",
  panel: "รายละเอียดสถานี",
  close: "ปิดรายละเอียดสถานี",
  tools: "ควบคุมมุมมองกล้อง",
  reset: "รีเซ็ตมุมมอง",
  zoomIn: "ซูมเข้า",
  zoomOut: "ซูมออก",
  rotate: "หมุนอัตโนมัติ",
  rotateOff: "หมุนอัตโนมัติ (ปิดอยู่เพราะตั้งค่าลดการเคลื่อนไหว)",
  home: "จุดจอด",
  homeLong: "จุดจอดหุ่นยนต์",
};

const TOOL_ICONS = {
  reset: ["M3 7V5a2 2 0 0 1 2-2h2", "M17 3h2a2 2 0 0 1 2 2v2", "M21 17v2a2 2 0 0 1-2 2h-2", "M7 21H5a2 2 0 0 1-2-2v-2"],
  zoomIn: ["M5 12h14", "M12 5v14"],
  zoomOut: ["M5 12h14"],
  rotate: ["M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8", "M21 3v5h-5", "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16", "M8 16H3v5"],
  close: ["M18 6 6 18", "m6 6 12 12"],
};

// Used only if a token is missing (no stylesheet): the light palette, so the scene is never black.
const FALLBACK = {
  "--twin-sky-top": "#CFE4D9", "--twin-sky-bottom": "#EDF5E9", "--twin-ground": "#D3E2CB", "--twin-floor": "#BFD1B8",
  "--twin-floor-line": "#9DB596", "--twin-soil": "#5B4636", "--twin-tray": "#8FA790", "--twin-frame": "#5E7867", "--twin-net": "#4F7A62",
  "--twin-light-sky": "#F5FBF6", "--twin-light-ground": "#A9BFA4", "--twin-sun": "#FFF1D6", "--twin-label-bg": "#FBFEFB",
  "--twin-label-fg": "#10241A", "--twin-select": "#0F5C2E", "--twin-home": "#1D4ED8",
  "--twin-mark-normal": "#15803D", "--twin-mark-yellow": "#946C00", "--twin-mark-brown": "#7A3B12", "--twin-mark-review": "#C2410C", "--twin-mark-unknown": "#5F7466",
  "--twin-plant-normal": "#6DBE4B", "--twin-plant-yellow": "#D8CC3C", "--twin-plant-brown": "#8E5E32", "--twin-plant-review": "#EE8F2C", "--twin-plant-unknown": "#AAB5AE",
};
const HEX = /^#[0-9a-f]{6}$/i;

let uid = 0;

// ---------------------------------------------------------------------------------------------------------------
// DOM shell (everything built with createElement / textContent)
// ---------------------------------------------------------------------------------------------------------------
function makeIcon(doc, paths, size = 18) {
  const svg = doc.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ class: "icon", width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.85, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" })) svg.setAttribute(k, String(v));
  for (const d of paths) {
    const p = doc.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

function make(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildShell(doc) {
  const id = `twin-${++uid}`;
  const shell = make(doc, "div", "twin");
  const stage = make(doc, "div", "twin__stage");
  const viewport = make(doc, "div", "twin__viewport");
  const loading = make(doc, "p", "twin__loading", T.loading);
  loading.setAttribute("role", "status");

  const tools = make(doc, "div", "twin__tools");
  tools.setAttribute("role", "group");
  tools.setAttribute("aria-label", T.tools);
  tools.hidden = true;
  const tool = (key, label, pressed) => {
    const b = make(doc, "button", "twin__tool");
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    if (pressed !== undefined) b.setAttribute("aria-pressed", String(pressed));
    b.append(makeIcon(doc, TOOL_ICONS[key], 20));
    tools.append(b);
    return b;
  };
  const btn = { reset: tool("reset", T.reset), zoomIn: tool("zoomIn", T.zoomIn), zoomOut: tool("zoomOut", T.zoomOut), rotate: tool("rotate", T.rotate, false) };

  const empty = make(doc, "div", "twin__empty");
  empty.hidden = true;
  empty.append(make(doc, "p", "twin__empty-title", T.emptyTitle), make(doc, "p", "twin__empty-text", T.emptyText));

  const panel = make(doc, "aside", "twin__panel");
  panel.hidden = true;
  panel.tabIndex = -1;
  panel.setAttribute("aria-labelledby", `${id}-title`);
  const head = make(doc, "div", "twin__panel-head");
  const title = make(doc, "h3", "twin__panel-title");
  title.id = `${id}-title`;
  const close = make(doc, "button", "twin__tool twin__tool--sm");
  close.type = "button";
  close.setAttribute("aria-label", T.close);
  close.title = T.close;
  close.append(makeIcon(doc, TOOL_ICONS.close, 18));
  head.append(title, close);
  const badge = make(doc, "p", "twin__panel-status");
  const facts = make(doc, "dl", "twin__facts");
  panel.append(head, badge, facts);

  stage.append(viewport, loading, tools, empty, panel);

  const note = make(doc, "p", "twin__note");
  note.setAttribute("role", "status");
  note.hidden = true;
  const hint = make(doc, "p", "twin__hint", T.hint);
  const listTitle = make(doc, "p", "twin__list-title", T.listTitle);
  listTitle.id = `${id}-list`;
  const list = make(doc, "ul", "twin__list");
  list.setAttribute("aria-labelledby", listTitle.id);
  shell.append(stage, note, hint, listTitle, list);
  return { shell, stage, viewport, loading, tools, btn, empty, panel, title, close, badge, facts, note, hint, listTitle, list };
}

// ---------------------------------------------------------------------------------------------------------------
// three.js helpers (procedural meshes: no model files)
// ---------------------------------------------------------------------------------------------------------------
/** One cos/romaine lettuce rosette (~170 triangles, vertex colours dark at the base to light at the tips). */
function lettuceGeometry(THREE) {
  const positions = [];
  const colors = [];
  const index = [];
  const LEAVES = 11;
  const ROWS = 5;
  const rings = [
    { len: 0.34, w: 0.15, a0: 0.5, a1: 1.15, lift: 0, tint: [0.86, 0.95, 0.8] },
    { len: 0.28, w: 0.13, a0: 0.3, a1: 0.85, lift: 0.03, tint: [0.97, 1, 0.84] },
    { len: 0.2, w: 0.1, a0: 0.1, a1: 0.45, lift: 0.07, tint: [1, 1, 0.74] },
  ];
  for (let l = 0; l < LEAVES; l++) {
    const ring = rings[l < 5 ? 0 : l < 9 ? 1 : 2];
    const phi = l * 2.399963; // golden angle: leaves never line up
    const dx = Math.cos(phi);
    const dz = Math.sin(phi);
    let r = 0.015;
    let y = ring.lift;
    const first = positions.length / 3;
    for (let row = 0; row <= ROWS; row++) {
      const t = row / ROWS;
      if (row > 0) {
        const alpha = ring.a0 + (ring.a1 - ring.a0) * t;
        r += Math.sin(alpha) * (ring.len / ROWS);
        y += Math.cos(alpha) * (ring.len / ROWS);
      }
      const w = ring.w * Math.pow(Math.sin(Math.PI * Math.min(0.97, t * 0.93 + 0.05)), 0.7);
      const shade = 0.5 + 0.5 * Math.pow(t, 0.8);
      for (const side of [-1, 0, 1]) {
        const lateral = (side * w) / 2;
        const cup = side === 0 ? 0 : 0.35 * w * (0.4 + t);
        positions.push(dx * r - dz * lateral, y + cup, dz * r + dx * lateral);
        const k = Math.min(1, shade + (side === 0 ? 0.08 : 0));
        colors.push(k * ring.tint[0], k * ring.tint[1], k * ring.tint[2]);
      }
    }
    for (let row = 0; row < ROWS; row++) {
      for (let s = 0; s < 2; s++) {
        const a = first + row * 3 + s;
        const b = a + 1;
        const c = a + 3;
        const d = c + 1;
        index.push(a, b, c, b, d, c);
      }
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  g.setIndex(index);
  g.computeVertexNormals();
  return g;
}

/** The whole insect-net envelope (4 walls, 2 gables, 2 roof panes) as ONE geometry: one draw call. uv = metres / cell. */
function netGeometry(THREE, W, D) {
  const pos = [];
  const uv = [];
  const idx = [];
  const vert = (x, y, z, u, v) => {
    pos.push(x, y, z);
    uv.push(u / NET_CELL, v / NET_CELL);
    return pos.length / 3 - 1;
  };
  const quad = (a, b, c, d) => idx.push(a, b, c, a, c, d);
  const hw = W / 2;
  const hd = D / 2;
  const H = WALL_H;
  const slant = Math.hypot(hd, ROOF_RISE);
  for (const z of [-hd, hd]) quad(vert(-hw, 0, z, 0, 0), vert(hw, 0, z, W, 0), vert(hw, H, z, W, H), vert(-hw, H, z, 0, H));
  for (const x of [-hw, hw]) {
    quad(vert(x, 0, -hd, 0, 0), vert(x, 0, hd, D, 0), vert(x, H, hd, D, H), vert(x, H, -hd, 0, H));
    const a = vert(x, H, -hd, 0, H);
    const b = vert(x, H, hd, D, H);
    const c = vert(x, H + ROOF_RISE, 0, D / 2, H + ROOF_RISE);
    idx.push(a, b, c);
  }
  for (const s of [-1, 1]) quad(vert(-hw, H, s * hd, 0, 0), vert(hw, H, s * hd, W, 0), vert(hw, H + ROOF_RISE, 0, W, slant), vert(-hw, H + ROOF_RISE, 0, 0, slant));
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  g.setIndex(idx);
  return g;
}

/** White net lines on a transparent cell (tinted by the material colour, so a theme change only re-colours the material). */
function netTexture(THREE, doc) {
  const c = doc.createElement("canvas");
  c.width = c.height = 32;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, 32, 32);
  ctx.fillStyle = "rgba(255,255,255,0.07)";
  ctx.fillRect(0, 0, 32, 32);
  ctx.fillStyle = "rgba(255,255,255,0.95)";
  ctx.fillRect(0, 0, 32, 2.5);
  ctx.fillRect(0, 0, 2.5, 32);
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.anisotropy = 4;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** Rounded selection frame (white, tinted by the material). */
function ringTexture(THREE, doc) {
  const c = doc.createElement("canvas");
  c.width = 256;
  c.height = 128;
  const ctx = c.getContext("2d");
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 9;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(8, 8, 240, 112, 30);
  else ctx.rect(8, 8, 240, 112);
  ctx.stroke();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** Greenhouse frame: posts, eave rails, ridge, rafters, gable king posts as instances of one unit box. */
function frameMembers(W, D) {
  const members = []; // { c: [x,y,z], s: [sx,sy,sz], to?: [x,y,z] (rafter end) }
  const hw = W / 2;
  const hd = D / 2;
  const nx = Math.max(2, Math.round(W / 3));
  const nz = Math.max(2, Math.round(D / 3));
  const post = 0.1;
  const H = WALL_H;
  for (let i = 0; i <= nx; i++) {
    const x = -hw + (i * W) / nx;
    for (const z of [-hd, hd]) members.push({ c: [x, H / 2, z], s: [post, H, post] });
    members.push({ c: [x, H + ROOF_RISE / 2, -hd / 2], s: [0.07, 0.07, Math.hypot(hd, ROOF_RISE)], from: [x, H, -hd], to: [x, H + ROOF_RISE, 0] });
    members.push({ c: [x, H + ROOF_RISE / 2, hd / 2], s: [0.07, 0.07, Math.hypot(hd, ROOF_RISE)], from: [x, H, hd], to: [x, H + ROOF_RISE, 0] });
  }
  for (let j = 1; j < nz; j++) {
    const z = -hd + (j * D) / nz;
    for (const x of [-hw, hw]) members.push({ c: [x, H / 2, z], s: [post, H, post] });
  }
  for (const z of [-hd, hd]) members.push({ c: [0, H, z], s: [W + post, 0.08, 0.08] });
  for (const x of [-hw, hw]) {
    members.push({ c: [x, H, 0], s: [0.08, 0.08, D + post] });
    members.push({ c: [x, H + ROOF_RISE / 2, 0], s: [post, ROOF_RISE, post] });
  }
  members.push({ c: [0, H + ROOF_RISE, 0], s: [W + post, 0.1, 0.1] });
  return members;
}

// ---------------------------------------------------------------------------------------------------------------
// mountTwin
// ---------------------------------------------------------------------------------------------------------------
/**
 * @param {HTMLElement} root       the box to fill (#twin-root)
 * @param {() => Promise<object>} getData  resolves to the /api/public/twin payload (called once; later data arrives via update())
 * @param {object} [opts]         test seams: loadThree() -> {THREE, OrbitControls}, createRenderer(THREE, canvas)
 */
export function mountTwin(root, getData, opts = {}) {
  const doc = root.ownerDocument;
  const win = doc.defaultView;
  const ui = buildShell(doc);
  root.replaceChildren(ui.shell);
  root.setAttribute("data-twin", "on");

  const reducedQuery = win.matchMedia?.("(prefers-reduced-motion: reduce)") ?? null;
  let reduced = Boolean(reducedQuery?.matches);

  let disposed = false;
  let mode = "loading"; // loading | 3d | 2d | lost
  let data = normalizeData(null);
  let layout = layoutStations([], null);
  let dataSig = "";
  let selectedId = null;
  let selectedOpener = null;
  let getDataToken = 0;
  let fallback = null;
  let view = null; // the 3D scene controller once it exists
  let ready;
  const chips = new Map(); // station id -> { li, button, mark, name, status }
  const stats = { frames: 0, drawCalls: 0, triangles: 0 };

  // ---- note / summary / list / panel (shared by the 3D and the 2D view) -----------------------------------------
  const setNote = (text) => {
    ui.note.textContent = text ?? "";
    ui.note.hidden = !text;
  };

  const surface = () => (mode === "3d" ? view?.canvas : fallback?.el);

  function syncSummary() {
    const el = surface();
    if (el) el.setAttribute("aria-label", ariaSummary(data.stations, { flat: mode !== "3d" }));
    ui.empty.hidden = data.stations.length > 0 || mode === "loading";
    ui.listTitle.hidden = ui.list.hidden = data.stations.length === 0;
    ui.hint.hidden = data.stations.length === 0 || mode === "loading";
    ui.hint.textContent = mode === "3d" ? T.hint : T.hint2d;
  }

  function markIcon(statusVisualInfo) {
    return makeIcon(doc, ICON_PATHS[statusVisualInfo.icon], 18);
  }

  function syncList() {
    const live = new Set();
    const ordered = [];
    for (const s of data.stations) {
      const key = String(s.id);
      live.add(key);
      const v = statusVisual(s.status);
      let c = chips.get(key);
      if (!c) {
        const li = make(doc, "li");
        const button = make(doc, "button", "twin__chip");
        button.type = "button";
        const mark = make(doc, "span", "twin__mark");
        const name = make(doc, "span", "twin__chip-name");
        const status = make(doc, "span", "twin__chip-status");
        button.append(mark, name, status);
        li.append(button);
        c = { li, button, mark, name, status, icon: null };
        button.addEventListener("click", () => select(s.id, button));
        chips.set(key, c);
      }
      c.button.dataset.status = v.status;
      if (c.icon !== v.icon) {
        c.mark.replaceChildren(markIcon(v));
        c.icon = v.icon;
      }
      c.name.textContent = s.name;
      c.status.textContent = v.label;
      c.button.setAttribute("aria-label", `${s.name} สถานะ ${v.label}`);
      c.button.title = s.name;
      if (String(s.id) === String(selectedId)) c.button.setAttribute("aria-current", "true");
      else c.button.removeAttribute("aria-current");
      ordered.push(c.li);
    }
    for (const [key, c] of chips) {
      if (!live.has(key)) {
        c.li.remove();
        chips.delete(key);
      }
    }
    const same = ui.list.children.length === ordered.length && ordered.every((li, i) => ui.list.children[i] === li);
    if (!same) ui.list.replaceChildren(...ordered); // only when the order really changed: keeps keyboard focus on a refresh
  }

  const stationById = (id) => data.stations.find((s) => String(s.id) === String(id)) ?? null;

  function fillPanel() {
    const s = stationById(selectedId);
    if (!s) return;
    const d = stationDetail(s, Date.now());
    const v = statusVisual(s.status);
    ui.title.textContent = d.title;
    ui.badge.dataset.status = v.status;
    const label = make(doc, "span", "twin__panel-status-text", v.label);
    ui.badge.replaceChildren(markIcon(v), label);
    ui.facts.replaceChildren(...d.rows.map((r) => {
      const row = make(doc, "div", "twin__fact");
      row.append(make(doc, "dt", "", r.label), make(doc, "dd", "", r.value));
      return row;
    }));
  }

  function select(id, opener) {
    if (!stationById(id)) return;
    selectedId = data.stations.find((s) => String(s.id) === String(id)).id;
    selectedOpener = opener ?? selectedOpener;
    ui.panel.hidden = false;
    fillPanel();
    syncList();
    view?.setSelected(selectedId);
    fallback?.update?.(fallbackState());
    ui.panel.focus({ preventScroll: true });
  }

  function closePanel(returnFocus = true) {
    if (selectedId === null) return;
    const opener = selectedOpener;
    selectedId = null;
    ui.panel.hidden = true;
    syncList();
    view?.setSelected(null);
    fallback?.update?.(fallbackState());
    if (returnFocus) (opener && opener.isConnected !== false ? opener : [...chips.values()][0]?.button)?.focus?.({ preventScroll: true });
  }
  const onClose = () => closePanel();
  ui.close.addEventListener("click", onClose);
  const onKey = (e) => {
    if (e.key === "Escape" && selectedId !== null) {
      e.stopPropagation();
      closePanel();
    }
  };
  root.addEventListener("keydown", onKey);

  const fallbackState = () => ({ stations: data.stations, layout, home: layout.home, selectedId });

  // ---- data ------------------------------------------------------------------------------------------------------
  function applyData(raw) {
    const next = normalizeData(raw);
    const sig = JSON.stringify(next);
    if (sig === dataSig) return; // identical refresh (the dashboard polls every minute): nothing to redraw
    dataSig = sig;
    data = next;
    layout = layoutStations(data.stations, data.home);
    if (selectedId !== null && !stationById(selectedId)) closePanel(false);
    syncList();
    if (selectedId !== null) fillPanel();
    view?.update();
    fallback?.update?.(fallbackState());
    syncSummary();
  }

  // ---- 2D fallback -------------------------------------------------------------------------------------------------
  function showFallback(message) {
    if (!fallback) {
      fallback = createFallback(doc, { onSelect: (id) => select(stationById(id)?.id ?? id) });
      ui.viewport.append(fallback.el);
    }
    fallback.el.hidden = false;
    if (view) view.canvas.hidden = true;
    ui.tools.hidden = true;
    ui.loading.hidden = true;
    fallback.update(fallbackState());
    setNote(message);
    syncSummary();
  }

  // ---- 3D scene --------------------------------------------------------------------------------------------------
  async function start3d() {
    let THREE;
    let OrbitControls;
    try {
      ({ THREE, OrbitControls } = await (opts.loadThree ? opts.loadThree() : loadVendored()));
    } catch {
      if (!disposed) {
        mode = "2d";
        showFallback(T.noLib);
      }
      return;
    }
    if (disposed) return;

    const canvas = doc.createElement("canvas");
    canvas.className = "twin__canvas";
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", ariaSummary(data.stations));
    let renderer;
    try {
      renderer = opts.createRenderer ? opts.createRenderer(THREE, canvas) : new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      mode = "2d";
      showFallback(T.noWebgl);
      return;
    }
    try {
      view = createView(THREE, OrbitControls, renderer, canvas);
    } catch (err) {
      renderer.dispose();
      renderer.forceContextLoss?.();
      view = null;
      mode = "2d";
      showFallback(T.noWebgl);
      return;
    }
    ui.viewport.append(canvas);
    mode = "3d";
    ui.loading.hidden = true;
    ui.tools.hidden = false;
    view.update();
    view.start();
    syncSummary();
  }

  function createView(THREE, OrbitControls, renderer, canvas) {
    const tracked = { geometries: new Set(), materials: new Set(), textures: new Set() };
    const own = (kind, obj) => {
      tracked[kind].add(obj);
      return obj;
    };
    const dispose3d = () => {
      for (const geometry of tracked.geometries) geometry.dispose();
      for (const material of tracked.materials) material.dispose();
      for (const texture of tracked.textures) texture.dispose();
      tracked.geometries.clear();
      tracked.materials.clear();
      tracked.textures.clear();
    };

    renderer.setPixelRatio(Math.min(win.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);
    if (renderer.shadowMap) {
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    }

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(FOV, 1.6, 0.1, 200);
    const hemi = new THREE.HemisphereLight(0xffffff, 0xffffff, 1);
    const sun = new THREE.DirectionalLight(0xffffff, 2);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.03;
    sun.shadow.radius = 3;
    scene.add(hemi, sun, sun.target);
    scene.fog = new THREE.Fog(0xffffff, 10, 60);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = !reduced;
    controls.dampingFactor = 0.09;
    controls.screenSpacePanning = false;
    controls.rotateSpeed = 0.7;
    controls.autoRotateSpeed = 0.7;
    controls.maxPolarAngle = 1.45; // never below the floor
    controls.minPolarAngle = 0.15;
    if (win.matchMedia?.("(pointer: coarse)").matches) canvas.style.touchAction = "pan-y"; // a vertical swipe on the scene still scrolls the page

    // state
    let size = { w: 0, h: 0 };
    let visible = typeof win.IntersectionObserver !== "function";
    let docVisible = !doc.hidden;
    let dirty = true;
    let raf = 0;
    let inFrame = false;
    let lastTs = 0;
    let world = null;
    let worldSig = "";
    let palette = null;
    let paletteSig = "";
    let themeEpoch = 0;
    let home = null; // the framing for the current layout and aspect
    let framed = false;
    let hovered = -1;
    let fly = null;
    let pointerDown = null;
    let hoverQueued = false;
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const dummy = new THREE.Object3D();
    const tmpColor = new THREE.Color();
    const tmpVec = new THREE.Vector3();

    // ---- palette --------------------------------------------------------------------------------------------------
    function readPalette() {
      const cs = win.getComputedStyle(root);
      const raw = {};
      for (const [name, fb] of Object.entries(FALLBACK)) {
        const v = cs.getPropertyValue(name).trim();
        raw[name] = HEX.test(v) ? v : fb;
      }
      raw.font = cs.fontFamily || "sans-serif";
      return raw;
    }
    const col = (name) => new THREE.Color(palette[name]);

    // ---- label sprites -------------------------------------------------------------------------------------------------
    /** The label's canvas texture for a given pixel width. A texture cannot change size in place, so a new one replaces it. */
    function labelCanvas(label, width) {
      if (label.canvas && label.canvas.width === width) return label.canvas;
      if (label.tex) {
        label.tex.dispose();
        tracked.textures.delete(label.tex);
      }
      label.canvas = doc.createElement("canvas");
      label.canvas.width = width;
      label.canvas.height = 128;
      label.tex = own("textures", new THREE.CanvasTexture(label.canvas));
      label.tex.colorSpace = THREE.SRGBColorSpace;
      label.tex.anisotropy = 4;
      label.sprite.material.map = label.tex;
      label.sprite.material.needsUpdate = true;
      return label.canvas;
    }

    function roundedRect(ctx, x, y, w, h, r) {
      if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
      else ctx.rect(x, y, w, h);
    }

    function drawLabel(label, { name, status, compact, selected, hover }) {
      const full = !compact || selected || hover;
      const c = labelCanvas(label, full ? 512 : 128);
      const ctx = c.getContext("2d");
      const v = statusVisual(status);
      const mark = palette[v.mark];
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      if (full) roundedRect(ctx, 6, 6, c.width - 12, c.height - 12, 30);
      else ctx.arc(64, 64, 58, 0, Math.PI * 2);
      ctx.fillStyle = palette["--twin-label-bg"];
      ctx.fill();
      ctx.lineWidth = selected ? 9 : 5;
      ctx.strokeStyle = selected ? palette["--twin-select"] : mark;
      ctx.stroke();
      ctx.save();
      const iconSize = full ? 64 : 62;
      ctx.translate(full ? 26 : 64 - iconSize / 2, full ? 32 : 64 - iconSize / 2);
      ctx.scale(iconSize / 24, iconSize / 24);
      ctx.lineWidth = 1.9;
      ctx.strokeStyle = mark;
      for (const d of ICON_PATHS[v.icon]) ctx.stroke(new win.Path2D(d));
      ctx.restore();
      if (full) {
        ctx.fillStyle = palette["--twin-label-fg"];
        ctx.textBaseline = "alphabetic";
        const maxW = c.width - 108 - 28;
        let count = 22;
        let text = truncateLabel(name, count);
        ctx.font = `700 40px ${palette.font}`;
        while (ctx.measureText(text).width > maxW && count > 3) text = truncateLabel(name, --count);
        ctx.fillText(text, 104, 58);
        ctx.font = `600 31px ${palette.font}`;
        ctx.fillText(v.label, 104, 102);
      }
      label.tex.needsUpdate = true;
      label.full = full;
    }

    function makeLabel(index) {
      const mat = own("materials", new THREE.SpriteMaterial({ transparent: true, depthTest: false, depthWrite: false, fog: false }));
      const sprite = new THREE.Sprite(mat);
      sprite.center.set(0.5, 0);
      sprite.renderOrder = 20;
      sprite.userData = { index };
      return { canvas: null, tex: null, sprite, sig: "", full: true };
    }

    function refreshLabel(label, s, index, force) {
      const compact = data.stations.length > COMPACT_ABOVE;
      const selected = String(s.id) === String(selectedId);
      const hover = hovered === index;
      const sig = `${themeEpoch}|${compact}|${selected}|${hover}|${s.name}|${s.status}`;
      if (!force && label.sig === sig) return;
      label.sig = sig;
      drawLabel(label, { name: s.name, status: s.status, compact, selected, hover });
    }

    // ---- world (rebuilt only when the layout changes) --------------------------------------------------------------
    const grown = new Map();
    function disposeWorld() {
      if (!world) return;
      world.items.forEach((it, i) => grown.set(String(it.id), world.cur[i]));
      scene.remove(world.group);
      for (const mesh of world.instanced) mesh.dispose();
      dispose3d();
      world = null;
    }

    function buildWorld() {
      disposeWorld();
      const { width: W, depth: D } = layout;
      const n = layout.items.length;
      const group = new THREE.Group();
      const instanced = [];
      const w = { group, instanced, items: layout.items, labels: [], cur: new Float32Array(n), tgt: new Float32Array(n), W, D };
      layout.items.forEach((it, i) => {
        w.cur[i] = grown.get(String(it.id)) ?? (reduced || opts.instantGrowth ? 1 : 0); // plants that already exist keep their size
      });

      // ground disc + floor slab + floor grid
      const ground = new THREE.Mesh(own("geometries", new THREE.CircleGeometry(Math.max(W, D) * 6, 48)), own("materials", new THREE.MeshStandardMaterial({ roughness: 1 })));
      ground.rotation.x = -Math.PI / 2;
      ground.position.y = -0.14;
      ground.receiveShadow = true;
      w.ground = ground;
      const floor = new THREE.Mesh(own("geometries", new THREE.BoxGeometry(W, 0.12, D)), own("materials", new THREE.MeshStandardMaterial({ roughness: 0.95 })));
      floor.position.y = -0.06;
      floor.receiveShadow = true;
      w.floor = floor;
      const lines = [];
      for (let x = Math.ceil(-W / 2) + 1; x < W / 2; x++) lines.push(x, 0.004, -D / 2, x, 0.004, D / 2);
      for (let z = Math.ceil(-D / 2) + 1; z < D / 2; z++) lines.push(-W / 2, 0.004, z, W / 2, 0.004, z);
      const gridGeo = own("geometries", new THREE.BufferGeometry());
      gridGeo.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
      w.grid = new THREE.LineSegments(gridGeo, own("materials", new THREE.LineBasicMaterial({ transparent: true, opacity: 0.55 })));

      // frame (instanced unit boxes)
      const members = frameMembers(W, D);
      const frame = new THREE.InstancedMesh(own("geometries", new THREE.BoxGeometry(1, 1, 1)), own("materials", new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.25 })), members.length);
      members.forEach((m, i) => {
        dummy.position.set(...m.c);
        dummy.rotation.set(0, 0, 0);
        dummy.scale.set(...m.s);
        if (m.from) {
          dummy.position.set(...m.c);
          dummy.lookAt(tmpVec.set(...m.to));
          dummy.scale.set(...m.s);
        }
        dummy.updateMatrix();
        frame.setMatrixAt(i, dummy.matrix);
      });
      frame.castShadow = true;
      frame.receiveShadow = true;
      instanced.push(frame);
      w.frame = frame;

      // net
      const netTex = own("textures", netTexture(THREE, doc));
      const net = new THREE.Mesh(own("geometries", netGeometry(THREE, W, D)), own("materials", new THREE.MeshBasicMaterial({ map: netTex, transparent: true, side: THREE.DoubleSide, depthWrite: false })));
      net.renderOrder = 5;
      w.net = net;

      // beds: tray + soil + plants (three instanced meshes for every station together)
      const trayGeo = own("geometries", new THREE.BoxGeometry(BED_W, 0.26, BED_D));
      const soilGeo = own("geometries", new THREE.BoxGeometry(BED_W - 0.14, 0.05, BED_D - 0.14));
      w.trays = new THREE.InstancedMesh(trayGeo, own("materials", new THREE.MeshStandardMaterial({ roughness: 0.7 })), Math.max(1, n));
      w.soil = new THREE.InstancedMesh(soilGeo, own("materials", new THREE.MeshStandardMaterial({ roughness: 1 })), Math.max(1, n));
      w.plants = new THREE.InstancedMesh(own("geometries", lettuceGeometry(THREE)), own("materials", new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.82, side: THREE.DoubleSide })), Math.max(1, n * PLANTS_PER_BED));
      w.trays.count = w.soil.count = n;
      w.plants.count = n * PLANTS_PER_BED;
      w.trays.castShadow = w.trays.receiveShadow = w.soil.receiveShadow = true;
      w.plants.castShadow = true;
      w.plants.receiveShadow = true;
      w.slots = layout.items.map((it) => plantSlots(it.id));
      layout.items.forEach((it, i) => {
        dummy.position.set(it.x, 0.13, it.z);
        dummy.rotation.set(0, 0, 0);
        dummy.scale.set(1, 1, 1);
        dummy.updateMatrix();
        w.trays.setMatrixAt(i, dummy.matrix);
        dummy.position.y = 0.285;
        dummy.updateMatrix();
        w.soil.setMatrixAt(i, dummy.matrix);
      });
      instanced.push(w.trays, w.soil, w.plants);

      // selection ring + labels + home
      const ringTex = own("textures", ringTexture(THREE, doc));
      w.ring = new THREE.Mesh(own("geometries", new THREE.PlaneGeometry(BED_W + 0.7, BED_D + 0.55)), own("materials", new THREE.MeshBasicMaterial({ map: ringTex, transparent: true, depthWrite: false })));
      w.ring.rotation.x = -Math.PI / 2;
      w.ring.position.y = 0.012;
      w.ring.visible = false;
      w.ring.renderOrder = 4;
      group.add(ground, floor, w.grid, frame, net, w.trays, w.soil, w.plants, w.ring);
      layout.items.forEach((it, i) => {
        const label = makeLabel(i);
        label.sprite.position.set(it.x, LABEL_Y, it.z);
        w.labels.push(label);
        group.add(label.sprite);
      });
      if (layout.home) {
        const disc = new THREE.Mesh(own("geometries", new THREE.CircleGeometry(0.5, 32)), own("materials", new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.85 })));
        disc.rotation.x = -Math.PI / 2;
        disc.position.set(layout.home.x, 0.014, layout.home.z);
        w.homeDisc = disc;
        const label = makeLabel(-1);
        label.sprite.position.set(layout.home.x, 0.55, layout.home.z);
        w.homeLabel = label;
        group.add(disc, label.sprite);
      }
      scene.add(group);
      world = w;

      // camera limits + shadow frustum for this floor
      home = framing(W, D, size.h ? size.w / size.h : 1.6);
      controls.minDistance = home.minDistance;
      controls.maxDistance = home.maxDistance;
      camera.far = home.far;
      camera.updateProjectionMatrix();
      const span = Math.max(W, D) * 0.75;
      Object.assign(sun.shadow.camera, { left: -span, right: span, top: span, bottom: -span, near: 1, far: span * 6 });
      sun.shadow.camera.updateProjectionMatrix();
      sun.position.set(W * 0.45, span * 1.6, D * 0.35);
      sun.target.position.set(0, 0, 0);
      scene.fog.near = home.distance * 0.9;
      scene.fog.far = home.distance * 3.4;
      themeEpoch++;
      paletteSig = "";
      applyTheme();
      syncStations(true);
      if (!framed && size.w) frameCamera(true);
      return w;
    }

    function applyTheme() {
      const next = readPalette();
      const sig = Object.values(next).join("|");
      if (sig === paletteSig || !world) return;
      paletteSig = sig;
      palette = next;
      themeEpoch++;
      const skyTop = col("--twin-sky-top");
      const dark = skyTop.r * 0.2126 + skyTop.g * 0.7152 + skyTop.b * 0.0722 < 0.06;
      hemi.color.copy(col("--twin-light-sky"));
      hemi.groundColor.copy(col("--twin-light-ground"));
      hemi.intensity = dark ? 1.15 : 1.55;
      sun.color.copy(col("--twin-sun"));
      sun.intensity = dark ? 1.5 : 2.5;
      scene.fog.color.copy(col("--twin-sky-bottom"));
      world.ground.material.color.copy(col("--twin-ground"));
      world.floor.material.color.copy(col("--twin-floor"));
      world.grid.material.color.copy(col("--twin-floor-line"));
      world.frame.material.color.copy(col("--twin-frame"));
      world.net.material.color.copy(col("--twin-net"));
      world.net.material.opacity = dark ? 0.45 : 0.6;
      world.soil.material.color.copy(col("--twin-soil"));
      world.trays.material.color.set(0xffffff);
      world.plants.material.color.set(0xffffff);
      world.ring.material.color.copy(col("--twin-select"));
      if (world.homeDisc) world.homeDisc.material.color.copy(col("--twin-home"));
      recolor();
      world.labels.forEach((label, i) => {
        const s = data.stations.find((x) => String(x.id) === String(world.items[i].id));
        if (s) refreshLabel(label, s, i, true);
      });
      if (world.homeLabel) drawHomeLabel(world.homeLabel);
      requestRender();
    }

    function drawHomeLabel(label) {
      const c = labelCanvas(label, 384);
      const ctx = c.getContext("2d");
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.beginPath();
      roundedRect(ctx, 6, 6, c.width - 12, c.height - 12, 30);
      ctx.fillStyle = palette["--twin-label-bg"];
      ctx.fill();
      ctx.lineWidth = 5;
      ctx.strokeStyle = palette["--twin-home"];
      ctx.stroke();
      ctx.save();
      ctx.translate(26, 32);
      ctx.scale(64 / 24, 64 / 24);
      ctx.lineWidth = 1.9;
      ctx.lineCap = ctx.lineJoin = "round";
      ctx.strokeStyle = palette["--twin-home"];
      for (const d of ICON_PATHS.home) ctx.stroke(new win.Path2D(d));
      ctx.restore();
      ctx.fillStyle = palette["--twin-label-fg"];
      ctx.font = `700 40px ${palette.font}`;
      ctx.fillText(T.home, 104, 80);
      label.tex.needsUpdate = true;
    }

    /** Instance colours (trays, plants) from the palette + selection / hover state. */
    function recolor() {
      if (!world) return;
      const tray = col("--twin-tray");
      const select = col("--twin-select");
      world.items.forEach((it, i) => {
        const s = data.stations.find((x) => String(x.id) === String(it.id));
        const state = String(it.id) === String(selectedId) ? 0.55 : hovered === i ? 0.3 : 0;
        world.trays.setColorAt(i, tmpColor.copy(tray).lerp(select, state));
        const base = col(statusVisual(s?.status).plant);
        const slots = world.slots[i];
        for (let k = 0; k < PLANTS_PER_BED; k++) world.plants.setColorAt(i * PLANTS_PER_BED + k, tmpColor.copy(base).multiplyScalar(0.9 + (slots[k].size - 0.88) * 0.7));
      });
      for (const m of [world.trays, world.plants]) if (m.instanceColor) m.instanceColor.needsUpdate = true;
      const sel = world.items.findIndex((it) => String(it.id) === String(selectedId));
      world.ring.visible = sel >= 0;
      if (sel >= 0) world.ring.position.set(world.items[sel].x, 0.012, world.items[sel].z);
    }

    function writePlants() {
      world.items.forEach((it, i) => {
        const slots = world.slots[i];
        const s = world.cur[i];
        for (let k = 0; k < PLANTS_PER_BED; k++) {
          const sl = slots[k];
          dummy.position.set(it.x + sl.x, PLANT_Y, it.z + sl.z);
          dummy.rotation.set(0, sl.rot, 0);
          dummy.scale.setScalar(Math.max(1e-4, s * sl.size));
          dummy.updateMatrix();
          world.plants.setMatrixAt(i * PLANTS_PER_BED + k, dummy.matrix);
        }
      });
      world.plants.instanceMatrix.needsUpdate = true;
      for (const m of [world.plants, world.trays]) {
        m.boundingSphere = null; // picking must see the new size
        m.boundingBox = null;
      }
    }

    /** Data -> targets, colours, labels. Cheap; called for every data change. */
    function syncStations(first) {
      if (!world) return;
      world.items.forEach((it, i) => {
        const s = data.stations.find((x) => String(x.id) === String(it.id));
        world.tgt[i] = plantScale(s?.age_days);
        if (reduced || opts.instantGrowth) world.cur[i] = world.tgt[i];
        if (s) refreshLabel(world.labels[i], s, i, first);
      });
      recolor();
      writePlants();
      requestRender();
    }

    // ---- sizing / framing / camera --------------------------------------------------------------------------------------
    function frameCamera(instant) {
      if (!world || !size.w) return;
      home = framing(world.W, world.D, size.w / size.h);
      controls.minDistance = home.minDistance;
      controls.maxDistance = home.maxDistance;
      camera.far = home.far;
      camera.updateProjectionMatrix();
      framed = true;
      flyTo(home.position, home.target, instant);
    }

    function flyTo(position, target, instant) {
      const to = { p: new THREE.Vector3(...position), t: new THREE.Vector3(...target) };
      if (instant || reduced) {
        fly = null;
        camera.position.copy(to.p);
        controls.target.copy(to.t);
        controls.update();
      } else {
        fly = { t0: 0, from: { p: camera.position.clone(), t: controls.target.clone() }, to };
      }
      requestRender();
    }

    function zoomBy(factor) {
      const target = controls.target.clone();
      const offset = camera.position.clone().sub(target);
      const len = Math.min(controls.maxDistance, Math.max(controls.minDistance, offset.length() * factor));
      const p = target.clone().add(offset.setLength(len));
      flyTo(p.toArray(), target.toArray(), false);
    }

    function resize() {
      const w = ui.stage.clientWidth;
      const h = ui.stage.clientHeight;
      if (!w || !h) {
        size = { w: 0, h: 0 };
        return;
      }
      const first = !size.w;
      size = { w, h };
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      if (first && !framed) frameCamera(true);
      requestRender();
    }

    // ---- render loop (on demand) ----------------------------------------------------------------------------------------------
    const active = () => !disposed && mode === "3d" && visible && docVisible && size.w > 0 && Boolean(world);

    function requestRender() {
      dirty = true;
      if (!raf && !inFrame && active()) raf = win.requestAnimationFrame(frame);
    }

    function updateLabelScales() {
      const perPx = (2 * Math.tan((FOV * Math.PI) / 360)) / size.h;
      const place = (label, wPx, hPx) => {
        const dist = camera.position.distanceTo(label.sprite.position);
        label.sprite.scale.set(wPx * perPx * dist, hPx * perPx * dist, 1);
      };
      world.labels.forEach((label, i) => (label.full ? place(label, LABEL_PX * 4, LABEL_PX) : place(label, LABEL_PX * 0.86, LABEL_PX * 0.86)));
      if (world.homeLabel) place(world.homeLabel, LABEL_PX * 3, LABEL_PX);
    }

    function frame(ts) {
      raf = 0;
      if (!active()) return;
      const dt = Math.min(0.1, lastTs ? (ts - lastTs) / 1000 : 1 / 60);
      lastTs = ts;
      inFrame = true;
      let again = false;
      // growth animation
      let grew = false;
      for (let i = 0; i < world.cur.length; i++) {
        if (world.cur[i] !== world.tgt[i]) {
          world.cur[i] = approach(world.cur[i], world.tgt[i], dt, GROW_RATE);
          grew = true;
        }
      }
      if (grew) {
        writePlants();
        again = true;
      }
      // camera fly-to
      if (fly) {
        fly.t0 ||= ts;
        const p = Math.min(1, (ts - fly.t0) / FLY_MS);
        const e = p * p * (3 - 2 * p);
        camera.position.lerpVectors(fly.from.p, fly.to.p, e);
        controls.target.lerpVectors(fly.from.t, fly.to.t, e);
        if (p >= 1) fly = null;
        again = true;
      }
      if (controls.update(dt)) again = true;
      clampTarget();
      if (dirty || again) {
        dirty = false;
        updateLabelScales();
        renderer.render(scene, camera);
        stats.frames++;
        if (renderer.info) {
          stats.drawCalls = renderer.info.render.calls;
          stats.triangles = renderer.info.render.triangles;
        }
      }
      inFrame = false;
      if (again || dirty) raf = win.requestAnimationFrame(frame); // otherwise the loop stops here until something changes
      else lastTs = 0;
    }

    function clampTarget() {
      const t = controls.target;
      t.x = Math.min(world.W / 2, Math.max(-world.W / 2, t.x));
      t.z = Math.min(world.D / 2, Math.max(-world.D / 2, t.z));
      t.y = Math.min(2, Math.max(0, t.y));
    }

    // ---- picking ---------------------------------------------------------------------------------------------------------------
    function pick(e) {
      if (!world) return -1;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return -1;
      ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -(((e.clientY - rect.top) / rect.height) * 2 - 1));
      raycaster.setFromCamera(ndc, camera);
      const hitLabel = raycaster.intersectObjects(world.labels.map((l) => l.sprite), false)[0];
      if (hitLabel) return hitLabel.object.userData.index;
      const hit = raycaster.intersectObjects([world.trays, world.plants], false)[0];
      if (!hit || hit.instanceId === undefined) return -1;
      return hit.object === world.plants ? Math.floor(hit.instanceId / PLANTS_PER_BED) : hit.instanceId;
    }

    const onPointerDown = (e) => {
      pointerDown = { x: e.clientX, y: e.clientY, id: e.pointerId };
    };
    const onPointerUp = (e) => {
      const d = pointerDown;
      pointerDown = null;
      if (!d || d.id !== e.pointerId || e.button > 0 || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) return; // a drag is a rotation, not a click
      const i = pick(e);
      if (i >= 0 && world.items[i]) select(world.items[i].id, chips.get(String(world.items[i].id))?.button);
    };
    const onPointerMove = (e) => {
      if (e.pointerType !== "mouse" || e.buttons || hoverQueued) return;
      hoverQueued = true;
      win.requestAnimationFrame(() => {
        hoverQueued = false;
        if (disposed || !world) return;
        const i = pick(e);
        canvas.classList.toggle("is-pick", i >= 0);
        if (i !== hovered) {
          const was = hovered;
          hovered = i;
          for (const j of [was, i]) {
            const s = j >= 0 && data.stations.find((x) => String(x.id) === String(world.items[j]?.id));
            if (s) refreshLabel(world.labels[j], s, j, false);
          }
          recolor();
          requestRender();
        }
      });
    };
    const onPointerLeave = () => {
      if (hovered < 0 || !world) return;
      const was = hovered;
      hovered = -1;
      canvas.classList.remove("is-pick");
      const s = data.stations.find((x) => String(x.id) === String(world.items[was]?.id));
      if (s) refreshLabel(world.labels[was], s, was, false);
      recolor();
      requestRender();
    };

    // ---- listeners / observers -------------------------------------------------------------------------------------------------
    const onContextLost = (e) => {
      e.preventDefault(); // lets the browser restore the context later
      mode = "lost";
      if (raf) win.cancelAnimationFrame(raf);
      raf = 0;
      canvas.hidden = true;
      showFallback(T.lost);
    };
    const onContextRestored = () => {
      if (disposed) return;
      mode = "3d";
      canvas.hidden = false;
      if (fallback) fallback.el.hidden = true;
      ui.tools.hidden = false;
      setNote(null);
      paletteSig = "";
      applyTheme();
      syncSummary();
      requestRender();
    };
    canvas.addEventListener("webglcontextlost", onContextLost);
    canvas.addEventListener("webglcontextrestored", onContextRestored);
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerleave", onPointerLeave);
    controls.addEventListener("change", requestRender);

    const onVisibility = () => {
      docVisible = !doc.hidden;
      if (docVisible) requestRender();
      else if (raf) {
        win.cancelAnimationFrame(raf);
        raf = 0;
      }
    };
    doc.addEventListener("visibilitychange", onVisibility);

    const resizeObserver = typeof win.ResizeObserver === "function" ? new win.ResizeObserver(resize) : null;
    resizeObserver?.observe(ui.stage);
    const intersectionObserver = typeof win.IntersectionObserver === "function"
      ? new win.IntersectionObserver((entries) => {
        visible = entries[entries.length - 1].isIntersecting; // off screen: the loop stops by itself
        if (visible) requestRender();
        else if (raf) {
          win.cancelAnimationFrame(raf);
          raf = 0;
        }
      })
      : null;
    intersectionObserver?.observe(ui.stage);
    // The theme toggle only flips <html data-theme>: re-read the tokens, no reload.
    const themeObserver = typeof win.MutationObserver === "function" ? new win.MutationObserver(() => applyTheme()) : null;
    themeObserver?.observe(doc.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    const onReducedChange = () => {
      reduced = Boolean(reducedQuery.matches);
      controls.enableDamping = !reduced;
      if (reduced) {
        controls.autoRotate = false;
        ui.btn.rotate.setAttribute("aria-pressed", "false");
      }
      ui.btn.rotate.disabled = reduced;
      ui.btn.rotate.title = reduced ? T.rotateOff : T.rotate;
      if (reduced && world) {
        world.cur.set(world.tgt);
        writePlants();
        fly = null;
      }
      requestRender();
    };
    reducedQuery?.addEventListener?.("change", onReducedChange);
    if (reduced) {
      ui.btn.rotate.disabled = true;
      ui.btn.rotate.title = T.rotateOff;
    }

    const onReset = () => frameCamera(false);
    const onZoomIn = () => zoomBy(0.78);
    const onZoomOut = () => zoomBy(1.28);
    const onRotate = () => {
      controls.autoRotate = !controls.autoRotate && !reduced;
      ui.btn.rotate.setAttribute("aria-pressed", String(controls.autoRotate));
      requestRender();
    };
    ui.btn.reset.addEventListener("click", onReset);
    ui.btn.zoomIn.addEventListener("click", onZoomIn);
    ui.btn.zoomOut.addEventListener("click", onZoomOut);
    ui.btn.rotate.addEventListener("click", onRotate);

    return {
      canvas,
      update() {
        const sig = JSON.stringify([layout.width, layout.depth, layout.items.map((i) => [i.id, +i.x.toFixed(3), +i.z.toFixed(3)]), layout.home]);
        if (sig !== worldSig || !world) {
          worldSig = sig;
          buildWorld();
        } else {
          syncStations(false);
        }
        requestRender();
      },
      setSelected() {
        if (!world) return;
        world.labels.forEach((label, i) => {
          const s = data.stations.find((x) => String(x.id) === String(world.items[i].id));
          if (s) refreshLabel(label, s, i, false);
        });
        recolor();
        requestRender();
      },
      start() {
        resize();
        requestRender();
      },
      info: () => ({ calls: renderer.info?.render.calls ?? 0, triangles: renderer.info?.render.triangles ?? 0 }),
      dispose() {
        if (raf) win.cancelAnimationFrame(raf);
        raf = 0;
        resizeObserver?.disconnect();
        intersectionObserver?.disconnect();
        themeObserver?.disconnect();
        doc.removeEventListener("visibilitychange", onVisibility);
        reducedQuery?.removeEventListener?.("change", onReducedChange);
        canvas.removeEventListener("webglcontextlost", onContextLost); // before forceContextLoss: that fires the event
        canvas.removeEventListener("webglcontextrestored", onContextRestored);
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerleave", onPointerLeave);
        controls.removeEventListener("change", requestRender);
        ui.btn.reset.removeEventListener("click", onReset);
        ui.btn.zoomIn.removeEventListener("click", onZoomIn);
        ui.btn.zoomOut.removeEventListener("click", onZoomOut);
        ui.btn.rotate.removeEventListener("click", onRotate);
        controls.dispose();
        if (world) for (const mesh of world.instanced) mesh.dispose();
        dispose3d();
        scene.clear();
        renderer.dispose();
        renderer.forceContextLoss?.(); // browsers allow only ~16 live WebGL contexts: give this one back now
        canvas.remove();
        world = null;
      },
    };
  }

  // ---- go --------------------------------------------------------------------------------------------------------------
  const token = ++getDataToken;
  const firstData = Promise.resolve()
    .then(() => getData?.())
    .then((d) => {
      if (!disposed && token === getDataToken && dataSig === "") applyData(d);
    })
    .catch(() => {
      if (!disposed) setNote(T.loadFail);
    });
  ready = start3d().then(() => firstData).then(() => {
    if (!disposed) {
      ui.loading.hidden = true;
      syncSummary();
    }
  });

  return {
    update(next) {
      if (disposed) return;
      getDataToken++;
      applyData(next);
    },
    ready,
    get mode() {
      return mode;
    },
    stats: () => ({ ...stats, mode, ...(view?.info?.() ?? {}) }),
    dispose() {
      if (disposed) return;
      disposed = true;
      root.removeEventListener("keydown", onKey);
      ui.close.removeEventListener("click", onClose);
      chips.clear();
      view?.dispose();
      view = null;
      fallback?.dispose();
      fallback = null;
      root.removeAttribute("data-twin");
      root.replaceChildren();
    },
  };
}

/** The vendored builds: relative URLs only, so no import map (blocked by the CSP) is needed. */
async function loadVendored() {
  const [THREE, orbit] = await Promise.all([
    import("../vendor/three-0.185.1/build/three.module.min.js"),
    import("../vendor/three-0.185.1/examples/jsm/controls/OrbitControls.js"),
  ]);
  return { THREE, OrbitControls: orbit.OrbitControls };
}
