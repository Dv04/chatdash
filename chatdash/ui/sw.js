// Service worker: caches the app shell only, so the page opens offline (and says UNKNOWN, since no data).
// Never caches /api/: decisions and capacity are always live or shown as unknown.
const CACHE = "cp2-shell-v27";
const SHELL = ["./", "index.html", "css/app.css", "js/app.js", "js/api.js", "js/lib.js", "js/board.js", "js/cards.js", "js/answer.js",
  "js/capacity.js", "js/graph.js", "js/spawn.js", "js/workitem.js", "js/palette.js", "js/voice.js", "js/chat.js", "js/settings.js", "js/gx.js", "js/field.js", "js/nebula.js", "js/gl-nebula.js", "js/gl-worker.js", "js/md.js", "js/accounts.js", "js/sky.js", "css/nebula.css", "manifest.webmanifest", "icon.svg"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  .then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  // network first so a new build always wins; the cache is the offline fallback
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((m) => m || caches.match("index.html"))));
});
