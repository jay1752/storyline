// Sends a small "still here" ping about every 30 seconds, only while the tab is visible and the person has touched the page in the
// last 2 minutes. It sends the page area (stock, portfolio...) and nothing else. Used for the admin Users tab.
(function () {
  var path = location.pathname;
  var area = "other";
  if (path === "/" ) area = "home";
  else if (path.indexOf("/analytics/") === 0) area = "stock";
  else if (path.indexOf("/analytics") === 0) area = "stocks";
  else if (/^\/management\/\d+/.test(path)) area = "story";
  else if (path.indexOf("/management") === 0) area = "tracking";
  else if (path.indexOf("/portfolio") === 0) area = "portfolio";
  else if (path.indexOf("/rules") === 0) area = "rules";
  else if (path.indexOf("/community") === 0) area = "community";
  else if (path.indexOf("/settings") === 0) area = "settings";
  else if (path.indexOf("/profile") === 0) area = "profile";
  else if (path.indexOf("/welcome") === 0) area = "welcome";
  else if (path.indexOf("/admin") === 0) area = "admin";

  var lastTouch = Date.now();
  ["pointerdown", "keydown", "scroll", "touchstart"].forEach(function (ev) {
    window.addEventListener(ev, function () { lastTouch = Date.now(); }, { passive: true });
  });

  function ping() {
    if (document.visibilityState !== "visible" || Date.now() - lastTouch > 120000) return;
    var body = JSON.stringify({ area: area });
    if (navigator.sendBeacon) navigator.sendBeacon("/activity/beat", new Blob([body], { type: "application/json" }));
    else fetch("/activity/beat", { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true }).catch(function () {});
  }
  ping();
  setInterval(ping, 30000);
})();
