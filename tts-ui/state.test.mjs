// ── state.test.mjs — unit tests for state machine transitions ─────────────
// Run: node --no-warnings state.test.mjs
import assert from 'node:assert/strict';
import { dispatch, getState, downloadedCount, chunkStatus, progressPct } from './static/state.js';

let passed = 0;
let failed = 0;

function test(name, fn) {
    // Reset before each test
    dispatch({ type: 'RESET' });
    try {
        fn();
        passed++;
        console.log(`  ✓ ${name}`);
    } catch (e) {
        failed++;
        console.error(`  ✗ ${name}`);
        console.error(`    ${e.message}`);
    }
}

// Helper: simulate a stream with N chunks arriving
function setupStream(nChunks, texts = undefined) {
    dispatch({ type: 'START_STREAM' });
    dispatch({ type: 'SET_TOTAL', count: nChunks });
    const chunks = texts || Array.from({ length: nChunks }, (_, i) => `chunk ${i}`);
    const charCounts = chunks.map(t => t.length);
    dispatch({ type: 'METADATA', chunks, charCounts });
}

function sendPart(idx) {
    dispatch({ type: 'AUDIO_PART', chunkIdx: idx, bytes: new Uint8Array(1024), url: `blob:test-${idx}` });
}

// ────────────────────────────────────────────────────────────────────────────
console.log('\nInvariants:');

test('I1: STREAM_DONE does NOT set finished=true', () => {
    setupStream(3);
    sendPart(0);
    dispatch({ type: 'STREAM_DONE' });
    assert.equal(getState().finished, false, 'finished should remain false after STREAM_DONE');
});

test('I1: CHUNK_ENDED on last chunk sets finished=true', () => {
    setupStream(2);
    sendPart(0);
    sendPart(1);
    dispatch({ type: 'CHUNK_ENDED' }); // chunk 0 ends → advance to 1
    assert.equal(getState().finished, false, 'not yet — still have chunk 1');
    dispatch({ type: 'CHUNK_ENDED' }); // chunk 1 ends → no more chunks
    assert.equal(getState().finished, true, 'should be finished after last chunk ends');
});

test('I2: Once finished, AUDIO_PART is a no-op', () => {
    setupStream(1);
    sendPart(0);
    dispatch({ type: 'CHUNK_ENDED' }); // finishes (only 1 chunk)
    assert.equal(getState().finished, true);

    const before = JSON.stringify(getState());
    dispatch({ type: 'AUDIO_PART', chunkIdx: 5, bytes: new Uint8Array(10), url: 'blob:late' });
    const after = getState();
    assert.equal(after.chunks[0].state, 'downloaded', 'chunk 0 state unchanged');
    assert.equal(after.chunks[5], undefined, 'no new chunk added');
});

test('I3: PLAY_AT requires target chunk has audio loaded', () => {
    setupStream(2);
    sendPart(0);
    // Chunk 1 has no audio yet
    dispatch({ type: 'PLAY_AT', idx: 1 });
    assert.equal(getState().playIdx, 0, 'should not seek to unloaded chunk');

    // Now load it and try again
    sendPart(1);
    dispatch({ type: 'PLAY_AT', idx: 1 });
    assert.equal(getState().playIdx, 1, 'should seek once audio is loaded');
});


test('SEEK_AT: allows an unloaded target (unlike PLAY_AT)', () => {
    setupStream(4);
    sendPart(0);
    // Chunk 3 has no audio yet — SEEK_AT must still move the cursor
    dispatch({ type: 'SEEK_AT', idx: 3 });
    const s = getState();
    assert.equal(s.playIdx, 3, 'cursor moves to unloaded target');
    assert.equal(s.resumeAt, 3, 'fetch cursor follows');
    assert.equal(s.statusText, 'Fetching chunk 4…', 'status tells user it is fetching');
});

