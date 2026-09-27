(function () {
  var seen = {};
  function toast(title, body) {
    var box = document.createElement("div");
    box.className = "push-toast";
    box.innerHTML = "<strong>" + title + "</strong><p>" + body + "</p>";
    document.body.appendChild(box);
    setTimeout(function () { box.classList.add("on"); }, 20);
    setTimeout(function () { box.remove(); }, 8000);
  }
  function poll() {
    fetch("/app/notificacoes?json=1&nao_lidas=1", {
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.json(); })
      .then(function (rows) {
        (rows || []).forEach(function (n) {
          if (seen[n.id]) return;
          seen[n.id] = true;
          if (n.kind === "chegando") toast(n.title, n.body);
        });
      })
      .catch(function () {});
  }
  poll();
  setInterval(poll, 8000);
})();
