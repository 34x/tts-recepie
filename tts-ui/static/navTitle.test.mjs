// ── navTitle.test.mjs — document.title derivation ───────────────────────────
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deriveTitle, progressSuffix } from './navTitle.js';

test('deriveTitle: first non-empty line, markdown stripped', () => {
    assert.equal(deriveTitle('# Chapter One\n\nbody text'), 'Chapter One');
    assert.equal(deriveTitle('\n\n   \nHello world'), 'Hello world');
    assert.equal(deriveTitle('> A quote\nnext'), 'A quote');
    assert.equal(deriveTitle('- item one\n- item two'), 'item one');
    assert.equal(deriveTitle('1. first\n2. second'), 'first');
});

test('deriveTitle: inline emphasis / code removed', () => {
    assert.equal(deriveTitle('**Bold** start'), 'Bold start');
    assert.equal(deriveTitle('an *emphatic* word'), 'an emphatic word');
    assert.equal(deriveTitle('a `code` here'), 'a code here');
});

test('deriveTitle: skips horizontal rules and blank markdown lines', () => {
    assert.equal(deriveTitle('---\n# Real title'), 'Real title');
    assert.equal(deriveTitle('***\n***\nEnd'), 'End');
});

test('deriveTitle: collapses whitespace', () => {
    assert.equal(deriveTitle('  lots   of\t space  '), 'lots of space');
});

test('deriveTitle: truncates with ellipsis', () => {
    const long = 'a'.repeat(100);
    const out = deriveTitle(long, 10);
    assert.equal(out.length, 10);
    assert.ok(out.endsWith('\u2026'));
    assert.equal(out, 'a'.repeat(9) + '\u2026');
});

test('deriveTitle: empty / non-usable input', () => {
    assert.equal(deriveTitle(''), '');
    assert.equal(deriveTitle('   \n\t\n'), '');
    assert.equal(deriveTitle(null), '');
    assert.equal(deriveTitle('---'), '');
});

test('progressSuffix: play mode only', () => {
    const st = (mode, i, n) => ({ mode, playIdx: i, chunks: Array.from({ length: n }, () => ({})) });
    assert.equal(progressSuffix(st('play', 0, 40)), ' \u00B7 1/40');
    assert.equal(progressSuffix(st('play', 11, 40)), ' \u00B7 12/40');
    assert.equal(progressSuffix(st('input', 0, 0)), '');
    assert.equal(progressSuffix(st('library', 0, 5)), '');
    assert.equal(progressSuffix(st('play', 0, 0)), '');
    assert.equal(progressSuffix(null), '');
});

test('progressSuffix: clamps out-of-range index', () => {
    const st = { mode: 'play', playIdx: 99, chunks: [{}, {}] };
    assert.equal(progressSuffix(st), ' \u00B7 2/2');
});
