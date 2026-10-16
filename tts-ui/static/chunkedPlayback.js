// ── chunkedPlayback.js — per-chunk fetch orchestration (third mode) ──────────
// The FE chunks the text (chunker.js) and requests ONE chunk at a time via
// POST /api/audio/speech (OpenAI-shaped, OpenAI-compatible passthrough in
// server.py which resolves the backend server-side — descriptor fields like
// `instructions`/`send_model`/`format` never reach the client).
// Deliberately isolated from playback.js's stream coroutine: no shared
// streamGen/waitingForNext machinery. Audio decisions stay in playback.js
// (sendPart/beginChunked). Failure semantics: bounded retry with backoff per
// chunk, then mark unavailable (skipped) — never fake silence, never abort
// the whole stream (unlike the multipart path).

import { chunkText } from './chunker.js';
import { dispatch, getState, downloadedCount } from './state.js';
import { beginChunked, sendPart, rearmChunked, completeChunked } from './playback.js';
import { computePin } from './settingsPin.js';

const API = '/api/audio/speech';
const MAX_ATTEMPTS = 3;      // 1 initial + 2 retries
const BACKOFF_MS = [500, 2000];
const CONCURRENCY = 1;       // max in-flight chunk requests (1 = strictly one after another)

// ── Session state (module-level: one chunked session at a time) ──────────────
// The pump is restartable: seekChunked() aborts the in-flight requests of the
// current pump (network-level cancel — no orphaned downloads) and starts a new
// pump at the selected index, running forward to the end. Already-downloaded
// chunks are skipped, so seek-back costs no traffic.
let _session = null;   // { text, voice, model, instruct, backend, chunks, pin }
let _pumpCtrl = null;  // AbortController for the current pump's in-flight fetches
let _pumpGen = 0;      // generation — stale pumps observe this and exit
let _pumpStart = -1;   // first index the LIVE pump is filling from (-1 = none)

/**
 * True when the live pump is filling the forward range that contains `idx`.
 * The pump always runs [start → end], skipping cached chunks, so any index
 * >= start is (or will be) downloaded by it. Indices before start are NOT
 * covered — e.g. seeking back from 12 to 3 leaves 4…11 as a hole the pump
 * (starting at 12) will never fill. Callers use this to decide whether a
 * cached target can be played as-is or needs the pump retargeted.
 */
export function pumpCovers(idx) {
    return _pumpStart >= 0 && idx >= _pumpStart;
}

/** Abort the current pump (if any). Called on seek, and on session end. */
function _abortPump() {
    _pumpGen++;
    if (_pumpCtrl) { try { _pumpCtrl.abort(); } catch {} _pumpCtrl = null; }
    // In-flight chunks are now dead (their AUDIO_PART will never arrive).
    // Clear their 'downloading' spinners back to 'pending' so no row is stuck
    // "generating" after a seek. No-op when nothing was in flight.
    dispatch({ type: 'CHUNK_FETCH_RESET' });
}

/**
 * Retarget the download+playback cursor to `idx`. Aborts in-flight fetches
 * from the previous cursor and starts a fresh pump at idx → end. Cached
 * chunks (< idx, or later ones already fetched) are preserved and skipped.
 */
export function seekChunked(idx) {
    if (!_session) return;
    const chunks = _session.chunks;
    const target = Math.max(0, Math.min(chunks.length - 1, Math.trunc(idx) || 0));
    dispatch({ type: 'SEEK_AT', idx: target });
    rearmChunked(target);   // move audio cursor (instant if cached)
    _abortPump();
    const gen = _pumpGen;
    // isLast: the last *chunk index in the whole text* — sendPart uses it to
    // close the session, so a partial pump (target → end) still finishes when
    // the text's final chunk lands.
    _pump(target, gen, chunks.length - 1);
}

export function startChunked(opts) {
    const {
        text, voice, model, instruct, backend,
        getResumeAt,    // () -> first chunk index to fetch/play (client concern)
    } = opts;

    const chunks = chunkText(text);
    if (!chunks.length) {
        dispatch({ type: 'STREAM_ERROR', msg: 'Nothing to synthesize.' });
        return;
    }

    // Order matters: START_STREAM (dispatched inside beginChunked) resets the
    // whole state, so METADATA/SET_TOTAL must come after it or the chunk rows
    // get wiped.
    const pin = computePin({ backend, model, voice, instruct });
    const resume = getResumeAt ? Math.max(0, Math.trunc(getResumeAt()) || 0) : 0;
    beginChunked(resume, pin); // dispatches START_STREAM + resets

    // Total up front so render/seek show the full timeline immediately
    dispatch({ type: 'METADATA', chunks, charCounts: null }); // chunk list rows + statuses
    dispatch({ type: 'SET_TOTAL', count: chunks.length });

    _session = { text, voice, model, instruct, backend, chunks, pin };
    _abortPump();                    // cancel any previous session's pump
    const gen = _pumpGen;            // capture this session's generation
    _pump(resume, gen, chunks.length - 1);
}

