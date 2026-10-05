/* Static shell only. Live operational data stays on the network. */
const CACHE = "syzygy-shell-v3";
const SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) {
    return cache.addAll(SHELL);
  }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (key) {
      return key !== CACHE;
    }).map(function (key) {
      return caches.delete(key);
    }));
  }));
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf("/api/") === 0) return;
  if (SHELL.indexOf(url.pathname) < 0) return;
  event.respondWith(fetch(request).then(function (response) {
    if (!response || response.status !== 200 || response.type !== "basic") return response;
    const copy = response.clone();
    caches.open(CACHE).then(function (cache) {
      cache.put(request, copy);
    });
    return response;
  }).catch(function () {
    return caches.match(request);
  }));
});
