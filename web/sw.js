/* SENTINEL ARENA - Service Worker
 *
 * Strategy:
 *   - App shell (HTML/CSS/JS): cache-first, revalidate in background
 *   - /api/state + /api/stream: network-only (never serve stale as live)
 *   - When the network is down, the app shell still loads and app.js shows
 *     the last snapshot from localStorage with a STALE badge.
 */
const CACHE = 'sentinel-v3';
const SHELL = [
  '/deck',
  '/style.css',
  '/app.js',
  '/manifest.json'
];

self.addEventListener('install', (ev) => {
  ev.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (ev) => {
  ev.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (ev) => {
  const url = new URL(ev.request.url);

  // never cache API or SSE: a stale PnL is worse than no PnL
  if (url.pathname.startsWith('/api/')) {
    return; // let the browser handle it (network only)
  }

  // navigation requests: try network, fall back to cached /deck
  if (ev.request.mode === 'navigate') {
    ev.respondWith(
      fetch(ev.request).catch(() =>
        caches.match('/deck').then((r) => r || caches.match('/'))
      )
    );
    return;
  }

  // static assets: cache-first with background revalidate
  ev.respondWith(
    caches.match(ev.request).then((cached) => {
      const network = fetch(ev.request).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(ev.request, copy)).catch(() => null);
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