test('SEEK_AT: preserves cached chunks (no re-download on seek-back)', () => {
    setupStream(4);
    sendPart(0); sendPart(1);
    dispatch({ type: 'SEEK_AT', idx: 2 });
    dispatch({ type: 'SEEK_AT', idx: 0 });
    const s = getState();
    assert.equal(s.playIdx, 0);
    assert.ok(s.chunks[0].audio && s.chunks[1].audio, 'cached chunks survive the seek');
    assert.equal(s.statusText, 'Chunk 1', 'cached target reports ready, not fetching');
});

test('SEEK_AT: un-finishes a completed playback', () => {
    setupStream(2);
    sendPart(0); sendPart(1);
    dispatch({ type: 'CHUNK_ENDED' }); // -> 1
    dispatch({ type: 'CHUNK_ENDED' }); // -> finished
    assert.equal(getState().finished, true);
    dispatch({ type: 'SEEK_AT', idx: 0 });
    const s = getState();
    assert.equal(s.finished, false, 'seeking revives playback');
    assert.equal(s.isPaused, false, 'and is not paused');
    assert.equal(s.chainPlaying, true, 'auto-continues forward');
});

test('SEEK_AT: out-of-range index is a no-op', () => {
    setupStream(3);
    sendPart(0);
    dispatch({ type: 'SEEK_AT', idx: 9 });
    assert.equal(getState().playIdx, 0, 'bogus target ignored');
    dispatch({ type: 'SEEK_AT', idx: -1 });
    assert.equal(getState().playIdx, 0, 'negative ignored');
});
test('I4: First AUDIO_PART auto-starts playback', () => {
    setupStream(3);
    sendPart(0);
    const s = getState();
    assert.equal(s.playIdx, 0, 'playback should start at chunk 0');
    assert.equal(s.chainPlaying, true, 'chain should be playing');
});

test('I4: Subsequent AUDIO_PART does not stop active playback', () => {
    setupStream(3);
    sendPart(0); // starts playback
    dispatch({ type: 'SET_PAUSED', paused: false }); // ensure playing state
    assert.equal(getState().chainPlaying, true);

    sendPart(1); // arrives while chunk 0 is "playing"
    const s = getState();
    assert.equal(s.playIdx, 0, 'should still be on chunk 0');
    assert.equal(s.chainPlaying, true, 'chain should not have been interrupted');
});

// ────────────────────────────────────────────────────────────────────────────
console.log('\nNormal flow:');

test('Full happy path: stream → auto-play → sequential chunks → done', () => {
    setupStream(3);
    sendPart(0); // auto-start at 0
    assert.equal(getState().playIdx, 0);

    sendPart(1); // arrives in background
    assert.equal(downloadedCount(), 2);

    dispatch({ type: 'CHUNK_ENDED' }); // chunk 0 done → advance to 1
    assert.equal(getState().playIdx, 1);

    sendPart(2); // last part arrives
    assert.equal(downloadedCount(), 3);

    dispatch({ type: 'CHUNK_ENDED' }); // chunk 1 done → advance to 2
    assert.equal(getState().playIdx, 2);

    dispatch({ type: 'STREAM_DONE' }); // stream complete — should NOT affect playback
    assert.equal(getState().finished, false, 'still playing chunk 2');

    dispatch({ type: 'CHUNK_ENDED' }); // chunk 2 done → no more
    assert.equal(getState().finished, true);
});

test('Pause and resume', () => {
    setupStream(2);
    sendPart(0);
    sendPart(1);
    dispatch({ type: 'CHUNK_ENDED' }); // advance to 1

    dispatch({ type: 'SET_PAUSED', paused: true });
    let s = getState();
    assert.equal(s.isPaused, true);
    assert.equal(s.chainPlaying, false);
    assert.equal(s.playIdx, 1, 'playIdx preserved during pause');

    dispatch({ type: 'SET_PAUSED', paused: false });
    s = getState();
    assert.equal(s.isPaused, false);
    assert.equal(s.chainPlaying, true);
});

test('Seek with PLAY_AT restarts from specified chunk', () => {
    setupStream(3);
    sendPart(0);
    sendPart(1);
    sendPart(2);
    dispatch({ type: 'CHUNK_ENDED' }); // now at 1
    assert.equal(getState().playIdx, 1);

    dispatch({ type: 'PLAY_AT', idx: 0 });
    const s = getState();
    assert.equal(s.playIdx, 0);
    assert.equal(s.chainPlaying, true);
    assert.equal(s.finished, false);
});

