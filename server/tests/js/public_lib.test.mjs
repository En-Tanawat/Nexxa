// Pure-function tests for static/js/public-lib.js (run by tests/test_web_public.py with `node`).
import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const lib = await import(pathToFileURL(resolve(process.env.PUBLIC_LIB ?? "app/web/static/js/public-lib.js")).href);

test("number formatting is Thai-friendly with one-decimal percentages", () => {
  assert.equal(lib.fmtInt(1234567), "1,234,567");
  assert.equal(lib.fmtInt(0), "0");
  assert.equal(lib.fmtInt(null), "–");
  assert.equal(lib.fmtInt(undefined), "–");
  assert.equal(lib.fmtPct(0.12345), "12.3%");
  assert.equal(lib.fmtPct(1 / 3), "33.3%");
  assert.equal(lib.fmtPct(0), "0.0%");
  assert.equal(lib.fmtPct(1), "100.0%");
  assert.equal(lib.fmtPct(null), "–");
  assert.equal(lib.fmtPct(0.5, 0), "50%");
  assert.equal(lib.fmtPct(Number.NaN), "–");
});

test("dates use Thai month names and Bangkok time", () => {
  assert.equal(lib.fmtDay("2026-11-16"), "16 พ.ย.");
  assert.equal(lib.fmtTime("2026-11-16T07:05:00Z"), "14:05");
  assert.match(lib.fmtDateTime("2026-11-16T07:05:00Z"), /16 พ\.ย\. 2569/);
  assert.equal(lib.fmtTime("not a date"), "–");
  assert.equal(lib.fmtTime(null), "–");
});

test("status maps to a badge kind plus Thai text (never colour alone)", () => {
  assert.deepEqual(lib.statusInfo("normal"), { kind: "ok", text: "ปกติ" });
  assert.deepEqual(lib.statusInfo("yellow"), { kind: "warn", text: "ใบเหลือง" });
  assert.deepEqual(lib.statusInfo("brown"), { kind: "danger", text: "จุดน้ำตาล" });
  assert.deepEqual(lib.statusInfo("review"), { kind: "info", text: "รอตรวจทาน" });
  assert.deepEqual(lib.statusInfo("unknown"), { kind: "neutral", text: "ยังไม่มีข้อมูล" });
  assert.deepEqual(lib.statusInfo("something-new"), { kind: "neutral", text: "ยังไม่มีข้อมูล" });
  assert.deepEqual(lib.statusInfo(undefined), { kind: "neutral", text: "ยังไม่มีข้อมูล" });
});

test("trend arrow vs yesterday: a rise of the abnormal rate is flagged as worse", () => {
  const up = lib.trendInfo(0.12, 0.08);
  assert.equal(up.dir, "up");
  assert.equal(up.tone, "bad");
  assert.match(up.text, /สูงขึ้น 4\.0 จุด/);
  const down = lib.trendInfo(0.05, 0.08);
  assert.equal(down.dir, "down");
  assert.equal(down.tone, "good");
  assert.match(down.text, /ลดลง 3\.0 จุด/);
  assert.equal(lib.trendInfo(0.081, 0.08).dir, "flat");
  assert.match(lib.trendInfo(0.08, 0.08).text, /ทรงตัว/);
  for (const [a, b] of [[null, 0.1], [0.1, null], [undefined, undefined]]) {
    const n = lib.trendInfo(a, b);
    assert.equal(n.dir, "none");
    assert.match(n.text, /เทียบกับเมื่อวานไม่ได้/);
  }
});

