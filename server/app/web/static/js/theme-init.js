// Sets <html data-theme="light|dark"> before the stylesheet paints (loaded in <head> without defer).
// Saved choice wins; otherwise light. Storage can be blocked (private mode, policies): never throw.
(function () {
  "use strict";
  var theme = null;
  try {
    theme = window.localStorage.getItem("nexxa-theme");
  } catch (e) {
    theme = null;
  }
  if (theme !== "light" && theme !== "dark") theme = "light"; // white by default; dark only when the visitor picks it
  document.documentElement.setAttribute("data-theme", theme);
  // Browser chrome colour = the theme's --color-background (light / dark blocks in nexxa.css).
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#18181B" : "#FFFFFF");
})();
