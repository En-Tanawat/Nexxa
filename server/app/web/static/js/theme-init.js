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
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (e) {
      dark = false;
    }
    theme = dark ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
