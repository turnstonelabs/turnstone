/* Shared theme toggle — turnstone design system
   Hook: window.onThemeChange(nextTheme) called after toggle */

function toggleTheme() {
  var next = document.documentElement.dataset.theme === "light" ? "" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("turnstone-theme", next || "dark");
  if (typeof window.onThemeChange === "function") window.onThemeChange(next);
}

(function initTheme() {
  var stored = localStorage.getItem("turnstone-theme");
  if (stored === "light") {
    document.documentElement.dataset.theme = "light";
  } else if (
    !stored &&
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: light)").matches
  ) {
    document.documentElement.dataset.theme = "light";
  }
})();