test("yesterday's abnormal rate comes from the trends payload, weighted by accepted photos", () => {
  const trends = { series: [
    { station_id: 1, points: [
      { day: "2026-11-15", n: 10, yellow_rate: 0.2, brown_rate: 0.1, rejected_rate: 0 },
      { day: "2026-11-16", n: 4, yellow_rate: 0.5, brown_rate: 0.0, rejected_rate: 0 }] },
    { station_id: 2, points: [
      { day: "2026-11-15", n: 10, yellow_rate: 0.0, brown_rate: 0.0, rejected_rate: 0.5 }] },
  ] };
  // station 1: 10 accepted photos * 0.3 ; station 2: 5 accepted photos * 0.0 -> 3 / 15
  assert.ok(Math.abs(lib.yesterdayAbnormal(trends, "2026-11-16T05:00:00+00:00") - 0.2) < 1e-9);
  assert.equal(lib.yesterdayAbnormal({ series: [] }, "2026-11-16T05:00:00+00:00"), null);
  assert.equal(lib.yesterdayAbnormal(null, "2026-11-16T05:00:00+00:00"), null);
  assert.equal(lib.yesterdayAbnormal(trends, "garbage"), null);
  const nulls = { series: [{ points: [{ day: "2026-11-15", n: 3, yellow_rate: null, brown_rate: null, rejected_rate: 1 }] }] };
  assert.equal(lib.yesterdayAbnormal(nulls, "2026-11-16T05:00:00Z"), null);
});

test("freshness: fresh within the stale window, stale after, unknown without a timestamp", () => {
  const now = Date.parse("2026-11-16T07:10:00Z");
  assert.equal(lib.freshness("2026-11-16T07:09:00Z", now), "fresh");
  assert.equal(lib.freshness("2026-11-16T07:00:00Z", now), "stale");
  assert.equal(lib.freshness(null, now), "unknown");
  assert.equal(lib.freshness("nope", now), "unknown");
});

test("relative time in Thai", () => {
  const now = Date.parse("2026-11-16T07:10:00Z");
  assert.equal(lib.relativeTime("2026-11-16T07:09:50Z", now), "เมื่อสักครู่");
  assert.equal(lib.relativeTime("2026-11-16T07:05:00Z", now), "5 นาทีที่แล้ว");
  assert.equal(lib.relativeTime("2026-11-16T04:10:00Z", now), "3 ชั่วโมงที่แล้ว");
  assert.equal(lib.relativeTime("2026-11-14T07:10:00Z", now), "2 วันที่แล้ว");
  assert.equal(lib.relativeTime("2026-11-16T07:30:00Z", now), "เมื่อสักครู่"); // clock skew: never "in the future"
  assert.equal(lib.relativeTime(null, now), "ยังไม่เคยตรวจ");
});

test("harvest wording for upcoming and overdue lots", () => {
  assert.deepEqual(lib.harvestInfo(5), { kind: "info", text: "อีก 5 วัน" });
  assert.deepEqual(lib.harvestInfo(1), { kind: "warn", text: "อีก 1 วัน" });
  assert.deepEqual(lib.harvestInfo(0), { kind: "warn", text: "ครบกำหนดวันนี้" });
  assert.deepEqual(lib.harvestInfo(-3), { kind: "danger", text: "เกินกำหนด 3 วัน" });
});

test("activity feed: newest first, only stations that have been seen, capped", () => {
  const now = Date.parse("2026-11-16T07:10:00Z");
  const st = (id, status, seen) => ({ id, name: "S" + id, status, last_seen_at: seen });
  const feed = lib.activityFeed([
    st(1, "normal", "2026-11-16T06:00:00Z"), st(2, "brown", "2026-11-16T07:05:00Z"),
    st(3, "unknown", null), st(4, "review", "2026-11-16T06:30:00Z"),
  ], now, 2);
  assert.equal(feed.length, 2);
  assert.deepEqual(feed.map((f) => f.id), [2, 4]);
  assert.equal(feed[0].when, "5 นาทีที่แล้ว");
  assert.equal(feed[0].badge.kind, "danger");
  assert.equal(lib.activityFeed([], now).length, 0);
});

test("fetch errors become a Thai message", () => {
  assert.match(lib.errorMessage({ status: 429, retryAfter: 60 }), /ถี่เกินไป/);
  assert.match(lib.errorMessage({ status: 500 }), /เซิร์ฟเวอร์/);
  assert.match(lib.errorMessage({ status: 503 }), /เซิร์ฟเวอร์/);
  assert.match(lib.errorMessage({ offline: true }), /อินเทอร์เน็ต/);
  assert.match(lib.errorMessage({ timeout: true }), /นานเกินไป/);
  assert.match(lib.errorMessage(new Error("boom")), /ไม่สำเร็จ/);
  assert.ok(!lib.errorMessage({ status: 500, body: "Traceback secret" }).includes("secret"));
});

