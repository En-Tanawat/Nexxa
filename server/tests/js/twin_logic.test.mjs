// Pure-function tests for static/js/twin-logic.js (run by tests/test_web_twin.py with `node`).
import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const L = await import(pathToFileURL(resolve(process.env.TWIN_LOGIC ?? "app/web/static/js/twin-logic.js")).href);

const st = (id, over = {}) => ({ id, name: `สถานี ${id}`, crop: "กรีนคอส", map_x: null, map_y: null, route_order: id, status: "normal", age_days: 20, last_seen_at: null, confidence: null, ...over });
const overlap = (a, b) => Math.abs(a.x - b.x) < L.BED_W && Math.abs(a.z - b.z) < L.BED_D;

// ---- growth curve ------------------------------------------------------------------------------
test("plant scale follows a clamped, monotonic growth curve", () => {
  assert.equal(L.plantScale(null), 0, "no batch planted -> no plants");
  assert.equal(L.plantScale(undefined), 0);
  assert.equal(L.plantScale(Number.NaN), 0);
  assert.equal(L.plantScale("12"), 0, "only real numbers count");
  assert.equal(L.plantScale(-4), 0, "planted in the future -> not there yet");
  assert.equal(L.plantScale(0), L.MIN_SCALE);
  assert.equal(L.plantScale(L.HARVEST_DAYS), 1);
  assert.equal(L.plantScale(10_000), 1, "never grows past full size");
  assert.ok(Math.abs(L.plantScale(L.HARVEST_DAYS / 2) - (L.MIN_SCALE + (1 - L.MIN_SCALE) / 2)) < 1e-12);
  let prev = -1;
  for (let age = 0; age <= 60; age += 0.5) {
    const s = L.plantScale(age);
    assert.ok(s >= prev && s >= L.MIN_SCALE && s <= 1, `age ${age}`);
    prev = s;
  }
});

test("approach eases to the target without overshooting and snaps when close", () => {
  assert.equal(L.approach(0, 1, 0.016, 6) > 0, true);
  assert.equal(L.approach(0, 1, 0.016, 6) < 1, true);
  assert.equal(L.approach(0.9999, 1, 0.016, 6), 1);
  assert.equal(L.approach(1, 0, 10, 6), 0, "huge frame time never overshoots");
  assert.equal(L.approach(0.4, 0.4, 0.016, 6), 0.4);
});

// ---- status -> visual -------------------------------------------------------------------------------
test("every status has a colour token, an icon and a Thai label; nothing relies on colour alone", () => {
  const seen = new Set();
  for (const status of ["normal", "yellow", "brown", "review", "unknown"]) {
    const v = L.statusVisual(status);
    assert.equal(v.status, status);
    assert.match(v.label, /[฀-๿]/);
    assert.match(v.mark, /^--twin-mark-/);
    assert.match(v.plant, /^--twin-plant-/);
    assert.ok(Object.hasOwn(L.ICON_PATHS, v.icon), `icon ${v.icon}`);
    seen.add(v.icon).add(v.label);
  }
  assert.equal(seen.size, 10, "five different icons and five different labels");
  assert.equal(L.statusVisual("normal").label, "ปกติ");
  assert.equal(L.statusVisual("yellow").label, "ใบเหลือง");
  assert.equal(L.statusVisual("brown").label, "จุดน้ำตาล");
  assert.equal(L.statusVisual("review").label, "รอตรวจทาน");
});

test("unexpected statuses (including prototype keys) fall back to unknown", () => {
  for (const bad of [undefined, null, "", "NORMAL", "healthy", "__proto__", "constructor", "toString", 7, {}]) {
    assert.equal(L.statusVisual(bad).status, "unknown", String(bad));
  }
});

test("icon paths are plain SVG path data (24px grid)", () => {
  for (const [id, paths] of Object.entries(L.ICON_PATHS)) {
    assert.ok(Array.isArray(paths) && paths.length >= 1, id);
    for (const d of paths) assert.match(d, /^[MmLlHhVvCcSsQqTtAaZz0-9\s.,-]+$/, `${id}: ${d}`);
  }
});

// ---- input hygiene -------------------------------------------------------------------------------------
test("normalizeData keeps only well-formed stations and coerces fields", () => {
  const out = L.normalizeData({
    stations: [st(1), null, "x", 5, { id: "7", name: 42, status: "weird", age_days: "3", map_x: "1", map_y: Number.NaN }, st(1, { name: "dup" }), st(2, { age_days: -1 })],
    home: { map_x: 1, map_y: 2 },
    updated_at: "2026-11-16T07:00:00Z",
    demo: 1,
  });
  assert.deepEqual(out.stations.map((s) => s.id), [1, "7", 2], "duplicates and junk are dropped");
  const seven = out.stations[1];
  assert.equal(seven.name, "42");
  assert.equal(seven.status, "unknown");
  assert.equal(seven.age_days, null);
  assert.equal(seven.map_x, null, "strings are not coordinates");
  assert.equal(out.stations[2].age_days, null, "negative age is not an age");
  assert.deepEqual(out.home, { map_x: 1, map_y: 2 });
  assert.equal(out.demo, true);
});

