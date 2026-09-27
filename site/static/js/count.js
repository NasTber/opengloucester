// Page view counting with GoatCounter (no cookies), loaded only when the site
// config turns it on. Sends the page path without any query string, the page's
// built title, where the visitor came from (without its query string), and the
// screen width. Searches and street lookups are counted as events, never with
// what was searched.
(function () {
  "use strict";
  var me = document.currentScript;
  var endpoint = me && me.getAttribute("data-endpoint");
  if (!endpoint || !navigator.sendBeacon) return;
  if (/^(localhost|127\.|\[::1\])/.test(location.hostname)) return;

  function referrer() {
    if (!document.referrer) return "";
    try {
      var r = new URL(document.referrer);
      return r.host === location.host ? r.pathname : r.origin + r.pathname;
    } catch (e) { return ""; }
  }

  function send(params) {
    var q = [];
    for (var k in params) q.push(k + "=" + encodeURIComponent(params[k]));
    navigator.sendBeacon(endpoint + "?" + q.join("&") + "&rnd=" + Math.random().toString(36).slice(2));
  }

  send({ p: location.pathname, t: me.getAttribute("data-title") || "", r: referrer(), s: window.screen.width });
  var searched = new URLSearchParams(location.search).get("q");
  if (searched && /^\/(streets|meetings\/search)\/$/.test(location.pathname)) {
    send({ p: location.pathname === "/streets/" ? "street-lookup" : "meeting-search", t: "", e: "true", s: window.screen.width });
  }
})();
