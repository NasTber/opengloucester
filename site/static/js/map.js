// Draws the maps made by the map_figure macro. The same places are always
// listed on the page, so the map is an extra, not the only way to see them.
(function () {
  "use strict";
  if (!window.L) return;

  function el(tag, text, attrs) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    for (var k in attrs || {}) node.setAttribute(k, attrs[k]);
    return node;
  }

  function popup(p) {
    var box = el("div");
    box.appendChild(el("strong", p.title));
    if (p.text) box.appendChild(el("p", p.text));
    if (p.url) {
      var a = el("a", p.link || "View on SeeClickFix", { href: p.url, target: "_blank", rel: "noopener" });
      a.appendChild(el("span", " (opens in new tab)", { "class": "visually-hidden" }));
      box.appendChild(a);
    }
    return box;
  }

  document.querySelectorAll("[data-map]").forEach(function (node) {
    var points = JSON.parse(document.getElementById(node.getAttribute("data-map")).textContent);
    if (!points.length) return;
    node.hidden = false;
    // On touch screens, one finger scrolls the page; pinch to zoom the map.
    var touch = window.matchMedia("(pointer: coarse)").matches;
    var map = L.map(node, { scrollWheelZoom: false, dragging: !touch, tap: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'
    }).addTo(map);
    var bounds = [];
    points.forEach(function (p) {
      L.circleMarker([p.lat, p.lng], {
        radius: p.size || 7, color: "#ffffff", weight: 2, fillColor: "#1e3e80", fillOpacity: 0.85
      }).bindPopup(popup(p)).addTo(map);
      bounds.push([p.lat, p.lng]);
    });
    map.fitBounds(bounds, { padding: [24, 24], maxZoom: 16 });
  });
})();
