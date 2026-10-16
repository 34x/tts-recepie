// Tests for settingsPin — fingerprint stability + invalidation semantics
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computePin, pinMatches } from './settingsPin.js';

test('pin is stable for same settings', () => {
    const a = computePin({ backend: 'piper', model: '', voice: 'af_heart', instruct: '' });
    const b = computePin({ backend: 'piper', model: '', voice: 'af_heart', instruct: '' });
    assert.equal(a, b);
    assert.match(a, /^[0-9a-f]{8}$/);
});

test('pin differs on any audio-affecting field change', () => {
    const base = { backend: 'kokoro', model: 'm1', voice: 'af_heart', instruct: '' };
    const variants = [
        { ...base, backend: 'supertonic' },
        { ...base, model: 'm2' },
        { ...base, voice: 'bf_gemma' },
        { ...base, instruct: 'cheerful' },
    ];
    const p0 = computePin(base);
    for (const v of variants) assert.notEqual(computePin(v), p0, JSON.stringify(v));
});

test('empty settings produce empty pin (nothing pinned)', () => {
    assert.equal(computePin({}), '');
    assert.equal(computePin({ backend: '', voice: '' }), '');
});

test('pinMatches requires both sides present and equal', () => {
    const p = computePin({ voice: 'x' });
    assert.equal(pinMatches(p, p), true);
    assert.equal(pinMatches(p, computePin({ voice: 'y' })), false);
    assert.equal(pinMatches('', p), false);  // legacy/missing pin → not reusable
    assert.equal(pinMatches(p, ''), false);
});