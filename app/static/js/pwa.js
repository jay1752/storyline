// Registers the service worker and powers the "Install app" pill on the Profile page.
(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* the app works without it */ });
    });
  }

  var deferred = null;
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone;
  var pill = document.getElementById("install-pill");
  var ua = navigator.userAgent;
  var inApp = /FBAN|FBAV|Instagram|Line\/|Twitter|LinkedInApp|Snapchat|MicroMessenger|GSA\//i.test(ua);   // another app's browser: it cannot add to the Home Screen
  var chromeIos = /CriOS|EdgiOS|FxiOS/i.test(ua);                                                       // Chrome/Edge/Firefox on iPhone: the Share button sits in the address bar

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    render();
  });
  window.addEventListener("appinstalled", function () { deferred = null; standalone = true; render(); });

  function render() {
    if (!pill) return;
    var text = pill.querySelector("[data-text]");
    var sub = pill.querySelector("[data-sub]");
    var btn = pill.querySelector("[data-install]");
    btn.hidden = true;
    var h = pill.querySelector("[data-how]");
    if (h) h.hidden = true;
    if (standalone) {
      text.textContent = "Installed on this device";
      text.className = "ip-ok";
      sub.textContent = "You are using the app.";
    } else if (deferred) {
      text.textContent = "Install Tenetfolio on this device";
      sub.textContent = "One tap from your home screen, no browser bars.";
      btn.hidden = false;
    } else if (ios) {
      text.textContent = "Install Tenetfolio on this iPhone";
      sub.textContent = inApp ? "Open this page in Safari first, then add it to your Home Screen." : "Apple has no install button: you add it from the Share menu. We'll show you where.";
      var how = pill.querySelector("[data-how]");
      if (how) how.hidden = false;
    } else {
      text.textContent = "Install Tenetfolio";
      sub.textContent = "Use your browser menu: Install app, or Add to Home screen.";
    }
  }
  var sheet = document.getElementById("ios-install");
  if (pill && sheet) {
    var how = pill.querySelector("[data-how]");
    var step1 = sheet.querySelector("[data-step1]");
    if (step1) step1.textContent = chromeIos ? "Tap the Share button in the address bar" : "Tap the Share button at the bottom of Safari";
    var warn = sheet.querySelector("[data-inapp]");
    if (warn) warn.hidden = !inApp;
    if (how) how.addEventListener("click", function () { if (sheet.showModal) sheet.showModal(); else sheet.setAttribute("open", ""); });
    sheet.querySelector("[data-close]").addEventListener("click", function () { if (sheet.close) sheet.close(); else sheet.removeAttribute("open"); });
    var copy = sheet.querySelector("[data-copy]");
    if (copy) copy.addEventListener("click", function () {
      var done = function () { copy.textContent = "Link copied. Paste it in Safari."; };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(location.href).then(done, done); else done();
    });
  }
  if (pill) {
    pill.querySelector("[data-install]").addEventListener("click", function () {
      if (!deferred) return;
      deferred.prompt();
      deferred.userChoice.finally(function () { deferred = null; render(); });
    });
    render();
  }
})();
