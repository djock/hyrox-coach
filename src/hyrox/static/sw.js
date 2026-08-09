/* Service worker: keep the shell openable without a network.
 *
 * Deliberately narrow. It caches the shell and the last successfully fetched
 * page so the app opens in a gym basement; it never caches a POST, because
 * completions go through the IndexedDB queue in app.js instead.
 */

const CACHE = "hyrox-v1";
const SHELL = ["/static/app.css", "/static/app.js", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Network-first for pages so he never trains off a stale session, falling
  // back to the cached copy when the tunnel is unreachable.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && request.destination !== "") {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});