test("each series gets a distinct colour + dash + marker combination", () => {
  const seen = new Set();
  for (let i = 0; i < 12; i++) {
    const s = lib.seriesStyle(i);
    assert.match(s.colorVar, /^--chart-[1-6]$/);
    assert.ok(Array.isArray(s.dash));
    assert.ok(["circle", "rect", "triangle", "rectRot", "crossRot", "star"].includes(s.point));
    seen.add(`${s.colorVar}|${s.dash.join(",")}|${s.point}`);
  }
  assert.equal(seen.size, 12);
  assert.deepEqual(lib.seriesStyle(0).dash, []);
  assert.notDeepEqual(lib.seriesStyle(1).dash, lib.seriesStyle(0).dash);
  assert.notEqual(lib.seriesStyle(0).colorVar, lib.seriesStyle(1).colorVar);
});

const TRENDS = { days: 30, series: [
  { station_id: 1, station: "สถานี 1", points: [
    { day: "2026-11-14", n: 10, yellow_rate: 0.1, brown_rate: 0.0, review_rate: 0, rejected_rate: 0.1 },
    { day: "2026-11-16", n: 10, yellow_rate: 0.3, brown_rate: 0.05, review_rate: 0, rejected_rate: 0.0 }] },
  { station_id: 2, station: "สถานี 2", points: [
    { day: "2026-11-15", n: 8, yellow_rate: null, brown_rate: 0.2, review_rate: 0, rejected_rate: 0.25 }] },
], same_age: [
  { age_day: 3, batches: [{ batch_label: "สถานี 1 (2026-10-01)", abnormal_rate: 0.1 }] },
  { age_day: 4, batches: [{ batch_label: "สถานี 1 (2026-10-01)", abnormal_rate: 0.2 }, { batch_label: "สถานี 2 (2026-10-10)", abnormal_rate: 0.05 }] },
] };

test("day series: shared sorted day axis, null where a station has no point", () => {
  const m = lib.buildDaySeries(TRENDS.series, "yellow_rate");
  assert.deepEqual(m.days, ["2026-11-14", "2026-11-15", "2026-11-16"]);
  assert.equal(m.datasets.length, 2);
  assert.deepEqual(m.datasets[0].data, [0.1, null, 0.3]);
  assert.deepEqual(m.datasets[1].data, [null, null, null]);
  assert.equal(m.datasets[0].label, "สถานี 1");
  assert.equal(m.datasets[0].stationId, 1);
  assert.notEqual(m.datasets[0].styleIndex, m.datasets[1].styleIndex);
  assert.deepEqual(lib.buildDaySeries(TRENDS.series, "rejected_rate").datasets[1].data, [null, 0.25, null]);
});

test("symptom rate series can add yellow + brown, but stays null when both are unknown", () => {
  const m = lib.buildDaySeries(TRENDS.series, "abnormal");
  assert.ok(Math.abs(m.datasets[0].data[2] - 0.35) < 1e-9);
  assert.equal(m.datasets[1].data[1], 0.2);
  assert.equal(lib.buildDaySeries([{ station_id: 3, station: "x", points: [{ day: "2026-11-16", yellow_rate: null, brown_rate: null }] }], "abnormal").datasets[0].data[0], null);
});

test("empty / garbage payloads give empty models, never throw", () => {
  assert.deepEqual(lib.buildDaySeries([], "yellow_rate"), { days: [], datasets: [] });
  assert.deepEqual(lib.buildDaySeries(null, "yellow_rate"), { days: [], datasets: [] });
  assert.deepEqual(lib.buildSameAge([]), { ages: [], datasets: [] });
  assert.deepEqual(lib.buildSameAge(undefined), { ages: [], datasets: [] });
  assert.equal(lib.hasAnyValue(lib.buildDaySeries([], "yellow_rate")), false);
});

test("same-age series: one dataset per batch over the age axis", () => {
  const m = lib.buildSameAge(TRENDS.same_age);
  assert.deepEqual(m.ages, [3, 4]);
  assert.equal(m.datasets.length, 2);
  assert.deepEqual(m.datasets[0].data, [0.1, 0.2]);
  assert.deepEqual(m.datasets[1].data, [null, 0.05]);
  assert.equal(m.datasets[1].label, "สถานี 2 (2026-10-10)");
  assert.equal(lib.hasAnyValue(m), true);
});