test("normalizeData survives garbage and caps the station count", () => {
  for (const bad of [null, undefined, 3, "s", [], {}, { stations: "no" }, { stations: null }]) {
    assert.deepEqual(L.normalizeData(bad).stations, [], String(bad));
    assert.equal(L.normalizeData(bad).home, null);
  }
  const many = L.normalizeData({ stations: Array.from({ length: 500 }, (_, i) => st(i + 1)) });
  assert.equal(many.stations.length, L.MAX_STATIONS);
  assert.equal(L.normalizeData({ stations: [st(1)], home: { map_x: null, map_y: 3 } }).home, null, "home needs both coordinates");
  assert.equal(L.normalizeData({ stations: [st(1, { name: "  \n " })] }).stations[0].name, "สถานี 1", "blank names get a readable default");
});

test("a huge station name is capped before it can reach the page", () => {
  const out = L.normalizeData({ stations: [st(1, { name: "ก".repeat(5_000_000), crop: "x".repeat(10_000) })] }).stations[0];
  assert.ok(Array.from(out.name).length <= L.MAX_NAME && out.name.endsWith("…"));
  assert.ok(Array.from(out.crop).length <= L.MAX_NAME);
  assert.equal(L.normalizeData({ stations: [st(1, { name: "<script>alert(1)</script>" })] }).stations[0].name, "<script>alert(1)</script>", "markup stays text, untouched");
});

// ---- layout -----------------------------------------------------------------------------------------------
test("empty list gives an empty, still sized floor", () => {
  const l = L.layoutStations([]);
  assert.deepEqual(l.items, []);
  assert.ok(l.width >= L.MIN_FLOOR_W && l.depth >= L.MIN_FLOOR_D);
  assert.equal(l.home, null);
});

test("map coordinates are kept in relative order and centred on the origin", () => {
  const l = L.layoutStations([st(1, { map_x: 1, map_y: 1 }), st(2, { map_x: 2, map_y: 1 }), st(3, { map_x: 1, map_y: 2 }), st(4, { map_x: 2, map_y: 2 })]);
  const p = Object.fromEntries(l.items.map((i) => [i.id, i]));
  assert.ok(p[1].x < p[2].x && p[3].x < p[4].x, "x order kept");
  assert.ok(p[1].z < p[3].z && p[2].z < p[4].z, "y order kept (map_y grows towards the front)");
  assert.ok(Math.abs(p[1].z - p[2].z) < 1e-9 && Math.abs(p[1].x - p[3].x) < 1e-9);
  const xs = l.items.map((i) => i.x);
  const zs = l.items.map((i) => i.z);
  assert.ok(Math.abs((Math.min(...xs) + Math.max(...xs)) / 2) < 1e-9 && Math.abs((Math.min(...zs) + Math.max(...zs)) / 2) < 1e-9);
  assert.ok(l.items.every((i) => i.source === "map"));
  for (const i of l.items) assert.ok(Math.abs(i.x) + L.BED_W / 2 <= l.width / 2 && Math.abs(i.z) + L.BED_D / 2 <= l.depth / 2, "inside the floor");
});

test("stations without coordinates fall back to a serpentine grid in route order", () => {
  const stations = [st(5, { route_order: 3 }), st(6, { route_order: 1 }), st(7, { route_order: 2 }), st(8, { route_order: null }), st(9, { route_order: 4 })];
  const l = L.layoutStations(stations);
  assert.ok(l.items.every((i) => i.source === "route"));
  assert.deepEqual(l.items.map((i) => i.id), [6, 7, 5, 9, 8], "by route_order, nulls last");
  const [a, b] = l.items;
  assert.ok(a.x < b.x && Math.abs(a.z - b.z) < 1e-9, "first row runs left to right");
  for (let i = 0; i < l.items.length; i++) for (let j = i + 1; j < l.items.length; j++) assert.ok(!overlap(l.items[i], l.items[j]));
});

test("a row filled in both directions keeps the route continuous (second row runs back)", () => {
  const l = L.layoutStations(Array.from({ length: 12 }, (_, i) => st(i + 1)));
  const rows = new Map();
  for (const it of l.items) rows.set(it.z.toFixed(3), [...(rows.get(it.z.toFixed(3)) ?? []), it]);
  const [r1, r2] = [...rows.values()];
  assert.ok(r1[0].x < r1[r1.length - 1].x);
  assert.ok(r2[0].x > r2[r2.length - 1].x);
  assert.ok(Math.abs(r1[r1.length - 1].x - r2[0].x) < 1e-9, "snake turns at the same column");
});

