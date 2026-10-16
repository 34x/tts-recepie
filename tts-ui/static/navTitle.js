// ── navTitle.js — document.title derivation (pure) ──────────────────────────
// A minimal title source for the browser tab / nav bar: the first meaningful
// line of the text, markdown markers stripped. Nothing fancy — deterministic
// and unit-tested.

const MAX_LEN = 60;

/** Strip leading markdown markers (headings, quotes, list bullets, emphasis). */
function stripMarkdown(line) {
    return line
        .replace(/^\s{0,3}#{1,6}\s+/, '')        // # heading
        .replace(/^\s{0,3}>\s?/, '')             // > quote
        .replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/, '') // - / * / 1. list
        .replace(/\*\*(.+?)\*\*/g, '$1')         // **bold**
        .replace(/(^|\W)[*_]([^*_]+)[*_](?=\W|$)/g, '$1$2') // *em* _em_
        .replace(/`([^`]+)`/g, '$1')             // `code`
        .replace(/^\s*[-=*_]{3,}\s*$/, '')       // --- hr → empty
        .trim();
}

/**
 * Derive a short title from raw text: first non-empty line, markdown markers
 * stripped, collapsed whitespace, truncated to ~MAX_LEN chars.
 * Returns '' when there is nothing usable.
 */
export function deriveTitle(raw, max = MAX_LEN) {
    if (!raw) return '';
    for (const rawLine of String(raw).split(/\r?\n/)) {
        const line = stripMarkdown(rawLine).replace(/\s+/g, ' ').trim();
        if (!line) continue;
        return line.length > max ? line.slice(0, max - 1).trimEnd() + '\u2026' : line;
    }
    return '';
}

/** " · 12/40" progress suffix for play mode, or '' when not applicable. */
export function progressSuffix(state) {
    if (!state || state.mode !== 'play') return '';
    const total = state.chunks?.length || 0;
    if (!total) return '';
    const idx = Math.max(state.playIdx ?? 0, 0);
    const cur = Math.min(idx + 1, total);
    return ` \u00B7 ${cur}/${total}`;
}
