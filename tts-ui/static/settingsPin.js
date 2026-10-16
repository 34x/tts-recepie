// ── settingsPin.js — settings fingerprint for cache validity ────────────────
// Short stable hash over the settings that affect synthesized audio.
// Attached to cached artifacts (chunk audio entries, future local cache) so a
// changed voice/model/instruct/backend invalidates reuse — "pin-keyed cache".
// Purely a validity tag: never alters playback behavior or the lastChunk
// bookmark, which stays settings-independent.

const PIN_LEN = 8; // hex chars kept

function xorHash(str) {
    // FNV-1a 32-bit — deterministic across sessions, tiny, collision risk is
    // irrelevant here: a collision only resurrects a stale cache entry that
    // would otherwise be regenerated (same risk class as any short hash tag).
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
        h ^= str.charCodeAt(i);
        h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
}

/**
 * Fingerprint of audio-affecting settings.
 * @param {{backend?: string, model?: string, voice?: string, instruct?: string}} s
 * @returns {string} short hex token, '' when all fields empty (nothing pinned)
 */
export function computePin(s = {}) {
    const norm = [
        s.backend || '', s.model || '', s.voice || '', s.instruct || '',
    ].join('\u0000');
    if (!norm.replaceAll('\u0000', '')) return '';
    return xorHash(norm).toString(16).padStart(PIN_LEN, '0');
}

/**
 * True when a cached artifact's pin still matches current settings.
 * A missing pin on the artifact means "unpinnable/legacy" → not reusable.
 */
export function pinMatches(artifactPin, currentPin) {
    return !!artifactPin && !!currentPin && artifactPin === currentPin;
}