// ── theme.js — theme resolution (pure) + DOM application ────────────────────
// Preference model (L3): 'auto' | 'light' | 'dark', where 'auto' follows the
// OS via matchMedia(prefers-color-scheme). The *resolved* theme
// ('light' | 'dark') is what gets written as data-theme on <html>; CSS reacts
// to [data-theme="light"]. Theme-color meta is kept in sync for the PWA chrome.

export const STORAGE_KEY = 'tts-theme';
export const THEMES = ['auto', 'light', 'dark'];
export const ICON = { auto: '\uD83C\uDF17', light: '\u2600\uFE0F', dark: '\uD83C\uDF19' }; // 🌗 ☀️ 🌙
const DARK_META = '#0d1117';
const LIGHT_META = '#ffffff';
const MEDIA = '(prefers-color-scheme: light)';

/** Coerce an arbitrary stored value to a valid preference. */
export function normalize(pref) {
    return THEMES.includes(pref) ? pref : 'auto';
}

/** Resolve a preference to a concrete theme given the OS preference. */
export function resolve(pref, prefersLight) {
    const p = normalize(pref);
    if (p === 'light') return 'light';
    if (p === 'dark') return 'dark';
    return prefersLight ? 'light' : 'dark';
}

/** Cycle auto → light → dark → auto. */
export function next(pref) {
    return THEMES[(THEMES.indexOf(normalize(pref)) + 1) % THEMES.length];
}

export function metaColor(resolved) {
    return resolved === 'light' ? LIGHT_META : DARK_META;
}

// ── DOM application ─────────────────────────────────────────────────────────

let _mq = null;
let _current = 'auto'; // current *preference* (not resolved)

export function getPreference() {
    let stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch { /* private mode */ }
    return normalize(stored);
}

function _apply() {
    const resolved = resolve(_current, !!_mq?.matches);
    document.documentElement.dataset.theme = resolved;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', metaColor(resolved));
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = ICON[_current];
}

export function setPreference(pref) {
    _current = normalize(pref);
    try { localStorage.setItem(STORAGE_KEY, _current); } catch { /* ignore */ }
    _apply();
}

export function cycle() {
    setPreference(next(getPreference()));
}

/** Wire OS-change listener; call once on startup. */
export function init() {
    _mq = window.matchMedia ? window.matchMedia(MEDIA) : null;
    _current = getPreference();
    _mq?.addEventListener?.('change', _apply);
    _apply();
}
