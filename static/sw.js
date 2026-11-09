// Service worker: makes the app installable and serves static files (CSS, icons, fonts)
// from the device cache so taps don't wait on them. Pages always come from the network.
const CACHE = "uv-static-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim())
));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  const sameStatic = url.origin === self.location.origin && url.pathname.startsWith("/static/");
  const font = url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
  if (req.method !== "GET" || !(sameStatic || font)) return;
  // Show the cached copy now, refresh it in the background.
  e.respondWith(caches.open(CACHE).then((cache) =>
    cache.match(req).then((hit) => {
      const fresh = fetch(req).then((res) => {
        if (res.ok || res.type === "opaque") cache.put(req, res.clone());
        return res;
      }).catch(() => hit);
      return hit || fresh;
    })
  ));
});