test("table model matches the chart data, formatted for people", () => {
  const m = lib.buildDaySeries(TRENDS.series, "yellow_rate");
  const t = lib.tableModel(m, "day");
  assert.deepEqual(t.head, ["วันที่", "สถานี 1", "สถานี 2"]);
  assert.deepEqual(t.rows[0], ["14 พ.ย.", "10.0%", "–"]);
  assert.deepEqual(t.rows[2], ["16 พ.ย.", "30.0%", "–"]);
  const a = lib.tableModel(lib.buildSameAge(TRENDS.same_age), "age");
  assert.equal(a.head[0], "อายุ (วัน)");
  assert.deepEqual(a.rows[1], ["4", "20.0%", "5.0%"]);
});

test("text summary names the highest and latest value, and says so when empty", () => {
  const m = lib.buildDaySeries(TRENDS.series, "abnormal");
  const s = lib.summaryText(m, "day", "อัตราอาการผิดปกติ");
  assert.match(s, /สถานี 1/);
  assert.match(s, /35\.0%/);
  assert.match(lib.summaryText({ days: [], datasets: [] }, "day", "x"), /ยังไม่มีข้อมูล/);
  assert.match(lib.summaryText(lib.buildSameAge(TRENDS.same_age), "age", "x"), /อายุ 4 วัน/);
});

test("valid range and station parsing from the query string", () => {
  assert.equal(lib.parseDays("7"), 7);
  assert.equal(lib.parseDays("100"), 100);
  assert.equal(lib.parseDays("45"), 30);
  assert.equal(lib.parseDays(null), 30);
  assert.equal(lib.parseStation("3"), 3);
  assert.equal(lib.parseStation("0"), null);
  assert.equal(lib.parseStation("abc"), null);
  assert.equal(lib.parseStation("1e3"), null);
  assert.equal(lib.parseStation(null), null);
});

test("end labels never overlap: pushed apart, kept inside the plot", () => {
  const out = lib.spreadLabels([100, 102, 104, 200], 14, 20, 300);
  for (let i = 1; i < out.length; i++) assert.ok(out[i] - out[i - 1] >= 14 - 1e-9, String(out));
  assert.ok(out.every((y) => y >= 20 && y <= 300));
  assert.equal(out[3], 200);
  assert.deepEqual(lib.spreadLabels([], 14, 0, 100), []);
  const crowded = lib.spreadLabels([290, 295, 300], 14, 20, 300);
  assert.ok(crowded.every((y) => y <= 300) && crowded[1] - crowded[0] >= 14);
});

test("getJson turns HTTP and network failures into small plain error objects", async () => {
  const resp = (status, body, headers = {}) => ({ ok: status < 400, status, headers: { get: (k) => headers[k] ?? null }, json: async () => body });
  assert.deepEqual(await lib.getJson("/x", { fetchImpl: async () => resp(200, { a: 1 }) }), { a: 1 });
  await assert.rejects(lib.getJson("/x", { fetchImpl: async () => resp(429, {}, { "Retry-After": "60" }) }), { status: 429, retryAfter: 60 });
  await assert.rejects(lib.getJson("/x", { fetchImpl: async () => resp(500, { detail: "Traceback secret" }) }), (e) => e.status === 500 && !JSON.stringify(e).includes("secret"));
  await assert.rejects(lib.getJson("/x", { fetchImpl: async () => { throw new TypeError("Failed to fetch"); } }), { offline: true });
  await assert.rejects(lib.getJson("/x", { timeoutMs: 20, fetchImpl: (u, { signal }) => new Promise((_, rej) => signal.addEventListener("abort", () => rej(new DOMException("x", "AbortError")))) }), { timeout: true });
  const ctl = new AbortController();
  ctl.abort();
  await assert.rejects(lib.getJson("/x", { signal: ctl.signal, fetchImpl: async (u, { signal }) => { throw new DOMException("x", "AbortError"); } }), { aborted: true });
});
