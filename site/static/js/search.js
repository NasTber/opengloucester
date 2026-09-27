// Meeting search. The text index is loaded only when there is a query. Lists
// meetings where every word appears, newest first, with the lines that match.
(function () {
  "use strict";
  var root = document.getElementById("search");
  if (!root || !window.fetch) return;
  var input = document.getElementById("search-q");
  var status = document.getElementById("search-status");
  var list = document.getElementById("search-results");
  var MAX_RESULTS = 50, MAX_EXCERPTS = 2, EXCERPT_CHARS = 220;

  var query = (new URLSearchParams(window.location.search).get("q") || "").trim();
  if (input) input.value = query;
  if (!query) return;
  document.title = query + " – " + document.title;

  // Lowercase without accents, so "cafe" finds "Café".
  function fold(s) {
    return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[‘’]/g, "'");
  }
  function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // A quoted query is one phrase; otherwise every word must appear.
  var folded = fold(query);
  var phrase = /^".+"$/.test(folded);
  var terms = phrase ? [folded.slice(1, -1).trim()] : folded.split(/\s+/);
  terms = terms.map(function (t) { return t.replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, ""); })
    .filter(function (t, i, all) { return t && all.indexOf(t) === i; });
  // Matches start at the beginning of a word: "art" finds "arts", not "party".
  var patterns = terms.map(function (t) { return new RegExp("(^|[^a-z0-9])" + escapeRegExp(t), "g"); });
  var whole = terms.length > 1 ? new RegExp("(^|[^a-z0-9])" + escapeRegExp(terms.join(" "))) : null;

  function el(tag, text, attrs) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    for (var k in attrs || {}) node.setAttribute(k, attrs[k]);
    return node;
  }
  function say(text) { status.textContent = text; }
  function test(re, s) { re.lastIndex = 0; return re.test(s); }

  // Text with each matching word wrapped in <mark>.
  function highlighted(text) {
    var out = document.createDocumentFragment(), low = fold(text);
    if (low.length !== text.length) { out.appendChild(document.createTextNode(text)); return out; }
    var ranges = [];
    patterns.forEach(function (re, i) {
      var m;
      re.lastIndex = 0;
      while ((m = re.exec(low))) {
        var start = m.index + m[1].length;
        ranges.push([start, start + terms[i].length]);
        if (re.lastIndex === m.index) re.lastIndex++;
      }
    });
    ranges.sort(function (a, b) { return a[0] - b[0]; });
    var pos = 0;
    ranges.forEach(function (r) {
      if (r[0] < pos) return;
      out.appendChild(document.createTextNode(text.slice(pos, r[0])));
      out.appendChild(el("mark", text.slice(r[0], r[1])));
      pos = r[1];
    });
    out.appendChild(document.createTextNode(text.slice(pos)));
    return out;
  }

  // The part of a long line around its first match.
  function clip(line) {
    if (line.length <= EXCERPT_CHARS) return line;
    var low = fold(line), first = line.length;
    patterns.forEach(function (re) {
      re.lastIndex = 0;
      var m = re.exec(low);
      if (m) first = Math.min(first, m.index + m[1].length);
    });
    var start = Math.max(0, Math.min(first - 60, line.length - EXCERPT_CHARS));
    var end = Math.min(line.length, start + EXCERPT_CHARS);
    // Cut at spaces, not mid-word.
    if (start > 0 && line.indexOf(" ", start) > -1 && line.indexOf(" ", start) < first) start = line.indexOf(" ", start) + 1;
    if (end < line.length && line.lastIndexOf(" ", end) > first) end = line.lastIndexOf(" ", end);
    return (start > 0 ? "…" : "") + line.slice(start, end).trim() + (end < line.length ? "…" : "");
  }

  function excerpts(meeting) {
    var found = [];
    meeting.docs.forEach(function (doc) {
      doc.text.split("\n").forEach(function (line) {
        var low = fold(line);
        var hits = patterns.filter(function (re) { return test(re, low); }).length;
        if (hits) found.push({ kind: doc.kind, line: line, hits: hits });
      });
    });
    found.sort(function (a, b) { return b.hits - a.hits; });
    return found.slice(0, MAX_EXCERPTS);
  }

  function show(index) {
    var matches = index.filter(function (m) {
      m.hay = m.hay || fold([m.board, m.date_text, m.date].concat(m.docs.map(function (d) { return d.text; })).join("\n"));
      return patterns.every(function (re) { return test(re, m.hay); });
    });
    // Meetings with the words together come first; otherwise newest first.
    if (whole) {
      matches = matches.filter(function (m) { return whole.test(m.hay); })
        .concat(matches.filter(function (m) { return !whole.test(m.hay); }));
    }
    var label = "“" + query + "”";
    if (!matches.length) {
      say("No meetings match " + label + ". Try fewer or different words.");
      return;
    }
    if (matches.length > MAX_RESULTS) {
      say("Showing " + MAX_RESULTS + " of " + matches.length + " meetings that match " + label + ". Add a word to narrow the list.");
    } else {
      say((matches.length === 1 ? "1 meeting matches " : matches.length + " meetings match ") + label + ".");
    }
    matches.slice(0, MAX_RESULTS).forEach(function (m) {
      var li = el("li");
      li.appendChild(el("a", m.board, { href: m.url }));
      li.appendChild(el("div", m.date_text, { "class": "small" }));
      excerpts(m).forEach(function (x) {
        var p = el("p", null, { "class": "excerpt" });
        p.appendChild(el("strong", x.kind + ": "));
        p.appendChild(highlighted(clip(x.line)));
        li.appendChild(p);
      });
      list.appendChild(li);
    });
  }

  if (!terms.length) { say("Enter a word to search for."); return; }
  say("Searching…");
  fetch(root.getAttribute("data-index"))
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(show)
    .catch(function () { say("Search couldn't load. Please try again later."); });
})();
