// 2D schematic of the greenhouse (SVG), used when WebGL is missing, fails or loses its context, and when twin.js itself
// cannot load. Same information as the 3D scene: one bed per station, status icon + Thai status text next to the name
// (never colour alone), a home marker. Colours come from CSS classes / tokens, so the theme toggle needs no JavaScript here.
// Safety: names go into <text> with textContent only; the SVG is built with createElementNS, never from strings.
import { BED_D, BED_W, ICON_PATHS, ariaSummary, plantScale, statusVisual, truncateLabel } from "./twin-logic.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const U = 40; // viewBox units per world unit
const PAD = 1.3; // world units of room around the floor for the labels above the top row
const COMPACT_ABOVE = 16; // more stations than this: icon-only chips (the name shows for the selected one and in the list)

function el(doc, tag, attrs = {}, text) {
  const node = doc.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Lucide-style 24px icon at world position (x, y), `size` world units wide. */
function icon(doc, id, x, y, size, cls) {
  const g = el(doc, "g", { class: cls, transform: `translate(${(x * U).toFixed(1)} ${(y * U).toFixed(1)}) scale(${((size * U) / 24).toFixed(3)})` });
  for (const d of ICON_PATHS[id]) g.append(el(doc, "path", { d }));
  return g;
}

function chip(doc, { icon: iconId, name, status, compact, home }) {
  const g = el(doc, "g", { class: "twinsvg__chip" });
  if (compact) {
    g.append(el(doc, "circle", { class: "twinsvg__chip-bg", cx: 0, cy: 0, r: 0.42 * U }));
    g.append(icon(doc, iconId, -0.26, -0.26, 0.52, "twinsvg__icon"));
    return g;
  }
  const w = 2.9;
  const h = status ? 0.9 : 0.62;
  g.append(el(doc, "rect", { class: "twinsvg__chip-bg", x: (-w / 2) * U, y: (-h / 2) * U, width: w * U, height: h * U, rx: 0.2 * U }));
  g.append(icon(doc, iconId, -w / 2 + 0.14, -0.2, 0.4, "twinsvg__icon"));
  g.append(el(doc, "text", { class: "twinsvg__name", x: (-w / 2 + 0.66) * U, y: (status ? -0.06 : 0.1) * U }, truncateLabel(name, home ? 12 : 14)));
  if (status) g.append(el(doc, "text", { class: "twinsvg__status", x: (-w / 2 + 0.66) * U, y: 0.27 * U }, status));
  return g;
}

/** -> { el, update({ stations, layout, home, selectedId }), dispose() }. `onSelect(id)` is called when a station is clicked. */
export function createFallback(doc, { onSelect } = {}) {
  const svg = el(doc, "svg", { class: "twinsvg", role: "img", focusable: "false", preserveAspectRatio: "xMidYMid meet" });
  const handler = (ev) => {
    const hit = ev.target?.closest?.("[data-id]");
    if (hit && onSelect) onSelect(hit.getAttribute("data-id"));
  };
  svg.addEventListener("click", handler);

  function update({ stations, layout, home, selectedId }) {
    const byId = new Map(stations.map((s) => [String(s.id), s]));
    const compact = stations.length > COMPACT_ABOVE;
    const { width: W, depth: D } = layout;
    svg.setAttribute("viewBox", [(-W / 2 - PAD) * U, (-D / 2 - PAD) * U, (W + 2 * PAD) * U, (D + 2 * PAD) * U].map((n) => n.toFixed(1)).join(" "));
    svg.setAttribute("aria-label", ariaSummary(stations, { flat: true }));
    const kids = [el(doc, "rect", { class: "twinsvg__floor", x: (-W / 2) * U, y: (-D / 2) * U, width: W * U, height: D * U, rx: 0.5 * U })];
    for (let x = Math.ceil(-W / 2) + 1; x < W / 2; x += 1) kids.push(el(doc, "line", { class: "twinsvg__grid", x1: x * U, y1: (-D / 2) * U, x2: x * U, y2: (D / 2) * U }));
    for (let z = Math.ceil(-D / 2) + 1; z < D / 2; z += 1) kids.push(el(doc, "line", { class: "twinsvg__grid", x1: (-W / 2) * U, y1: z * U, x2: (W / 2) * U, y2: z * U }));

    const labels = []; // drawn last so no bed covers a chip
    for (const it of layout.items) {
      const s = byId.get(String(it.id));
      if (!s) continue;
      const v = statusVisual(s.status);
      const selected = String(s.id) === String(selectedId);
      const g = el(doc, "g", { class: `twinsvg__st${selected ? " is-selected" : ""}`, "data-id": s.id, "data-status": v.status, transform: `translate(${(it.x * U).toFixed(1)} ${(it.z * U).toFixed(1)})` });
      g.append(el(doc, "rect", { class: "twinsvg__tray", x: (-BED_W / 2) * U, y: (-BED_D / 2) * U, width: BED_W * U, height: BED_D * U, rx: 0.16 * U }));
      const grown = plantScale(s.age_days);
      if (grown > 0) {
        const r = (0.07 + 0.11 * grown) * U;
        for (let row = 0; row < 2; row++) {
          for (let i = 0; i < 8; i++) g.append(el(doc, "circle", { class: "twinsvg__plant", cx: (-BED_W / 2 + 0.3 + i * ((BED_W - 0.6) / 7)) * U, cy: (row ? 0.2 : -0.2) * U, r: r.toFixed(1) }));
        }
      }
      g.append(el(doc, "rect", { class: "twinsvg__hit", x: (-BED_W / 2 - 0.1) * U, y: (-BED_D / 2 - 1.2) * U, width: (BED_W + 0.2) * U, height: (BED_D + 1.3) * U }));
      const full = !compact || selected;
      const c = chip(doc, { icon: v.icon, name: s.name, status: full ? v.label : null, compact: !full });
      c.setAttribute("transform", `translate(0 ${((-BED_D / 2 - (full ? 0.72 : 0.62)) * U).toFixed(1)})`);
      g.append(c);
      (selected ? labels : kids).push(g); // the selected station goes on top
    }
    if (layout.home) {
      const g = el(doc, "g", { class: "twinsvg__home", transform: `translate(${(layout.home.x * U).toFixed(1)} ${(layout.home.z * U).toFixed(1)})` });
      g.append(el(doc, "circle", { class: "twinsvg__home-ring", cx: 0, cy: 0, r: 0.5 * U }));
      const c = chip(doc, { icon: "home", name: "จุดจอด", status: null, compact: false, home: true });
      c.setAttribute("transform", `translate(0 ${(-0.95 * U).toFixed(1)})`);
      g.append(c);
      kids.push(g);
    }
    svg.replaceChildren(...kids, ...labels);
  }

  return {
    el: svg,
    update,
    dispose() {
      svg.removeEventListener("click", handler);
      svg.replaceChildren();
      svg.remove();
    },
  };
}