test("mixed data: mapped stations keep their map place, the rest queue in a strip in front", () => {
  const l = L.layoutStations([st(1, { map_x: 0, map_y: 0 }), st(2, { map_x: 10, map_y: 0 }), st(3, { route_order: 2 }), st(4, { route_order: 1 })]);
  const p = Object.fromEntries(l.items.map((i) => [i.id, i]));
  assert.equal(p[1].source, "map");
  assert.equal(p[3].source, "route");
  assert.ok(p[3].z > p[1].z && p[4].z > p[1].z, "strip is in front of the mapped rows");
  assert.ok(p[4].x < p[3].x, "strip follows route order");
  for (let i = 0; i < l.items.length; i++) for (let j = i + 1; j < l.items.length; j++) assert.ok(!overlap(l.items[i], l.items[j]));
});

test("identical or crowded coordinates never overlap and stay inside the floor", () => {
  const l = L.layoutStations(Array.from({ length: 30 }, (_, i) => st(i + 1, { map_x: 5, map_y: 5, route_order: i + 1 })));
  assert.equal(l.items.length, 30);
  for (let i = 0; i < l.items.length; i++) {
    const a = l.items[i];
    assert.ok(Math.abs(a.x) + L.BED_W / 2 <= l.width / 2 + 1e-9 && Math.abs(a.z) + L.BED_D / 2 <= l.depth / 2 + 1e-9);
    for (let j = i + 1; j < l.items.length; j++) assert.ok(!overlap(a, l.items[j]), `${a.id} vs ${l.items[j].id}`);
  }
});

test("a straight line of stations (one axis constant) spreads along the other axis", () => {
  const l = L.layoutStations([st(1, { map_x: 3, map_y: 0 }), st(2, { map_x: 3, map_y: 1 }), st(3, { map_x: 3, map_y: 2 })]);
  const zs = l.items.map((i) => i.z);
  assert.ok(zs[0] < zs[1] && zs[1] < zs[2]);
  assert.ok(l.items.every((i) => Math.abs(i.x) < 1e-9));
});

test("huge / odd coordinates still produce finite numbers", () => {
  const l = L.layoutStations([st(1, { map_x: 1e12, map_y: -1e12 }), st(2, { map_x: -1e12, map_y: 1e12 }), st(3, { map_x: 0.5, map_y: 0.5 })]);
  for (const i of l.items) assert.ok(Number.isFinite(i.x) && Number.isFinite(i.z));
  assert.ok(Number.isFinite(l.width) && Number.isFinite(l.depth) && l.width < 200 && l.depth < 200);
});

test("sixty stations lay out quickly without overlap", () => {
  const l = L.layoutStations(Array.from({ length: 60 }, (_, i) => st(i + 1, { map_x: i % 10, map_y: Math.floor(i / 10) })));
  assert.equal(l.items.length, 60);
  for (let i = 0; i < 60; i++) for (let j = i + 1; j < 60; j++) assert.ok(!overlap(l.items[i], l.items[j]));
});

test("the home position uses the same transform as the stations", () => {
  const l = L.layoutStations([st(1, { map_x: 0, map_y: 0 }), st(2, { map_x: 4, map_y: 0 })], { map_x: 0, map_y: 0 });
  assert.ok(Math.abs(l.home.x - l.items[0].x) < 1e-9 && Math.abs(l.home.z - l.items[0].z) < 1e-9);
  assert.equal(L.layoutStations([st(1)], null).home, null);
  const unmapped = L.layoutStations([st(1)], { map_x: 1, map_y: 1 });
  assert.ok(unmapped.home && Math.abs(unmapped.home.x) <= unmapped.width / 2 && Math.abs(unmapped.home.z) <= unmapped.depth / 2, "home without map stations sits on the floor");
});

test("plant slots are deterministic per station and stay inside the bed", () => {
  const a = L.plantSlots(1);
  assert.equal(a.length, L.PLANTS_PER_BED);
  assert.deepEqual(a, L.plantSlots(1));
  assert.notDeepEqual(a, L.plantSlots(2));
  for (const s of a) {
    assert.ok(Math.abs(s.x) <= L.BED_W / 2 - 0.1 && Math.abs(s.z) <= L.BED_D / 2 - 0.05, JSON.stringify(s));
    assert.ok(s.size >= 0.85 && s.size <= 1.1);
  }
  assert.deepEqual(L.plantSlots("abc"), L.plantSlots("abc"), "string ids work too");
});

