// ── settings.js — persisted UI toggles (pure + thin storage adapter) ────────
// A single JSON blob in localStorage holds the checkbox knobs in the settings
// panel, so they survive reloads. Kept deliberately dumb: read → merge over
// defaults (unknown/malformed values ignored) → write whole object.
//
// Storage is injectable so the merge logic is unit-testable without a DOM.

export const SETTINGS_KEY = 'tts-ui.settings';

/** Defaults: chunked is the only transport; paste auto-fire off. */
export const DEFAULTS = Object.freeze({
    pasteStream: false,
    normalizeText: true,
});

const KEYS = Object.keys(DEFAULTS);

/** Coerce one value to boolean, falling back to the default for junk. */
function asBool(value, fallback) {
    if (typeof value === 'boolean') return value;
    if (value === 'true' || value === 1 || value === '1') return true;
    if (value === 'false' || value === 0 || value === '0') return false;
    return fallback;
}

/**
 * Merge a parsed blob over DEFAULTS. Only known boolean keys are honoured;
 * anything else (missing, null, wrong type, extra keys) is ignored.
 */
export function normalize(raw) {
    const out = { ...DEFAULTS };
    if (raw && typeof raw === 'object') {
        for (const k of KEYS) out[k] = asBool(raw[k], DEFAULTS[k]);
    }
    return out;
}

/**
 * Build a settings store over a Storage-like backend (defaults to
 * localStorage). All access is guarded — private mode / disabled storage
 * degrades to in-memory defaults rather than throwing.
 */
export function createSettingsStore(storage = defaultStorage()) {
    function get() {
        if (!storage) return { ...DEFAULTS };
        try { return normalize(JSON.parse(storage.getItem(SETTINGS_KEY) || 'null')); }
        catch { return { ...DEFAULTS }; }
    }

    function set(partial) {
        const next = normalize({ ...get(), ...partial });
        try { storage?.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
        return next;
    }

    return { get, set };
}

function defaultStorage() {
    try { return globalThis.localStorage || null; } catch { return null; }
}
