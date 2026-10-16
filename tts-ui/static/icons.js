// ── icons.js — inline SVG icon set (no fonts, no runtime cost) ──────────────
// Text glyphs for media controls (▶️ ⏸ ⏮ ⏭) render inconsistently: mix of
// emoji/text presentation, per-platform font shapes, and fixed colours that
// ignore the theme. These are plain SVG path strings using `currentColor`, so
// they are identical on every platform and follow the text colour/theme.
//
// Strings only — assigned via innerHTML, coloured by CSS. No work at runtime.

const SVG = (body) =>
    `<svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" ` +
    `aria-hidden="true" focusable="false">${body}</svg>`;

export const SVG_ICONS = {
    play:  SVG('<path d="M8 5v14l11-7z"/>'),
    pause: SVG('<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>'),
    prev:  SVG('<path d="M6 5h2v14H6zM20 5v14L9.5 12z"/>'),
    next:  SVG('<path d="M16 5h2v14h-2zM4 5v14l10.5-7z"/>'),
};