/**
 * Fetch chunks sequentially from `start` to `last`, feeding each via sendPart.
 * Runs at most CONCURRENCY requests in flight at once; already-cached indices
 * are skipped without a network call. A pump becomes stale when _pumpGen changes
 * (seek/new session) — it then stops and its in-flight requests are aborted
 * by _abortPump().
 */
function _pump(start, gen, last) {
    const { chunks, voice, model, instruct, backend, pin } = _session;
    const ctrl = new AbortController();
    _pumpCtrl = ctrl;
    _pumpStart = start;
    const stale = () => gen !== _pumpGen;

    const fetchChunk = async (idx) => {
        dispatch({ type: 'CHUNK_FETCHING', idx }); // row shows a spinner while in flight
        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            if (stale()) return null;
            try {
                const resp = await fetch(API, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ input: chunks[idx], voice, model, instruct, backend }),
                    signal: ctrl.signal,   // network-level cancel on seek
                });
                if (resp.ok) return await resp.blob();
                const code = resp.status;
                if (attempt < MAX_ATTEMPTS) {
                    dispatch({ type: 'RETRY_SCHEDULED', chunkIdx: idx, code, attempt });
                    await sleep(BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]);
                    continue;
                }
                dispatch({ type: 'RETRY_FAILED', chunkIdx: idx, code }); // mark unavailable, skip
                return null;
            } catch (e) {
                if (stale() || e.name === 'AbortError') return null;
                if (attempt < MAX_ATTEMPTS) {
                    dispatch({ type: 'RETRY_SCHEDULED', chunkIdx: idx, code: 0, attempt });
                    await sleep(BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]);
                    continue;
                }
                dispatch({ type: 'RETRY_FAILED', chunkIdx: idx, code: 0 });
                return null;
            }
        }
        return null;
    };

    // Windowed pipeline: keep up to CONCURRENCY requests ahead of the cursor.
    // CONCURRENCY=1 means strictly one fetch at a time — a seek starts exactly
    // one request (the target), not target+1.
    (async () => {
        let next = start;
        const inflight = new Map(); // idx -> Promise<blob|null>

        const launch = (i) => {
            if (i < start || i > last || i >= chunks.length) return;
            if (inflight.has(i)) return;
            if (getState().chunks[i]?.audio) return; // already downloaded — skip, no traffic
            inflight.set(i, fetchChunk(i));
        };
        launch(start);

        for (; next <= last; next++) {
            if (stale()) return;
            const p = inflight.get(next);
            inflight.delete(next);
            if (getState().chunks[next]?.audio) {
                // Already downloaded (e.g. seek-back): feed nothing, but keep
                // the window filled so the downloader runs forward.
                for (let k = 1; k <= CONCURRENCY; k++) launch(next + k);
                continue;
            }
            const blob = p ? await p : null;
            if (stale()) return;
            if (blob) {
                // Await: sendPart ingests the blob (arrayBuffer) and dispatches
                // AUDIO_PART. Without this the loop can reach the tail and call
                // completeChunked() while the final chunk's AUDIO_PART is still
                // pending -> STREAM_DONE with nothing downloaded.
                await sendPart(next, blob, next === last, pin);
                if (stale()) return;
            }
            // Fill the window from the tail
            for (let k = 1; k <= CONCURRENCY; k++) launch(next + k);
        }

        // Reached the end of the text. Only a pump that ran to the text's
        // final chunk completes the session; a stale pump exited above.
        // Idempotent: if the last chunk was already cached (and thus never fed
        // through sendPart), this is what closes the session.
        if (stale() || last !== chunks.length - 1) return;
        const s = getState();
        if (!downloadedCount(s) && !s.finished) {
            // Nothing ever arrived (all failed / nothing queued)
            dispatch({ type: 'STATUS', text: 'No chunks available — tap a chunk or re-Play' });
        }
        completeChunked();
    })().catch(e => { if (!stale()) dispatch({ type: 'STREAM_ERROR', msg: e.message }); });
}

/** Clear the session (used when leaving playback) so a later seek is a no-op. */
export function endChunked() {
    _abortPump();
    _pumpStart = -1;
    _session = null;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }