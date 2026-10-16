// ── library.js — saved-text store (text only, no audio) ───────────────────
//
// Persists synthesized texts, most-recent-first, deduped by normalized
// content. The backing store is INJECTED behind a minimal interface so it can
// be swapped later (IndexedDB, server API) without touching callers:
//
//   read()        -> string | null   serialized payload, or null when empty
//   write(text)   -> boolean         true on success, false on failure
//
// The default backing store is localStorage (lazy — importing this module in
// a non-browser context such as tests does not throw).

const LIBRARY_KEY = 'tts-ui.library';

// Cap on stored text, measured in UTF-16 code units (JS string length).
// localStorage accounts usage as UTF-16, so this is ~2× the byte budget.
// Single tunable knob — change here, nowhere else.
export const LIBRARY_CAP_UNITS = 2 * 1024 * 1024;

// djb2 string hash — synchronous, dependency-free. Used only as a dedupe key
// (not a security boundary), so collisions are acceptable; dedupe compares
// normalized text as well.
export function djb2(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) {
        h = ((h << 5) + h + str.charCodeAt(i)) >>> 0;
    }
    return h.toString(36);
}

// Normalize for dedupe: trim + collapse internal whitespace. Case-sensitive.
export function normalize(text) {
    return text.trim().replace(/\s+/g, ' ');
}

/** localStorage-backed store. Lazy so tests can import the module. */
export function makeLocalStore(key = LIBRARY_KEY) {
    return {
        read() {
            try { return localStorage.getItem(key); } catch { return null; }
        },
        write(text) {
            try { localStorage.setItem(key, text); return true; }
            catch { return false; } // quota exceeded or storage blocked
        },
    };
}

/**
 * Create a library bound to a backing store.
 *
 * Storage interface — the only contract a backing store must satisfy:
 *   read()        -> string | null   serialized payload, or null when empty
 *   write(text)   -> boolean         true on success, false on failure
 *
 * @param {{read: () => (string|null), write: (t: string) => boolean}} store
 * @param {{now?: () => number, cap?: number}} [opts]  cap in UTF-16 code units
 */
export function createLibrary(store = makeLocalStore(), opts = {}) {
    const now = opts.now || (() => Date.now());
    const cap = opts.cap ?? LIBRARY_CAP_UNITS;

    function load() {
        const raw = store.read();
        if (!raw) return [];
        try {
            const arr = JSON.parse(raw);
            return Array.isArray(arr) ? arr : [];
        } catch { return []; }
    }

    function persist(entries) {
        if (!store.write(JSON.stringify(entries))) {
            throw new Error('library: storage write failed (quota or blocked)');
        }
        return entries;
    }

    // Keep newest entries until the cap is reached. The newest is always kept
    // even if it alone exceeds the cap, so a save is never silently lost.
    function evict(entries) {
        const newestFirst = entries.slice().sort((a, b) => b.lastUsedAt - a.lastUsedAt);
        const keep = new Set();
        let total = 0;
        for (const e of newestFirst) {
            if (keep.size === 0 || total + e.chars <= cap) {
                keep.add(e.id);
                total += e.chars;
            }
        }
        return entries.filter(e => keep.has(e.id));
    }

    return {
        /** Entries sorted most-recent-first. */
        list() {
            return load().slice().sort((a, b) => b.lastUsedAt - a.lastUsedAt);
        },

        /** Stable id for a text — the dedupe key, without saving. */
        idFor(text) {
            return djb2(normalize(text));
        },

        /** Single entry by id, or null. */
        get(id) {
            return load().find(e => e.id === id) ?? null;
        },

        /** Add or refresh a text. Dedupes by normalized content; returns list.
         *  Re-saving preserves the entry's remembered `lastChunk`. */
        save(text) {
            const norm = normalize(text);
            if (!norm) return this.list();
            const id = djb2(norm);
            const existing = load();
            const prior = existing.find(e => e.id === id);
            const entries = existing.filter(e => e.id !== id);
            entries.push({
                id,
                text,
                chars: text.length,
                lastUsedAt: now(),
                lastChunk: prior ? prior.lastChunk : 0,
            });
            return persist(evict(entries));
        },

        /** Remember the last-played chunk for an entry. Does not reorder. */
        setLastChunk(id, idx) {
            const entries = load();
            const entry = entries.find(e => e.id === id);
            if (!entry) return this.list();
            entry.lastChunk = Math.max(0, Math.trunc(idx) || 0);
            return persist(entries);
        },

        /** Remove one entry by id; returns the updated list. */
        remove(id) {
            return persist(load().filter(e => e.id !== id));
        },

        /** Drop all entries. */
        clear() {
            return persist([]);
        },
    };
}

// Default instance — localStorage-backed.
export const library = createLibrary();
