(function () {
  var root = document.documentElement;
  var chosen = root.getAttribute("data-theme") || "auto";
  function apply() {
    if (chosen !== "auto") return;
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.setAttribute("data-theme", "auto");
    root.classList.toggle("is-dark", !!dark);
    document.body && document.body.classList.toggle("is-dark", !!dark);
  }
  apply();
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", apply);
  }
})();