// ---- text ------------------------------------------------------------------------------------------------------
test("labels are truncated by whole characters, never mid-cluster, and neutralised", () => {
  assert.equal(L.truncateLabel("สถานี 1"), "สถานี 1");
  assert.equal(L.truncateLabel("  a \n\t b  "), "a b", "whitespace collapses");
  assert.equal(L.truncateLabel("abcdefghij", 5), "abcd…");
  assert.equal(L.truncateLabel("abcde", 5), "abcde");
  const long = "ก".repeat(200);
  const t = L.truncateLabel(long, 12);
  assert.equal(Array.from(t).length, 12);
  assert.ok(t.endsWith("…"));
  // a Thai tone mark stays with its consonant (a grapheme cluster is never split)
  const t2 = L.truncateLabel("กุ้งก้ามกรามใหญ่มากๆ", 5);
  const clusters = Array.from(new Intl.Segmenter("th", { granularity: "grapheme" }).segment(t2), (x) => x.segment);
  assert.equal(clusters.length, 5, "4 whole clusters + the ellipsis");
  assert.equal(clusters.slice(0, 2).join(""), "กุ้ง", "the tone mark and vowel stayed on their consonant");
  assert.equal(L.truncateLabel("เ".repeat(30), 6).replace(/…$/, "").endsWith("เ"), false, "never ends on a leading vowel");
  assert.equal(L.truncateLabel("a\u0000b‮evil"), "ab" + "evil", "control and bidi-override characters are removed");
  assert.equal(L.truncateLabel(null), "");
  assert.equal(L.truncateLabel(undefined), "");
  assert.equal(L.truncateLabel(12345), "12345");
  assert.equal(L.truncateLabel("<script>alert(1)</script>", 40), "<script>alert(1)</script>", "markup is just text here");
});

test("aria summary counts stations by status in Thai", () => {
  assert.equal(L.ariaSummary([]), "แผนผัง 3 มิติของโรงเรือน: ยังไม่มีสถานีให้แสดง");
  const s = L.ariaSummary([st(1), st(2, { status: "yellow" }), st(3, { status: "yellow" }), st(4, { status: "unknown" })]);
  assert.match(s, /^แผนผัง 3 มิติของโรงเรือน: 4 สถานี/);
  assert.match(s, /ปกติ 1/);
  assert.match(s, /ใบเหลือง 2/);
  assert.match(s, /ยังไม่มีข้อมูล 1/);
  assert.doesNotMatch(s, /จุดน้ำตาล|รอตรวจทาน/, "zero counts are left out");
  assert.doesNotMatch(L.ariaSummary([st(1, { name: "<script>" })]), /script/);
  assert.match(L.ariaSummary([st(1)], { flat: true }), /^แผนผัง 2 มิติของโรงเรือน/);
});

test("station detail lists the six facts with Thai labels and safe values", () => {
  const now = Date.parse("2026-11-16T07:10:00Z");
  const d = L.stationDetail(st(1, { name: "<b>x</b>", status: "yellow", age_days: 28, last_seen_at: "2026-11-16T07:05:00Z", confidence: 0.774 }), now);
  assert.equal(d.title, "<b>x</b>");
  const m = Object.fromEntries(d.rows.map((r) => [r.label, r.value]));
  assert.deepEqual(Object.keys(m), ["ผัก", "อายุ", "สถานะ", "ตรวจล่าสุด", "ความมั่นใจ"]);
  assert.equal(m["ผัก"], "กรีนคอส");
  assert.equal(m["อายุ"], "28 วัน");
  assert.equal(m["สถานะ"], "ใบเหลือง");
  assert.match(m["ตรวจล่าสุด"], /5 นาทีที่แล้ว/);
  assert.equal(m["ความมั่นใจ"], "77%");
  const empty = Object.fromEntries(L.stationDetail(st(2, { crop: null, status: "unknown", age_days: null }), now).rows.map((r) => [r.label, r.value]));
  assert.equal(empty["ผัก"], "ไม่ระบุผัก");
  assert.equal(empty["อายุ"], "ยังไม่มีล็อตที่ปลูกอยู่");
  assert.equal(empty["ตรวจล่าสุด"], "ยังไม่เคยตรวจ");
  assert.equal(empty["ความมั่นใจ"], "–");
});

test("camera framing grows with the floor, respects aspect ratio and stays above ground", () => {
  const a = L.framing(10, 6, 1.6);
  const b = L.framing(30, 20, 1.6);
  const narrow = L.framing(10, 6, 0.6);
  assert.ok(b.distance > a.distance && narrow.distance > a.distance);
  for (const f of [a, b, narrow]) {
    assert.ok(f.position[1] > 0 && f.minDistance < f.distance && f.distance < f.maxDistance);
    assert.ok(f.maxPolar < Math.PI / 2, "never below the floor");
    assert.ok(f.far > f.maxDistance * 2);
  }
  assert.ok(Number.isFinite(L.framing(0, 0, 0).distance), "degenerate input stays finite");
});
