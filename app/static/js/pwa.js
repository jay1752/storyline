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
      sub.textContent = "Tap the Share button, then Add to Home Screen.";
    } else {
      text.textContent = "Install Tenetfolio";
      sub.textContent = "Use your browser menu: Install app, or Add to Home screen.";
    }
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
