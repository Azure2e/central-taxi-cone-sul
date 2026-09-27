(function () {
  if (!navigator.geolocation) return;
  var token = (document.querySelector("input[name=csrf]") || {}).value || "";
  var lastSent = 0;

  function send(pos) {
    var now = Date.now();
    if (now - lastSent < 8000) return;
    lastSent = now;
    fetch("/motorista/posicao", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token
      },
      body: JSON.stringify({
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        acc: pos.coords.accuracy
      })
    }).catch(function () {});
  }

  navigator.geolocation.watchPosition(send, function () {}, {
    enableHighAccuracy: true,
    maximumAge: 5000,
    timeout: 12000
  });
  navigator.geolocation.getCurrentPosition(send, function () {}, {
    enableHighAccuracy: true,
    timeout: 8000
  });
})();
