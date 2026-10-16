// ── progressBar.test.mjs — SVG bar geometry (pure logic) ───────────────────
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stateRuns, playFraction } from './progressBar.js';

const c = (state, chars = 10) => ({ state, chars });

test('stateRuns: empty list', () => {
    assert.deepEqual(stateRuns([]), []);
});

test('stateRuns: all pending collapses to one run', () => {
    assert.deepEqual(stateRuns([c('pending'), c('pending'), c('pending')]),
        [{ x: 0, w: 3, state: 'pending' }]);
});

test('stateRuns: coalesces contiguous equal states', () => {
    const runs = stateRuns([
        c('downloaded'), c('downloaded'),
        c('downloading'), c('pending'), c('pending'),
        c('downloaded'),
    ]);
    assert.deepEqual(runs, [
        { x: 0, w: 2, state: 'downloaded' },
        { x: 2, w: 1, state: 'downloading' },
        { x: 3, w: 2, state: 'pending' },
        { x: 5, w: 1, state: 'downloaded' },
    ]);
});

test('stateRuns: nodes == transitions, not chunk count', () => {
    // 1000 chunks, alternating → many runs; but a uniform book → 1 run.
    const uniform = Array.from({ length: 1000 }, () => c('downloaded'));
    assert.equal(stateRuns(uniform).length, 1);
});

test('stateRuns: covers the whole range with no gaps', () => {
    const chunks = [c('pending'), c('downloaded'), c('pending'), c('errored')];
    const runs = stateRuns(chunks);
    assert.equal(runs[0].x, 0);
    const last = runs[runs.length - 1];
    assert.equal(last.x + last.w, chunks.length);
    for (let i = 1; i < runs.length; i++) {
        assert.equal(runs[i].x, runs[i - 1].x + runs[i - 1].w);
    }
});

test('playFraction: no chunks / no playhead', () => {
    assert.deepEqual(playFraction([], 0), { start: 0, end: 0 });
    assert.deepEqual(playFraction([c('downloaded')], -1), { start: 0, end: 0 });
    assert.deepEqual(playFraction([c('downloaded')], 5), { start: 0, end: 0 });
});

test('playFraction: char-weighted, not index-weighted', () => {
    // chunk 0 = 10 chars, chunk 1 = 30 chars, chunk 2 = 10 chars (total 50)
    const chunks = [c('downloaded', 10), c('downloaded', 30), c('downloaded', 10)];
    assert.deepEqual(playFraction(chunks, 0), { start: 0, end: 0.2 });
    assert.deepEqual(playFraction(chunks, 1), { start: 0.2, end: 0.8 });
    assert.deepEqual(playFraction(chunks, 2), { start: 0.8, end: 1 });
});

test('playFraction: last chunk ends at 1', () => {
    const chunks = [c('downloaded', 7), c('downloaded', 13)];
    assert.equal(playFraction(chunks, 1).end, 1);
});

test('playFraction: zero-char chunks do not produce NaN', () => {
    const chunks = [c('downloaded', 0), c('downloaded', 10)];
    const f = playFraction(chunks, 0);
    assert.equal(f.start, 0);
    assert.equal(f.end, 0);
});
