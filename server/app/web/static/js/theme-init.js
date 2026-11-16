// Sets <html data-theme="light|dark"> before the stylesheet paints (loaded in <head> without defer).
// Saved choice wins; otherwise follow the system. Storage can be blocked (private mode, policies): never throw.
(function () {
  "use strict";
  var theme = null;
  try {
    theme = window.localStorage.getItem("nexxa-theme");
  } catch (e) {
    theme = null;
  }
  if (theme !== "light" && theme !== "dark") {
    var dark = false;
    try {
      // a page may ask for dark when the visitor has not chosen (the robot screen); a saved choice still wins
      dark = document.documentElement.getAttribute("data-default-theme") === "dark" || window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (e) {
      dark = false;
    }
    theme = dark ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
  // Browser chrome colour = the theme's --color-background (light / dark blocks in nexxa.css).
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#0B1911" : "#E7F0E8");
})();