test('CHUNK_ENDED when not playing is a no-op', () => {
    setupStream(2);
    // Don't send any parts — playback never starts (playIdx stays -1)
    dispatch({ type: 'CHUNK_ENDED' });
    assert.equal(getState().finished, false, 'should not finish if never started');
});

// ────────────────────────────────────────────────────────────────────────────
console.log('\nResume at chunk:');

test('resumeAt=0 behaves as normal auto-start', () => {
    dispatch({ type: 'START_STREAM', resumeAt: 0 });
    dispatch({ type: 'SET_TOTAL', count: 3 });
    dispatch({ type: 'METADATA', chunks: ['a', 'b', 'c'], charCounts: [1, 1, 1] });
    sendPart(0);
    assert.equal(getState().playIdx, 0);
});

test('resumeAt>0 suppresses auto-start before the target', () => {
    dispatch({ type: 'START_STREAM', resumeAt: 2 });
    dispatch({ type: 'SET_TOTAL', count: 3 });
    dispatch({ type: 'METADATA', chunks: ['a', 'b', 'c'], charCounts: [1, 1, 1] });
    sendPart(0);
    assert.equal(getState().playIdx, -1, 'must not start before target');
    sendPart(1);
    assert.equal(getState().playIdx, -1, 'still before target');
});

test('resumeAt>0 starts playback exactly at the target', () => {
    dispatch({ type: 'START_STREAM', resumeAt: 2 });
    dispatch({ type: 'SET_TOTAL', count: 4 });
    dispatch({ type: 'METADATA', chunks: ['a', 'b', 'c', 'd'], charCounts: [1, 1, 1, 1] });
    sendPart(0);
    sendPart(1);
    sendPart(2);
    const s = getState();
    assert.equal(s.playIdx, 2, 'should start at the resume target');
    assert.equal(s.chainPlaying, true);
});

test('parts before resume target are still buffered', () => {
    dispatch({ type: 'START_STREAM', resumeAt: 2 });
    dispatch({ type: 'SET_TOTAL', count: 3 });
    dispatch({ type: 'METADATA', chunks: ['a', 'b', 'c'], charCounts: [1, 1, 1] });
    sendPart(0);
    sendPart(1);
    const s = getState();
    assert.ok(s.chunks[0].audio, 'chunk 0 buffered');
    assert.ok(s.chunks[1].audio, 'chunk 1 buffered');
    assert.equal(s.playIdx, -1);
});

test('START_STREAM without resumeAt defaults to 0', () => {
    dispatch({ type: 'START_STREAM' });
    assert.equal(getState().resumeAt, 0);
});

// ────────────────────────────────────────────────────────────────────────────
console.log('\nNavigation:');

test('GOTO_LIBRARY switches to library mode', () => {
    dispatch({ type: 'GOTO_LIBRARY' });
    assert.equal(getState().mode, 'library');
});

test('GOTO_INPUT returns to input mode', () => {
    dispatch({ type: 'GOTO_LIBRARY' });
    dispatch({ type: 'GOTO_INPUT' });
    assert.equal(getState().mode, 'input');
});

test('library visit does not disturb playback state', () => {
    setupStream(2);
    sendPart(0);
    dispatch({ type: 'GOTO_LIBRARY' });
    const s = getState();
    assert.equal(s.mode, 'library');
    assert.equal(s.playIdx, 0, 'playback index preserved');
    assert.equal(downloadedCount(), 1, 'received parts preserved');
});

test('START_STREAM always enters play mode, never library', () => {
    dispatch({ type: 'GOTO_LIBRARY' });
    dispatch({ type: 'START_STREAM' });
    assert.equal(getState().mode, 'play');
});

test('RESET returns to input mode', () => {
    dispatch({ type: 'GOTO_LIBRARY' });
    dispatch({ type: 'RESET' });
    assert.equal(getState().mode, 'input');
});

// ────────────────────────────────────────────────────────────────────────────
console.log('\nErrors:');

