(function () {
  var seen = {};
  var token = (document.querySelector("input[name=csrf]") || {}).value || "";

  function askPermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      Notification.requestPermission();
    }
  }

  function toast(title, body) {
    var box = document.createElement("div");
    box.className = "push-toast";
    box.innerHTML = "<strong>" + title + "</strong><p>" + body + "</p>";
    document.body.appendChild(box);
    setTimeout(function () { box.classList.add("on"); }, 20);
    setTimeout(function () { box.remove(); }, 9000);
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(title, { body: body, tag: "conesul-motorista" });
      } catch (e) {}
    }
  }

  function poll() {
    fetch("/motorista/notificacoes?json=1&nao_lidas=1", {
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.json(); })
      .then(function (rows) {
        (rows || []).forEach(function (n) {
          if (seen[n.id]) return;
          seen[n.id] = true;
          toast(n.title, n.body);
        });
      })
      .catch(function () {});
  }

  askPermission();
  poll();
  setInterval(poll, 7000);
})();
