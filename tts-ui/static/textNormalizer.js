// ── textNormalizer.js — deterministic client-side text cleanup ─────────────
//
// Purpose: strip characters that break TTS speech *generation* (emojis and
// stray symbols a model's tokenizer rejects) so every backend synthesizes the
// same clean text. This is NOT a general "bad character" cleaner — markdown,
// links and the like are a separate concern and deliberately out of scope.
//
// Guarantees:
//   - Preserves ALL Unicode letters. Removal targets only emoji / symbol code
//     points (verified to contain zero letter code points); ru/de/ja/… survive.
//   - Deterministic + idempotent: cleanText(x) === cleanText(cleanText(x)).
//     Determinism keeps the stored per-entry resume index (lastChunk) valid
//     across sessions because chunking is computed on the same cleaned text.
//
// Extension: rules live in the STEPS array below as an ordered [name, fn]
// list. To add a rule, append [name, fn] — existing steps are never reordered
// or removed, so new rules drop in without disturbing existing behavior. The
// same mechanism is used by tests (see textNormalizer.test.mjs).

const _INVISIBLE = /[\u200B\uFEFF]/g; // zero-width space + BOM (never meaningful)

// Emoji code points. Each range contains ONLY symbols/pictographs — no letters.
// Covers faces, animals, food, activities, travel, objects, flags and the
// classic symbol-emoji families. (U+1F200-U+1F2FF enclosed ideographs excluded
// on purpose — a future decision whether to treat them as emoji.)
const _EMOJI_CP = '[\u{1F1E6}-\u{1F1FF}\u{1F300}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2B00}-\u{2BFF}]';
// Presentation modifiers + the emoji ZWJ joiner. Removed only when adjacent to
// an emoji so a stray ZWJ inside a non-emoji script (e.g. Indic conjuncts) is
// left untouched. (U+200C ZWNJ is intentionally NOT touched — it is a real
// word separator in Persian/Urdu/etc., so stripping it would change wording.)
const _MODIFIER = '[\u200D\uFE0E\uFE0F]';

// An emoji optionally followed by presentation modifiers / the ZWJ joiner (the
// family 👨‍👩‍👧 is emoji + ZWJ + emoji …). Matches one or both sides so a leading
// modifier before the emoji is also caught.
const _EMOJI_RUN = new RegExp(`(?:${_EMOJI_CP}${_MODIFIER}*|${_MODIFIER}*${_EMOJI_CP})`, 'gu');

// v1 rule: strip emoji / symbol code points (invisible chars first, then runs).
function stripEmojiAndSymbols(text) {
    return text.replace(_INVISIBLE, '').replace(_EMOJI_RUN, '');
}

// Ordered list of [name, fn] steps. Append new rules here; never reorder.
const STEPS = [
    ['v1_emoji', stripEmojiAndSymbols],
];

// Build a cleaning function from an ordered [name, fn] step list. Pure: same
// input + same steps → same output. Non-string / null input passes through
// unchanged so callers can pass anything safely.
function makeCleaner(steps) {
    return (text) => {
        if (text == null) return text;
        if (typeof text !== 'string') return String(text);
        let out = text;
        for (const [, step] of steps) out = step(out);
        return out;
    };
}

// The default pipeline (the single v1 rule today).
export const cleanText = makeCleaner(STEPS);

// Names of the active rules, in order. Diagnostics + tests only; never mutate
// STEPS directly from outside — append via a new makeCleaner call instead.
export function stepNames() {
    return STEPS.map(([name]) => name);
}

// Exposed for composition and tests: build a pipeline that layers an extra
// rule on top of the defaults without touching the shared STEPS list.
export { makeCleaner, STEPS };
