// Runs before the first paint so a saved theme does not flash the other one. The same key is used by shell.mjs.
(function () {
  try {
    var saved = localStorage.getItem('disk-steward-theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved);
  } catch (e) {
    // storage is blocked: the page follows the system theme
  }
})();
