import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cleanText, stepNames, makeCleaner, STEPS } from './textNormalizer.js';

// Stable code points (avoid literal emoji in source — they shift under editing).
const FACE = String.fromCodePoint(0x1F300); // representative emoji-block code point
const PARTY = String.fromCodePoint(0x1F389);
const MAN = String.fromCodePoint(0x1F468); // 👨
const WOMAN = String.fromCodePoint(0x1F469); // 👩
const ZWJ = String.fromCodePoint(0x200D);
const ZWS = String.fromCodePoint(0x200B); // zero-width space (invisible)

test('system: cleanText is an ordered pipeline over named steps', () => {
    assert.equal(typeof cleanText, 'function');
    assert.deepEqual(stepNames(), ['v1_emoji']);
    // The extension contract: STEPS is an ordered [name, fn] list you can append to.
    assert.ok(Array.isArray(STEPS) && STEPS[0][0] === 'v1_emoji');
});

test('emoji / symbol code points are removed', () => {
    assert.ok(!cleanText(`hi ${FACE} there`).includes(FACE), 'face removed');
    assert.ok(!cleanText(`go ${PARTY} now`).includes(PARTY), 'party popper removed');
    // family glyph (emoji + ZWJ + emoji) is fully removed
    assert.equal(cleanText(`${MAN}${ZWJ}${WOMAN}`), '');
});

test('invisible format characters are removed', () => {
    const withZWS = `a${ZWS}b`;
    assert.ok(!cleanText(withZWS).includes(ZWS), 'zero-width space removed');
    assert.equal(cleanText(withZWS), 'ab');
});

test('all Unicode letters are preserved', () => {
    // These scripts have no emoji; they must come back byte-identical.
    const scripts = ['Привет мир', 'Hallo Welt', '你好世界', 'こんにちは', 'Ελληνικά', 'שלום עולם'];
    for (const s of scripts) assert.equal(cleanText(s), s, `preserved: ${s}`);
});

test('cleanText is idempotent', () => {
    const sample = `x ${FACE} ${PARTY} y ${MAN}${ZWJ}${WOMAN} z`;
    assert.equal(cleanText(sample), cleanText(cleanText(sample)), 'clean twice == clean once');
});

test('non-string / null input passes through unchanged', () => {
    assert.equal(cleanText(null), null);
    assert.equal(cleanText(undefined), undefined);
    assert.equal(cleanText(123), '123');
});

test('pipeline is extensible: a later rule drops in without disturbing v1_emoji', () => {
    // Baseline with the default pipeline.
    const base = cleanText('x ' + FACE + ' y');
    assert.ok(!base.includes(FACE));
    assert.equal(base, 'x  y');

    // Layer a new (idempotent) rule on top via makeCleaner — without mutating
    // the shared STEPS list. Existing behaviour must be preserved AND the new
    // rule must run on the already-cleaned text.
    const withFuture = makeCleaner([...STEPS, ['v2_upper', (t) => t.toUpperCase()]])('x ' + FACE + ' y');
    assert.ok(!withFuture.includes(FACE), 'emoji still stripped after adding a rule');
    assert.equal(withFuture, 'X  Y', 'new rule applied on top of cleaned text');

    // The default STEPS list is untouched.
    assert.deepEqual(stepNames(), ['v1_emoji']);
});
