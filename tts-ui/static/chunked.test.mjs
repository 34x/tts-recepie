// Static ordering test: beginChunked (which dispatches the resetting
// START_STREAM) must be called before METADATA/SET_TOTAL, or the chunk rows
// get wiped (render bug we hit in first wiring).
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

test('chunkedPlayback: beginChunked(START_STREAM) precedes METADATA + SET_TOTAL', async () => {
    const src = readFileSync(new URL('./chunkedPlayback.js', import.meta.url), 'utf8');
    const mBegin = /beginChunked\(resume, pin\)/.exec(src);
    const mMeta = /dispatch\(\{ type: 'METADATA', chunks/.exec(src);
    const mTotal = /dispatch\(\{ type: 'SET_TOTAL', count: chunks\.length \}\)/.exec(src);
    assert.ok(mBegin && mMeta && mTotal, 'all three calls present');
    assert.ok(mBegin.index < mMeta.index && mMeta.index < mTotal.index,
        'beginChunked must run before METADATA before SET_TOTAL');
});