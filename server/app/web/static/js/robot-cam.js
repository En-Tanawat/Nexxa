// Near-live robot camera: shows the latest relayed frame (/api/public/robot/frame) in #robot-cam-stream.
// Only same-origin requests. The frame is re-fetched every `refresh_s` (5 s) while the tab is visible and the
// status says the robot is online with a fresh frame; otherwise there is no timer at all. A 204 / 429 / network
// failure makes <img> fire "error": that is shown as "no frame". All text goes in with textContent.
const FRAME_URL = "/api/public/robot/frame";
const LOAD_TIMEOUT_MS = 15_000; // a stuck request must not block the next refresh forever

const TEXTS = {
  offline: ["หุ่นยนต์ออฟไลน์", "หุ่นยนต์หยุดส่งข้อมูลเกิน 45 วินาที ภาพจะแสดงอีกครั้งเมื่อหุ่นยนต์กลับมาออนไลน์", "⚪ ออฟไลน์"],
  stale: ["ภาพจากกล้องหยุดอัปเดต", "ไม่ได้รับภาพใหม่จากหุ่นยนต์เกิน 60 วินาที", "⚪ ภาพเก่า"],
  empty: ["ยังไม่มีภาพจากกล้อง", "หุ่นยนต์จะส่งภาพเมื่อเริ่มตรวจ", "⚪ รอภาพ"],
};

export function initRobotCam() {
  const $ = (id) => document.getElementById(id);
  const img = $("robot-cam-stream");
  if (!img) return { setStatus() {} };
  const fallback = $("robot-cam-fallback"), title = $("robot-fallback-title"), text = $("robot-fallback-text");
  const ageEl = $("robot-cam-age"), chip = $("robot-chip-live-text");
  let wanted = false, timer = null, loading = false, loadingSince = 0, ageS = null, ageAt = 0, refreshMs = 5000;

  const age = () => (ageS == null ? null : ageS + Math.floor((Date.now() - ageAt) / 1000));

  function paintAge() {
    const a = age();
    if (!ageEl) return;
    ageEl.hidden = a == null || img.hidden;
    if (!ageEl.hidden) ageEl.textContent = `ภาพเมื่อ ${a} วินาทีที่แล้ว`;
  }

  function showLive() {
    img.hidden = false;
    if (fallback) fallback.hidden = true;
    if (chip) chip.textContent = "🔴 ภาพล่าสุด";
    paintAge();
  }

  function showEmpty(kind) {
    const [t, d, c] = TEXTS[kind];
    img.hidden = true;
    if (fallback) fallback.hidden = false;
    if (title) title.textContent = t;
    if (text) text.textContent = d;
    if (chip) chip.textContent = c;
    paintAge();
  }

  function load() {
    if (loading && Date.now() - loadingSince < LOAD_TIMEOUT_MS) return;
    loading = true;
    loadingSince = Date.now();
    img.src = `${FRAME_URL}?ts=${Date.now()}`;
  }

  function sync() {
    const run = wanted && !document.hidden;
    if (run && !timer) {
      load();
      timer = setInterval(() => { paintAge(); load(); }, refreshMs);
    } else if (!run && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  img.addEventListener("load", () => { loading = false; showLive(); });
  img.addEventListener("error", () => { loading = false; showEmpty("empty"); });
  document.addEventListener("visibilitychange", sync);

  return {
    /** data = the /api/public/robot JSON (or {online:false} when it could not be fetched). */
    setStatus(data) {
      const cam = data && data.camera ? data.camera : {};
      ageS = Number.isFinite(cam.age_s) ? cam.age_s : null;
      ageAt = Date.now();
      refreshMs = (Number.isFinite(cam.refresh_s) && cam.refresh_s > 0 ? cam.refresh_s : 5) * 1000;
      wanted = Boolean(data && data.online && cam.available);
      if (!wanted) {
        loading = false;
        img.removeAttribute("src"); // drop the old frame and abort any in-flight load
        showEmpty(!data || !data.online ? "offline" : ageS == null ? "empty" : "stale");
      }
      sync();
    },
  };
}
