// Registers the service worker and shows a quiet, dismissible "install" hint on the pages a phone user lives on.
(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* the app works without it */ });
    });
  }

  var hint = document.getElementById("install-hint");
  if (!hint) return;
  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone;
  var KEY = "tf.install.dismissed";
  var dismissed = false;
  try { dismissed = Date.now() - Number(localStorage.getItem(KEY) || 0) < 30 * 864e5; } catch (e) { /* private mode */ }
  if (standalone || dismissed || !/^\/(portfolio|management)/.test(location.pathname)) return;

  var text = hint.querySelector("[data-text]");
  var go = hint.querySelector("[data-install]");
  var close = hint.querySelector("[data-dismiss]");
  var deferred = null;
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;

  function show(msg, canInstall) {
    text.textContent = msg;
    go.hidden = !canInstall;
    hint.hidden = false;
  }
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    show("Add Tenetfolio to your home screen for one-tap access.", true);
  });
  window.addEventListener("appinstalled", function () { hint.hidden = true; });
  if (ios) show("To install: tap the Share button, then Add to Home Screen.", false);

  go.addEventListener("click", function () {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.finally(function () { deferred = null; hint.hidden = true; });
  });
  close.addEventListener("click", function () {
    hint.hidden = true;
    try { localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* ignore */ }
  });
})();
