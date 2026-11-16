// NEXXA UI behaviour: progressive enhancement only — every page works without this file.
// User-supplied text is never parsed as HTML; this file only toggles attributes.
(() => {
  "use strict";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Toasts: close button + auto-dismiss after 6s (paused while hovered or focused).
  const dismiss = (toast) => {
    if (!toast.isConnected) return;
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), reduceMotion.matches ? 0 : 220);
  };
  document.querySelectorAll("[data-toast]").forEach((toast) => {
    let timer = setTimeout(() => dismiss(toast), 6000);
    const pause = () => clearTimeout(timer);
    const resume = () => { timer = setTimeout(() => dismiss(toast), 3000); };
    toast.addEventListener("mouseenter", pause);
    toast.addEventListener("focusin", pause);
    toast.addEventListener("mouseleave", resume);
    toast.addEventListener("focusout", resume);
    toast.querySelector("[data-toast-close]")?.addEventListener("click", () => dismiss(toast));
  });

  // Show/hide password.
  document.querySelectorAll("[data-toggle-password]").forEach((btn) => {
    const input = document.getElementById(btn.dataset.togglePassword);
    if (!input) return;
    btn.hidden = false;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", String(show));
      btn.setAttribute("aria-label", show ? "ซ่อนรหัสผ่าน" : "แสดงรหัสผ่าน");
      input.focus();
    });
  });

  // Confirm destructive POSTs: <form data-confirm="...">.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    // Prevent double submits; the button keeps its label for screen readers.
    if (form.hasAttribute("data-busy-form")) {
      form.querySelectorAll("button[type=submit]").forEach((b) => {
        b.setAttribute("aria-disabled", "true");
        b.classList.add("is-busy");
      });
      form.addEventListener("submit", (e) => e.preventDefault(), { once: true });
    }
  });

  // Mobile nav strip: keep the current section in view.
  document.querySelector(".nav__link[aria-current=page]")?.scrollIntoView({ block: "nearest", inline: "center" });

  // Robot ESP32 live monitor auto-polling
  const robotRoot = document.querySelector("[data-robot-monitor]");
  if (robotRoot) {
    const poll = () => {
      fetch("/api/public/robot")
        .then((r) => r.json())
        .then((data) => {
          if (!data) return;
          const setTxt = (id, txt) => {
            const el = robotRoot.querySelector(`[data-robot-${id}]`);
            if (el && txt !== undefined && txt !== null) el.textContent = txt;
          };
          setTxt("state", data.state_label || (data.online ? data.state : "ออฟไลน์"));
          setTxt("battery", data.battery_pct !== null && data.battery_pct !== undefined ? `${data.battery_pct}%` : "-");
          setTxt("wifi", data.wifi_rssi !== null && data.wifi_rssi !== undefined ? `${data.wifi_rssi} dBm` : "-");
          setTxt("station", data.station_name || (data.station_id ? `สถานี ${data.station_id}` : "จุดพัก / แท่นชาร์จ"));
          setTxt("angle", data.camera_angle_label ? `มุมกล้อง: ${data.camera_angle_label}` : "ไม่ได้เปิดกล้อง");
          setTxt("step", data.step || "-");
          setTxt("hud-status", data.online ? "LIVE" : "OFFLINE");
          setTxt("hud-angle", data.camera_angle_label ? `มุม${data.camera_angle_label}` : "-");

          const badge = robotRoot.querySelector("[data-robot-badge-online]");
          if (badge) {
            badge.textContent = data.online ? "ออนไลน์" : "ออฟไลน์";
            badge.className = data.online ? "badge badge--ok" : "badge badge--neutral";
          }
        })
        .catch(() => {});
    };
    setInterval(poll, 4000);
  }
})();
