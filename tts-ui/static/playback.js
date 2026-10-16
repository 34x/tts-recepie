// ── playback.js — imperative audio control for the chunked session ──────────
// The FE chunks the text (chunker.js) and fetches ONE chunk at a time
// (chunkedPlayback.js). This module owns every audio decision: start,
// continue, finish. The player is driven by explicit commands at those
// decision points, NOT by subscribing to state diffs.
//
// Chunk audio lives in state.chunks[i].audio (blob URL + metadata); download
// state lives in state.chunks[i].state. See state.js for the model.

import { dispatch, getState } from './state.js';

const MIME = 'audio/mpeg';
// Build marker — if this line is missing from the console, you are running
// STALE code (service worker cache). Every code change bumps this version.
console.log('[tts-build] playback.js v7-chunked');

let player = null;          // <audio> element (set via init)
let t0 = 0;                 // session start timestamp (for timing logs)
let waitingForNext = false; // onended fired, next chunk not yet downloaded
let streamComplete = false; // ingestion finished (success or error)
// Chunked-session state (fed via sendPart, see beginChunked)
let chunkedStartAt = 0;       // first chunk to auto-play
let chunkedAudioStarted = false;
let chunkedFinish = null;     // completion callback set by beginChunked

export function init(audioEl) {
    player = audioEl;

    // Reconcile the media element's REAL state into app state so the UI can't
    // get stuck showing "playing" when the browser pauses it on its own while
    // the tab is visible (resume() then works, since isPaused flips back).
    // Feeds the existing SET_PAUSED reducer (already guards on a live session),
    // so no new state/flag is added. We reconcile in BOTH directions: a
    // seek/restart internally pauses the element before swapping src and
    // replaying, and that internal pause would otherwise leave the icon stuck
    // on "play" until the next dispatch — the 'playing' event fired by the
    // replay restores it, so listening to both keeps the icon faithful.
    player.addEventListener('pause', () => {
        dispatch({ type: 'SET_PAUSED', paused: true });
    });
    player.addEventListener('playing', () => {
        dispatch({ type: 'SET_PAUSED', paused: false });
    });
}

// ── Internal: low-level audio element control ───────────────────────────────

function _startAudioAt(i) {
    const state = getState();
    const audio = state.chunks[i]?.audio;
    if (!audio) return;

    player.pause();
    // Do NOT revoke blob URLs here — chunks may be replayed later.
    // URLs are cleaned up only on stopAll() / new session start.
    player.src = audio.url;
    player.onended = () => _onChunkEnded(i);
    console.log(`[tts-timing] CHUNK_STARTED_PLAY idx=${i} +${(performance.now() - t0).toFixed(0)}ms`);
    console.log(`[playback] ▶ chunk ${i}`);
    player.play().catch(e => console.warn('[playback] play() rejected:', e.message));
}

function _stopAudio() {
    player.pause();
    if (player.src && player.src.startsWith('blob:')) {
        URL.revokeObjectURL(player.src);
    }
    player.removeAttribute('src');
    player.load();
}

// Pause and detach the current source WITHOUT revoking its blob URL. Used on
// seek: the old chunk must stop immediately, but it stays cached/playable
// (D1.1 preserves downloaded audio across seeks).
function _haltAudio() {
    player.pause();
    player.onended = null;
    player.removeAttribute('src');
    player.load();
}

// Decision point: current chunk finished. Advance or finish?
function _onChunkEnded(endedIdx) {
    const s = getState();
    // Stale-end guard: a seek moves playIdx while the OLD chunk's audio may
    // still be playing. When that old chunk ends, endedIdx is no longer the
    // active chunk — advancing from it (endedIdx+1) would hijack playback
    // back to a chunk the user already seeked away from.
    if (endedIdx !== s.playIdx) {
        console.log(`[playback] ignoring stale onended chunk=${endedIdx} playIdx=${s.playIdx}`);
        return;
    }
    const next = endedIdx + 1;
    console.log(`[playback] ■ chunk ${endedIdx} ended, playIdx=${s.playIdx}`);

    if (s.chunks[next]?.audio) {
        // Next chunk is already downloaded — advance state and start audio together
        dispatch({ type: 'PLAY_AT', idx: next });
        _startAudioAt(next);
    } else if (!s.finished && !streamComplete) {
        // Downloader still working, next chunk hasn't arrived yet — wait for it
        waitingForNext = true;
        console.log(`[playback] ⏳ waiting for chunk ${next}`);
        dispatch({ type: 'STATUS', text: `Waiting for chunk ${next + 1}…` });
    } else {
        // No more chunks coming — mark finished via state machine
        dispatch({ type: 'CHUNK_ENDED' });
        console.log(`[playback] ■ session done after chunk ${endedIdx}`);
    }
}

