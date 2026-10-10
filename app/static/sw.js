// Tenetfolio service worker.
// It caches ONLY static files, and only under their versioned URL (?v=...), so a deploy changes the URL and the new file is fetched at once.
// Pages, search, chat and every data response are never touched: they always come from the server, so a price or a holding can't be stale.
// With no connection, a page load shows /offline (not the last page seen).
const VERSION = "tf-sw-v1";
const STATIC = VERSION + "-static";
const OFFLINE_URL = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC)
      .then((cache) => cache.addAll([OFFLINE_URL, "/static/icons/icon-192.png"]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    if (url.searchParams.has("v")) {
      // versioned file: the URL changes whenever the content does, so a cached copy is always the right one
      event.respondWith(
        caches.open(STATIC).then(async (cache) => {
          const hit = await cache.match(req);
          if (hit) return hit;
          const res = await fetch(req);
          if (res.ok) cache.put(req, res.clone());
          return res;
        })
      );
    } else {
      // unversioned file (images and icons): network first, the cache only when offline
      event.respondWith(
        fetch(req)
          .then((res) => { if (res.ok) caches.open(STATIC).then((c) => c.put(req, res.clone())); return res; })
          .catch(() => caches.match(req))
      );
    }
  }
  // anything else: not intercepted
});
