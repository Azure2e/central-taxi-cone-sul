(function () {
  var el = document.getElementById("mapa-rotas");
  if (!el || typeof L === "undefined") return;
  if (window.__coneMap) return;

  var cacheKey = "cone_rotas_geo_v1";
  var cities = window.CONE_CITIES || {};
  var routes = window.CONE_ROUTES || [];
  try {
    var saved = localStorage.getItem(cacheKey);
    if (saved) {
      var parsed = JSON.parse(saved);
      if (parsed.cities) cities = parsed.cities;
      if (parsed.routes) routes = parsed.routes;
    }
    localStorage.setItem(cacheKey, JSON.stringify({ cities: cities, routes: routes, ts: Date.now() }));
  } catch (e) {}

  var map = L.map("mapa-rotas", { zoomControl: true, scrollWheelZoom: false, updateWhenIdle: true }).setView([-13.1, -60.55], 8);
  L.tileLayer("/static/vendor/osm-fallback.png", { maxZoom: 6, opacity: 0 }).addTo(map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 17,
    attribution: "&copy; OpenStreetMap",
    updateWhenIdle: true,
    keepBuffer: 6
  }).addTo(map);

  var bounds = [];
  Object.keys(cities).forEach(function (name) {
    var p = cities[name];
    if (!p || p.length < 2) return;
    L.marker(p).addTo(map).bindPopup(name);
    bounds.push(p);
  });
  var colors = ["#1a6a63", "#e76f51", "#2a9d8f", "#f4a261"];
  routes.forEach(function (r, i) {
    if (!r.from || !r.to) return;
    L.polyline([r.from, r.to], { color: colors[i % colors.length], weight: 4, opacity: 0.85 })
      .addTo(map)
      .bindPopup((r.label || "Rota") + (r.price ? " · R$ " + r.price : ""));
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [24, 24] });

  var live = {};
  function upsert(p) {
    if (p.lat == null || p.lng == null) return;
    var latlng = [Number(p.lat), Number(p.lng)];
    var html = (p.name || "Táxi") + (p.plate ? " · " + p.plate : "");
    if (live[p.id]) {
      live[p.id].setLatLng(latlng);
      live[p.id].setPopupContent(html);
    } else {
      live[p.id] = L.marker(latlng).addTo(map).bindPopup("GPS · " + html);
    }
  }
  function tick() {
    fetch("/api/gps/ao-vivo", { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok) return;
        (data.pontos || []).forEach(upsert);
      })
      .catch(function () {});
  }
  tick();
  setInterval(tick, 8000);
  window.__coneMap = map;
})();