test('RETRY_FAILED sets error state and marks the chunk', () => {
    setupStream(2);
    sendPart(0);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 1, code: 503 });
    const s = getState();
    assert.ok(s.error.includes('Chunk 2'));
    assert.equal(s.chunks[1].state, 'errored');
});

test('STREAM_ERROR sets error', () => {
    setupStream(2);
    dispatch({ type: 'STREAM_ERROR', msg: 'Network error' });
    assert.equal(getState().error, 'Network error');
});

// ────────────────────────────────────────────────────────────────────────────
console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed\n`);

// ────────────────────────────────────────────────────────────────────────────
console.log('\nResume-marker protection: failed/truncated streams never finish:');

test('Truncated stream: last played chunk ended, total not reached -> NOT finished', () => {
    setupStream(5);
    sendPart(0); sendPart(1); sendPart(2); // network cut after 3 of 5
    dispatch({ type: 'CHUNK_ENDED' }); // 0 -> 1
    dispatch({ type: 'CHUNK_ENDED' }); // 1 -> 2
    dispatch({ type: 'STREAM_DONE' }); // stream ends early
    dispatch({ type: 'CHUNK_ENDED' }); // chunk 2 ends, nothing more coming
    const s = getState();
    assert.equal(s.finished, false, 'truncated stream must not mark finished');
    assert.equal(s.playIdx, 2, 'playback position preserved for resume');
});

test('Full stream: last played chunk ended with all parts -> finished', () => {
    setupStream(3);
    sendPart(0); sendPart(1); sendPart(2);
    dispatch({ type: 'CHUNK_ENDED' });
    dispatch({ type: 'CHUNK_ENDED' });
    dispatch({ type: 'CHUNK_ENDED' });
    assert.equal(getState().finished, true, 'complete listen still finishes');
});

test('RETRY_FAILED mid-stream does not finish (marker survives)', () => {
    setupStream(3);
    sendPart(0);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 1, code: 503 });
    dispatch({ type: 'CHUNK_ENDED' }); // chunk 0 ends, chunk 1 errored, nothing coming
    assert.equal(getState().finished, false, 'backend failure must not mark finished');
});

test('Early-end with client-known chunk list DOES finish (total is always known)', () => {
    dispatch({ type: 'START_STREAM' });
    dispatch({ type: 'METADATA', chunks: ['a'], charCounts: [1] });
    sendPart(0);
    dispatch({ type: 'CHUNK_ENDED' });
    // Client-side chunking knows every chunk upfront (chunks.length), so a
    // session with all chunks downloaded is provably complete.
    assert.equal(getState().finished, true, 'all chunks known and downloaded → finished');
});

// ────────────────────────────────────────────────────────────────────────────
console.log('\nPer-chunk fetch (chunkedPlayback):');

// Local test runner override: dispatch is imported per-run; RESET works.
test('RETRY_SCHEDULED sets status, does not set error, re-marks chunk downloading', () => {
    setupStream(3);
    dispatch({ type: 'RETRY_SCHEDULED', chunkIdx: 1, code: 500, attempt: 1 });
    const s = getState();
    assert.equal(s.error, null);
    assert.ok(s.statusText.includes('retry 1'));
    assert.equal(s.chunks[1].state, 'downloading');
});

test('RETRY_FAILED marks chunk error, not finished', () => {
    setupStream(3);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 1, code: 503 });
    const s = getState();
    assert.equal(s.chunks[1].state, 'errored');
    assert.ok(s.error.includes('Chunk 2'));
    assert.equal(s.finished, false); // I1: only CHUNK_ENDED completes
});

test('failed chunk does not stop playback: AUDIO_PART after RETRY_FAILED continues', () => {
    setupStream(3);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 0, code: 500 });
    sendPart(1); // chunk 0 failed; auto-start at first available part
    const s = getState();
    assert.equal(s.playIdx, 1);
    assert.ok(s.chainPlaying);
});

test('RETRY_FAILED out-of-range idx is a no-op', () => {
    setupStream(2);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 99, code: 500 });
    const s = getState();
    assert.ok(!s.chunks[99]);
    assert.ok(!s.error || !s.error.includes('100'));
});

test('skipped chunk: total counts still prove completeness only from parts', () => {
    setupStream(2);
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 0, code: 500 });
    sendPart(1);
    dispatch({ type: 'CHUNK_ENDED' }); // playing chunk 1 ended; chunk 0 never arrived
    const s = getState();
    assert.equal(s.finished, false); // truncated: not all chunks downloaded
    assert.ok(s.statusText.includes('early') || s.statusText.includes('1/2'));
});

test('RETRY actions while mode=library are harmless', () => {
    setupStream(2);
    dispatch({ type: 'GOTO_LIBRARY' });
    dispatch({ type: 'RETRY_SCHEDULED', chunkIdx: 0, code: 500, attempt: 2 });
    dispatch({ type: 'RETRY_FAILED', chunkIdx: 0, code: 500 });
    assert.equal(getState().mode, 'library'); // I: navigation not disturbed
});
// ────────────────────────────────────────────────────────────────────────────
console.log('\nUnified chunk model:');

test('every chunk starts pending, dense array sized to the session', () => {
    setupStream(3);
    const s = getState();
    assert.equal(s.chunks.length, 3);
    assert.ok(s.chunks.every(c => c.state === 'pending' && c.audio === null));
});

test('AUDIO_PART moves a chunk pending -> downloaded and stores audio', () => {
    setupStream(2);
    sendPart(0);
    const s = getState();
    assert.equal(s.chunks[0].state, 'downloaded');
    assert.ok(s.chunks[0].audio);
    assert.equal(s.chunks[1].state, 'pending');
});

test('CHUNK_FETCHING marks a chunk downloading (target + lookahead)', () => {
    setupStream(3);
    dispatch({ type: 'CHUNK_FETCHING', idx: 1 });
    dispatch({ type: 'CHUNK_FETCHING', idx: 2 });
    const s = getState();
    assert.equal(s.chunks[1].state, 'downloading');
    assert.equal(s.chunks[2].state, 'downloading');
    assert.equal(s.chunks[0].state, 'pending');
});

test('INVARIANT: a chunk cannot be active unless downloaded', () => {
    setupStream(3);
    dispatch({ type: 'SEEK_AT', idx: 1 }); // seek to an unloaded, pending chunk
    const s = getState();
    assert.equal(s.playIdx, 1);
    // The sought chunk has no audio yet -> it must NOT read as "active".
    assert.notEqual(chunkStatus(1), 'active');
    sendPart(1); // now it arrives
    assert.equal(getState().chunks[1].state, 'downloaded');
});

test('SEEK_AT to a pending chunk keeps other downloaded chunks intact', () => {
    setupStream(4);
    sendPart(0);
    sendPart(1);
    dispatch({ type: 'SEEK_AT', idx: 3 });
    const s = getState();
    assert.equal(s.chunks[0].state, 'downloaded', 'earlier chunk preserved');
    assert.equal(s.chunks[1].state, 'downloaded', 'earlier chunk preserved');
    assert.equal(s.chunks[3].state, 'pending', 'target not yet fetched');
    assert.equal(s.playIdx, 3);
});

test('downloadedCount and progressPct derive from chunk states', () => {
    setupStream(4);
    sendPart(0);
    sendPart(2);
    assert.equal(downloadedCount(), 2);
    assert.equal(progressPct(), 50);
});

test('CHUNK_FETCH_RESET clears stuck downloading rows back to pending', () => {
    setupStream(4);
    dispatch({ type: 'CHUNK_FETCHING', idx: 0 });
    dispatch({ type: 'CHUNK_FETCHING', idx: 1 });
    sendPart(0); // 0 arrives -> downloaded
    dispatch({ type: 'CHUNK_FETCH_RESET' }); // seek aborts the pump
    const s = getState();
    assert.equal(s.chunks[0].state, 'downloaded', 'downloaded chunk preserved across seek');
    assert.equal(s.chunks[1].state, 'pending', 'aborted in-flight chunk is not left spinning');
    assert.equal(chunkStatus(1), 'pending');
});


console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
