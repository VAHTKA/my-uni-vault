// Service worker: installable app + instant navigation.
//  - static files (CSS, JS, icons) and fonts come from the device cache;
//  - the main tab pages open instantly from the cache, and the page itself then
//    fetches the fresh copy and updates what changed (see app.js).
const STATIC = "uv-static-v2";
const PAGES = "uv-pages-v1";
const TAB_PAGES = ["/files/", "/timetable", "/reminders", "/settings"];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== STATIC && k !== PAGES).map((k) => caches.delete(k))))
    .then(() => self.clients.claim())
));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  // Signing out: forget every saved page so the next person on this device sees none of them.
  if (sameOrigin && url.pathname === "/logout") {
    e.waitUntil(caches.delete(PAGES));
    return;
  }

  // The app icon opens "/", which only redirects to the files page: skip the round trip.
  if (req.mode === "navigate" && sameOrigin && url.pathname === "/" && !url.search) {
    e.respondWith(caches.open(PAGES).then((cache) => cache.match("/files/")).then((hit) =>
      hit ? Response.redirect("/files/", 302) : fetch(req)));
    return;
  }

  // Tab pages: show the saved copy straight away; the page refreshes it in the background.
  if (req.mode === "navigate" && sameOrigin && TAB_PAGES.includes(url.pathname)) {
    e.respondWith((async () => {
      const cache = await caches.open(PAGES);
      const hit = await cache.match(url.pathname + url.search);
      if (hit) return hit;
      const res = await fetch(req);
      const type = res.headers.get("content-type") || "";
      if (res.ok && !res.redirected && type.includes("text/html")) cache.put(url.pathname + url.search, res.clone());
      return res;
    })());
    return;
  }

  const font = url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
  if (!(sameOrigin && url.pathname.startsWith("/static/")) && !font) return;
  // Static files carry a ?v=<hash> that changes with the file, so a saved copy is always right.
  e.respondWith(caches.open(STATIC).then((cache) =>
    cache.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok || res.type === "opaque") cache.put(req, res.clone());
      return res;
    }))
  ));
});
