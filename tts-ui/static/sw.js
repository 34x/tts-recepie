/* TTS PWA service worker — network-first for app code, cache-first for static assets */
//
// Strategy:
//   - /api/*                → network only (never cached)
//   - app code (/ , *.html, *.js, *.css) → NETWORK FIRST, cache fallback
//   - other static assets   → CACHE FIRST, network fallback
//
// Why network-first for app code: the previous cache-first policy silently
// served stale JS/HTML after every deploy. The cache is only repopulated when
// this script changes (install -> cache.addAll) or CACHE is renamed, so any
// edit to app.js/playback.js/etc. was invisible until someone remembered to
// bump the version. Network-first makes deploys take effect immediately;
// the cache remains only as an offline fallback.
const CACHE = 'tts-ui-v11';

// Precache on install — used for offline fallback, NOT authoritative.
const STATIC_ASSETS = [
    '/',
    '/index.html',
    '/manifest.json',
    '/style.css',
    '/app.js',
    '/state.js',
    '/render.js',
    '/navTitle.js',
    '/theme.js',
    '/icons.js',
    '/progressBar.js',
    '/settings.js',
    '/settingsPin.js',
    '/api.js',
    '/playback.js',
    '/chunkedPlayback.js',
    '/chunker.js',
    '/library.js',
    '/icons/icon-192.png',
    '/icons/icon-512.png',
    '/silence.mp3'
];

// Extensions treated as "app code" — network-first so edits land immediately.
const NETWORK_FIRST_EXT = ['.html', '.js', '.css'];

function isAppCode(url) {
    if (url.pathname === '/' || url.pathname.endsWith('/')) return true;
    return NETWORK_FIRST_EXT.some((ext) => url.pathname.endsWith(ext));
}

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (e) => {
    const url = new URL(e.request.url);
    // API calls always go to network — never cache dynamic responses.
    if (url.pathname.startsWith('/api/')) return;
    // Only handle same-origin GET requests.
    if (e.request.method !== 'GET') return;
    if (url.origin !== location.origin) return;

    if (isAppCode(url)) {
        // Network-first: always try the server, fall back to cache offline.
        e.respondWith(
            fetch(e.request).then((resp) => {
                if (resp.ok) {
                    const clone = resp.clone();
                    caches.open(CACHE).then((cache) => cache.put(e.request, clone));
                }
                return resp;
            }).catch(() => caches.match(e.request))
        );
        return;
    }

    // Cache-first for truly static assets (icons, audio, manifest).
    e.respondWith(
        caches.match(e.request).then((cached) => {
            if (cached) return cached;
            return fetch(e.request).then((resp) => {
                if (resp.ok && url.origin === location.origin) {
                    const clone = resp.clone();
                    caches.open(CACHE).then((cache) => cache.put(e.request, clone));
                }
                return resp;
            });
        })
    );
});
