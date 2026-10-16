import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalize, createSettingsStore, DEFAULTS, SETTINGS_KEY } from './settings.js';

// Minimal in-memory Storage stand-in.
function memStorage(initial = {}) {
    const map = new Map(Object.entries(initial));
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => { map.set(k, String(v)); },
        _raw: map,
    };
}

test('normalize: empty/absent blob yields defaults', () => {
    assert.deepEqual(normalize(null), { ...DEFAULTS });
    assert.deepEqual(normalize(undefined), { ...DEFAULTS });
    assert.deepEqual(normalize({}), { ...DEFAULTS });
});

test('normalize: known keys override defaults', () => {
    assert.deepEqual(
        normalize({ pasteStream: true }),
        { ...DEFAULTS, pasteStream: true });
});

test('normalize: junk values fall back to defaults, extra keys dropped', () => {
    const out = normalize({ pasteStream: null, bogus: true });
    assert.deepEqual(out, { ...DEFAULTS });
    assert.equal('bogus' in out, false);
});

test('normalize: string/number booleans are coerced', () => {
    const out = normalize({ pasteStream: '1' });
    assert.equal(out.pasteStream, true, 'coerced from "1"');
    assert.equal(out.normalizeText, DEFAULTS.normalizeText, 'untouched default preserved');
});

test('store: defaults when storage empty', () => {
    const s = createSettingsStore(memStorage());
    assert.deepEqual(s.get(), { ...DEFAULTS });
});

test('store: set() persists partial and round-trips', () => {
    const storage = memStorage();
    const s = createSettingsStore(storage);
    const next = s.set({ pasteStream: true });
    assert.equal(next.pasteStream, true);
    assert.deepEqual(Object.keys(next), Object.keys(DEFAULTS), 'no foreign keys introduced');

    const raw = JSON.parse(storage._raw.get(SETTINGS_KEY));
    assert.equal(raw.pasteStream, true);

    // A fresh store over the same storage sees the persisted value.
    assert.equal(createSettingsStore(storage).get().pasteStream, true);
});

test('store: malformed JSON degrades to defaults, does not throw', () => {
    const storage = memStorage({ [SETTINGS_KEY]: '{not json' });
    const s = createSettingsStore(storage);
    assert.deepEqual(s.get(), { ...DEFAULTS });
});

test('store: null storage (private mode) never throws', () => {
    const s = createSettingsStore(null);
    assert.deepEqual(s.get(), { ...DEFAULTS });
    assert.deepEqual(s.set({ pasteStream: true }).pasteStream, true);
});
