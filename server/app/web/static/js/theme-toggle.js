// Light/dark toggle: flips <html data-theme> (first set by theme-init.js) and remembers the choice.
// Until the user picks one, the page keeps following the system setting.
(() => {
  "use strict";
  const KEY = "nexxa-theme";
  const root = document.documentElement;
  const buttons = document.querySelectorAll("[data-theme-toggle]");
  const meta = document.querySelector('meta[name="theme-color"]');

  const saved = () => {
    try { return window.localStorage.getItem(KEY); } catch { return null; }
  };
  const save = (theme) => {
    try { window.localStorage.setItem(KEY, theme); } catch { /* storage blocked: the choice lasts for this page */ }
  };
  const apply = (theme) => {
    root.setAttribute("data-theme", theme);
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(theme === "dark")));
    meta?.setAttribute("content", getComputedStyle(root).getPropertyValue("--color-background").trim());
  };

  buttons.forEach((b) => b.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    save(next);
    apply(next);
  }));
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
    if (saved() !== "light" && saved() !== "dark") apply(e.matches ? "dark" : "light");
  });
  apply(root.getAttribute("data-theme") === "dark" ? "dark" : "light");
})();
