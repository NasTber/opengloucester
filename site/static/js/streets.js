// Street lookup. The street index is loaded only when there is a query (or
// when the street box is first used, for its suggestions). Street names are
// matched the same way the site builds the index: house numbers dropped and
// street types ("Av", "Avenue", "AVE") written one way.
(function () {
  "use strict";
  var root = document.getElementById("street");
  if (!root || !window.fetch) return;
  var input = document.getElementById("street-q");
  var names = document.getElementById("street-names");
  var status = document.getElementById("street-status");
  var results = document.getElementById("street-results");
  var index = null;

  var query = (new URLSearchParams(window.location.search).get("q") || "").trim();
  if (input) input.value = query;

  function load() {
    if (!index) {
      index = fetch(root.getAttribute("data-index"))
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }
    return index;
  }

  function el(tag, text, attrs) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    for (var k in attrs || {}) node.setAttribute(k, attrs[k]);
    return node;
  }

  function keyFor(text, suffixes) {
    var words = text.toUpperCase().replace(/[.,'’]/g, "").replace(/\s+(UNIT|APT|#)\s*\S+$/, "")
      .replace(/^\s*#?\d+[A-Z]?(\s*[-–\/&]?\s*\d+[A-Z]?)*\s+/, "").trim().split(/\s+/);
    var last = words[words.length - 1];
    if (words.length > 1 && suffixes[last]) words[words.length - 1] = suffixes[last];
    return words.join(" ");
  }

  function date(iso) {
    var d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function money(n) { return "$" + Math.round(n).toLocaleString("en-US"); }

  function section(title, total, shown, items) {
    var wrap = document.createDocumentFragment();
    wrap.appendChild(el("h3", title + " (" + total.toLocaleString("en-US") + ")"));
    if (!total) { wrap.appendChild(el("p", "None found.", { "class": "small" })); return wrap; }
    var list = el("ul", "", { "class": "plain-list" });
    items.forEach(function (li) { list.appendChild(li); });
    wrap.appendChild(list);
    if (total > shown) wrap.appendChild(el("p", "Showing the latest " + shown + ".", { "class": "small" }));
    return wrap;
  }

  function item(lead, href, detail, extra) {
    var li = el("li");
    li.appendChild(href ? el("a", lead, { href: href }) : el("strong", lead));
    li.appendChild(el("span", detail, { "class": "item-detail" }));
    if (extra) li.appendChild(el("span", extra, { "class": "item-detail" }));
    return li;
  }

  function show(key, s) {
    results.textContent = "";
    status.textContent = s.name + ": " + s.meetings_total + " agenda and minutes mentions, " +
      s.permits_total + " permits, " + s.requests_total + " 311 requests.";
    results.appendChild(el("h2", s.name, { id: "street-name" }));
    results.appendChild(section("On agendas and minutes", s.meetings_total, s.meetings.length, s.meetings.map(function (m) {
      return item(m.board, m.url, date(m.date) + " · " + m.doc, m.line);
    })));
    results.appendChild(section("Building and demolition permits", s.permits_total, s.permits.length, s.permits.map(function (p) {
      var detail = [p.type, date(p.date), p.status].concat(p.cost ? ["Estimated " + money(p.cost)] : []).join(" · ");
      return item(p.address, null, detail, p.work);
    })));
    results.appendChild(section("311 requests, past 12 months", s.requests_total, s.requests.length, s.requests.map(function (r) {
      return item(r.category, r.url, [r.address, date(r.date), r.status === "open" ? "Open" : "Closed"].join(" · "));
    })));
  }

  function suggest(matches, streets) {
    results.textContent = "";
    status.textContent = matches.length ? "No exact match. Did you mean one of these?" : "No street by that name was found.";
    if (!matches.length) return;
    var list = el("ul", "", { "class": "plain-list" });
    matches.slice(0, 12).forEach(function (k) {
      var li = el("li");
      li.appendChild(el("a", streets[k].name, { href: "/streets/?q=" + encodeURIComponent(streets[k].name) }));
      list.appendChild(li);
    });
    results.appendChild(list);
  }

  if (input && names) {
    input.addEventListener("focus", function fill() {
      input.removeEventListener("focus", fill);
      load().then(function (data) {
        Object.keys(data.streets).forEach(function (k) { names.appendChild(el("option", "", { value: data.streets[k].name })); });
      }).catch(function () {});
    });
  }

  if (!query) return;
  document.title = query + " – " + document.title;
  status.textContent = "Looking up " + query + "…";
  load().then(function (data) {
    var streets = data.streets, key = keyFor(query, data.suffixes);
    if (streets[key]) { show(key, streets[key]); return; }
    var bare = key.split(" ").filter(function (w) { return !data.suffixes[w]; }).join(" ");
    var matches = Object.keys(streets).filter(function (k) { return bare && k.indexOf(bare) === 0; });
    if (matches.length === 1) show(matches[0], streets[matches[0]]);
    else suggest(matches, streets);
  }).catch(function () {
    status.textContent = "The street list could not be loaded. Please try again later.";
  });
})();