// ── Public imperative API — called from app.js at explicit decision points ───

/** Start (or restart) playback at a specific chunk index (audio must exist). */
export function playAt(idx) {
    const s = getState();
    if (idx < 0 || !s.chunks[idx]?.audio) return; // I3: no audio at target

    waitingForNext = false;
    dispatch({ type: 'PLAY_AT', idx });
    _startAudioAt(idx);
}

/** Pause current playback. */
export function pause() {
    const s = getState();
    if (s.isPaused || s.finished) return;
    player.pause();
    dispatch({ type: 'SET_PAUSED', paused: true });
    console.log('[playback] ⏸ paused');
}

/** Resume from pause. */
export function resume() {
    const s = getState();
    if (!s.isPaused || s.finished) return;
    player.play().catch(e => console.warn('[playback] resume rejected:', e.message));
    dispatch({ type: 'SET_PAUSED', paused: false });
    console.log('[playback] ▶ resumed');
}

/** Stop all audio and clean up media element. */
export function stopAll() {
    waitingForNext = false;
    _stopAudio();
}

// ── Part ingestion for the chunked session ──────────────────────────────────
// chunkedPlayback.js fetches chunks and feeds them here; audio start / continue
// / finish decisions stay in this file, so the imperative audio logic has
// exactly one owner.

export async function sendPart(idx, blob, isLast = false, pin = null) {
    if (!chunkedFinish) return; // no active chunked session
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const url = URL.createObjectURL(new Blob([bytes], { type: MIME }));
    dispatch({ type: 'AUDIO_PART', chunkIdx: idx, bytes, url, genTime: null, pin });

    const s = getState();
    console.log(`[tts-timing] CHUNK_ARRIVED(idxed) idx=${idx} +${(performance.now() - t0).toFixed(0)}ms`);

    if (waitingForNext && idx === s.playIdx + 1) {
        waitingForNext = false;
        dispatch({ type: 'PLAY_AT', idx });
        _startAudioAt(idx);
    } else if (!chunkedAudioStarted && idx >= chunkedStartAt) {
        chunkedAudioStarted = true;
        _startAudioAt(idx);
    }
    if (isLast) {
        completeChunked();
    }
}

// Idempotent session completion. Called by sendPart (final chunk arrived) and
// by the chunked pump when it reaches the end of the text — the latter covers
// the case where the final chunk was already downloaded and therefore never
// fed through sendPart.
export function completeChunked() {
    if (!chunkedFinish) return; // no session, or already completed
    const fin = chunkedFinish;
    chunkedFinish = null;
    streamComplete = true;
    dispatch({ type: 'STREAM_DONE', elapsedMs: performance.now() - t0 });
    if (waitingForNext) {
        waitingForNext = false;
        _onChunkEnded(getState().playIdx);
    }
    fin?.();
}

export function beginChunked(resumeAt = 0, pin = null) {
    waitingForNext = false;
    streamComplete = false;
    chunkedStartAt = Math.max(0, Math.trunc(resumeAt) || 0);
    chunkedAudioStarted = false;
    chunkedFinish = () => {}; // session is live until completeChunked() clears it

    for (const c of getState().chunks) {
        if (c.audio?.url) URL.revokeObjectURL(c.audio.url);
    }

    t0 = performance.now();
    dispatch({ type: 'START_STREAM', transport: 'chunked', statusText: 'Fetching chunks…', resumeAt: chunkedStartAt, pin });
}

/**
 * Retarget playback to `idx` (user tapped a chunk / the progress bar).
 * Called by chunkedPlayback.seekChunked() right after it moves the fetch
 * cursor. If the target audio is already downloaded, start it immediately;
 * otherwise sendPart()'s auto-start plays it the moment it arrives.
 * Blob URLs are intentionally NOT revoked — downloaded chunks stay playable.
 */
export function rearmChunked(idx) {
    if (!chunkedFinish) return; // no active chunked session
    const s = getState();
    const c = s.chunks[idx];
    if (!c) return;
    chunkedStartAt = idx;
    waitingForNext = false;
    if (c.audio) {
        chunkedAudioStarted = true; // suppress sendPart's auto-start for this idx
        _startAudioAt(idx);
    } else {
        chunkedAudioStarted = false;
        // Seek target not downloaded yet: stop the chunk that was playing.
        // Otherwise it keeps sounding (and its stale onended could fire) while
        // the user waits for the new target — playback must follow the seek.
        _haltAudio();
        dispatch({ type: 'STATUS', text: `Fetching chunk ${idx + 1}…` });
    }
}
