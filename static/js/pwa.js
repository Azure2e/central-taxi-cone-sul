(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  }
  var deferred = null;
  function showBars(on) {
    document.querySelectorAll("#pwa-install").forEach(function (el) { el.hidden = !on; });
  }
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    showBars(true);
  });
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("#pwa-install-btn, .js-pwa-install");
    if (!btn) return;
    if (deferred) {
      deferred.prompt();
      deferred.userChoice.finally(function () {
        deferred = null;
        showBars(false);
      });
      return;
    }
    window.location.href = "/instalar";
  });
  function urlBase64ToUint8Array(b64) {
    var pad = "=".repeat((4 - (b64.length % 4)) % 4);
    var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  async function enablePush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
    var perm = await Notification.requestPermission();
    if (perm !== "granted") return;
    var key = (window.VAPID_PUBLIC || "").trim();
    if (!key) {
      var info = await fetch("/api/push/key").then(function (r) { return r.json(); }).catch(function () { return {}; });
      key = info.publicKey || "";
    }
    if (!key) return;
    var reg = await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key)
    });
    await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON())
    });
  }
  if (document.body && document.body.dataset && document.body.dataset.push !== "off") {
    enablePush().catch(function () {});
  }
})();
