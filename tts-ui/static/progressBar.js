// ── progressBar.js — SVG segmented progress bars (option O2) ───────────────
//
// Why not N <div>s: percentage widths across N flex children accumulate
// sub-pixel rounding → overflow and 1px seams. Why not a CSS gradient: the
// string grows O(N) and is awkward to diff. SVG rects in a chunk-unit viewBox
// scale cleanly, and run-coalescing keeps DOM nodes ≈ state transitions
// (usually a handful), not chunk count.
//
// Both bars are non-uniform in time (a chunk's width is its share of the
// text), so the play bar maps position by characters, not by index.

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Coalesce a chunk list into runs of equal download state.
 * Pure — unit-tested without a DOM.
 * @returns {Array<{x:number,w:number,state:string}>}
 */
export function stateRuns(chunks) {
    const out = [];
    let start = 0;
    for (let i = 1; i <= chunks.length; i++) {
        if (i === chunks.length || chunks[i].state !== chunks[start].state) {
            out.push({ x: start, w: i - start, state: chunks[start].state });
            start = i;
        }
    }
    return out;
}

/**
 * Playhead extent as fractions of total characters: {start, end}.
 * `start` = first char of the current chunk, `end` = last char + 1.
 * Pure — unit-tested.
 */
export function playFraction(chunks, playIdx) {
    const total = chunks.reduce((s, c) => s + (c.chars || 0), 0);
    if (!total || playIdx < 0 || playIdx >= chunks.length) return { start: 0, end: 0 };
    let before = 0;
    for (let i = 0; i < playIdx; i++) before += chunks[i].chars || 0;
    return { start: before / total, end: (before + (chunks[playIdx].chars || 0)) / total };
}

function ensureSvg(host, viewBox) {
    if (!host) return null;
    let svg = host.firstElementChild;
    if (!svg || svg.tagName.toLowerCase() !== 'svg') {
        svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('class', 'bar-svg');
        svg.setAttribute('preserveAspectRatio', 'none');
        host.replaceChildren(svg);
    }
    if (svg.getAttribute('viewBox') !== viewBox) svg.setAttribute('viewBox', viewBox);
    return svg;
}

function rect(cls, x, w) {
    const el = document.createElementNS(SVG_NS, 'rect');
    el.setAttribute('x', x);
    el.setAttribute('y', 0);
    el.setAttribute('width', w);
    el.setAttribute('height', 1);
    el.setAttribute('class', cls);
    return el;
}

/**
 * Download bar: one rect per contiguous run of equal state. Colors come from
 * CSS classes (seg-pending / seg-downloading / seg-downloaded / seg-errored).
 * Rebuilds only when the run signature changes.
 */
export function renderDownloadBar(host, chunks) {
    const n = chunks.length;
    const svg = ensureSvg(host, `0 0 ${n || 1} 1`);
    if (!svg) return;
    if (!n) { svg.replaceChildren(); return; }

    const rs = stateRuns(chunks);
    const sig = rs.map((r) => `${r.x}:${r.w}:${r.state}`).join('|');
    if (svg.dataset.sig === sig) return;
    svg.dataset.sig = sig;

    const frag = document.createDocumentFragment();
    for (const r of rs) frag.appendChild(rect(`seg seg-${r.state}`, r.x, r.w));
    svg.replaceChildren(frag);
}

/**
 * Play bar: filled up to the end of the current chunk (char-weighted) plus a
 * cursor marking the current chunk itself.
 */
export function renderPlayBar(host, chunks, playIdx) {
    const svg = ensureSvg(host, '0 0 1000 1');
    if (!svg) return;

    const { start, end } = playFraction(chunks, playIdx);
    const sig = `${start.toFixed(4)}:${end.toFixed(4)}`;
    if (svg.dataset.sig === sig) return;
    svg.dataset.sig = sig;

    const frag = document.createDocumentFragment();
    if (end > 0) frag.appendChild(rect('play-fill', 0, (end * 1000).toFixed(2)));
    if (playIdx >= 0) {
        const w = Math.max(2, (end - start) * 1000);
        frag.appendChild(rect('play-cursor', (start * 1000).toFixed(2), w.toFixed(2)));
    }
    svg.replaceChildren(frag);
}
