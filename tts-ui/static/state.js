// ── state.js — central state + dispatch (no DOM, no side effects) ────────
//
// Chunk model: ONE dense array, one object per chunk. Download state and
// playback state are separate fields, so a chunk can never be "playing" and
// "not downloaded" at the same time.
//
//   chunks[i] = {
//     text,                       // chunk source text
//     chars,                      // its length (for duration estimates)
//     state: 'pending' | 'downloading' | 'downloaded' | 'errored',
//     audio: null | { url, kb, genTime, pin },   // blob URL once downloaded
//   }
//
// Invariants enforced by guards:
//   I1. `finished` may only be set to true by: CHUNK_ENDED when every chunk is
//       downloaded (truncated / errored sessions stop playback but keep the
//       resume marker)
//   I2. Once `finished`, stream actions are no-ops (stream is over)
//   I3. `PLAY_AT` requires target chunk audio to be loaded
//   I4. Stream-receiving actions may auto-start playback but never stop it

export const SPEECH_RATE = 15; // chars/sec for duration estimate
// Display class per chunk status (CSS keys: pending/generating/ready/active/done/error)
export const CHUNK_ICONS = { pending: '', generating: '', ready: '', active: '▶', done: '✓', error: '✕' };

// All UI icons in one place — change here to restyle globally
export const ICONS = {
    back: '\u2190',
    settings: '\u2699\uFE0F',
    close: '\u2715',
    clear: '\u2715',
    play: '\u25B6\uFE0F',
    pause: '\u23F8',
    prev: '\u23EE',
    next: '\u23ED',
    library: '\uD83D\uDCDA',
    delete: '\uD83D\uDDD1',
    download: '\u2B07',
};

function initialState() {
    return {
        mode: 'input',         // 'input' | 'library' | 'play'
        transport: 'chunked',  // 'chunked' | 'buffered' — how the session fetches audio
        chunks: [],            // dense [{text, chars, state, audio}] — the single source of truth
        playIdx: -1,           // currently active chunk index
        resumeAt: 0,           // first chunk index playback may auto-start at (0 = normal)
        isPaused: false,
        chainPlaying: false,   // playback chain is live (not paused/stopped)
        totalChunks: 0,        // number of chunks in the session
        finished: false,       // playback ended — terminal until RESET/START_STREAM
        statusText: '',        // top bar status text
        statsText: '',         // bottom stats line
        error: null,           // active error message (null = no error)
        totalBytes: 0,         // total audio bytes received (for debug)
        streamStart: null,     // performance.now() at stream start (for elapsed timing)
        pin: '',               // settings fingerprint of this session
    };
}

let state = initialState();
const listeners = [];

// ── Debug: ring buffer of recent actions (exported for UI) ────────────────
export const __actions = [];

export function getState() { return state; }

export function subscribe(fn) {
    listeners.push(fn);
    return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
}

function notify(prev) {
    for (const fn of listeners) fn(state, prev);
}

export function dispatch(action) {
    __actions.push(`[${performance.now().toFixed(0)}ms] ${action.type}` +
        (action.idx !== undefined ? ` idx=${action.idx}` : '') +
        (action.paused !== undefined ? ` paused=${action.paused}` : ''));
    if (__actions.length > 50) __actions.shift();

    const prev = state;
    if (action.type !== 'RESET' && action.type !== 'START_STREAM') {
        state = structuredClone(state);
    }

    switch (action.type) {
        // ── Lifecycle: always valid, resets everything ────────────────────────
        case 'RESET': {
            state = initialState();
            break;
        }

        case 'START_STREAM': {
            state = {
                ...initialState(),
                mode: 'play',
                transport: action.transport || 'chunked',
                statusText: action.statusText || 'Connecting…',
                resumeAt: action.resumeAt || 0,
                pin: action.pin || '',   // settings fingerprint of this session
            };
            break;
        }

        // ── Navigation: presentation-only, no playback side effects ─────────

        case 'GOTO_LIBRARY': {
            state.mode = 'library';
            break;
        }

        case 'GOTO_INPUT': {
            state.mode = 'input';
            break;
        }

        // ── Session setup ───────────────────────────────────────────────────

        case 'SET_TOTAL': {
            if (state.finished) break;
            state.totalChunks = action.count;
            break;
        }

        case 'METADATA': {
            if (state.finished) break;
            state.chunks = (action.chunks || []).map((t, i) => ({
                text: t.trim(),
                chars: (action.charCounts && action.charCounts[i]) || t.length,
                state: 'pending',
                audio: null,
            }));
            break;
        }

        // ── Per-chunk download state ────────────────────────────────────────

        // A chunk fetch was launched (chunkedPlayback pump). Never downgrades a
        // chunk that already has audio.
        case 'CHUNK_FETCHING': {
            const c = state.chunks[action.idx];
            if (c && c.state !== 'downloaded') c.state = 'downloading';
            break;
        }

        // A pump was aborted (seek / new session): any chunk still marked
        // 'downloading' will never receive its AUDIO_PART, so drop it back to
        // 'pending' rather than leaving a permanent spinner. Downloaded chunks
        // are untouched (D1.1: cached audio is preserved across seeks).
        case 'CHUNK_FETCH_RESET': {
            for (const c of state.chunks) {
                if (c && c.state === 'downloading') c.state = 'pending';
            }
            break;
        }

        case 'AUDIO_PART': {
            if (state.finished) break; // I2
            const i = action.chunkIdx;
            const c = state.chunks[i];
            if (!c) break; // out-of-range: ignore (no phantom chunks)
            c.audio = {
                url: action.url, kb: Math.round(action.bytes.length / 1024),
                genTime: action.genTime ?? null,
                pin: action.pin || state.pin || '',  // settings fingerprint tag
            };
            c.state = 'downloaded';
            state.totalBytes += action.bytes.length;
            // I4: auto-start playback on first chunk, never stop an already-playing
            // chain. With a resume target set, hold until that chunk arrives.
            if (state.playIdx === -1 && !state.finished && i >= state.resumeAt) {
                state.playIdx = i;
                state.chainPlaying = true;
                state.isPaused = false;
            }
            break;
        }

        case 'RETRY_SCHEDULED': {
            const c = state.chunks[action.chunkIdx];
            if (c && c.state !== 'downloaded') c.state = 'downloading';
            state.statusText = `Chunk ${action.chunkIdx + 1} failed (HTTP ${action.code}) — retry ${action.attempt}`;
            break;
        }

        case 'RETRY_FAILED': {
            const i = action.chunkIdx;
            const c = state.chunks[i];
            if (!c) break; // out-of-range: no-op
            c.state = 'errored';
            state.error = `Chunk ${i + 1} failed after retries (HTTP ${action.code})`;
            state.statusText = `Chunk ${i + 1} unavailable — skipping`;
            break;
        }

        // I1: STREAM_DONE is NOT a valid path to finished=true.
        // Download complete ≠ playback complete. Only updates status text.
        case 'STREAM_DONE': {
            if (!downloadedCount()) {
                state.statusText = 'No audio';
                state.error = 'Stream returned no parts.';
            } else {
                state.statusText = `Received ${downloadedCount()} part(s)`;
                if (action.elapsedMs) state.statsText = `stream: ${(action.elapsedMs / 1000).toFixed(1)}s`;
            }
            break;
        }

        // ── Playback actions ────────────────────────────────────────────────

        case 'PLAY_AT': {
            const i = action.idx;
            if (i < 0 || !state.chunks[i]?.audio) return notify(prev); // I3: guard — no audio at target
            state.playIdx = i;
            state.chainPlaying = true;
            state.isPaused = false;
            state.finished = false; // user explicitly re-engaged playback
            break;
        }

        // Deliberate I3 exception: user selected a chunk that has no audio yet
        // (still downloading, or never queued). Unlike PLAY_AT this is allowed —
        // the target need not be loaded. Moves the playback cursor AND the
        // fetch cursor (resumeAt); chunkedPlayback's pump re-targets from here.
        // Already-downloaded chunks are preserved (seek-back is instant).
        case 'SEEK_AT': {
            const i = action.idx;
            if (i < 0 || !state.chunks.length || i >= state.chunks.length) break;
            state.playIdx = i;
            state.resumeAt = i;
            state.chainPlaying = true;
            state.isPaused = false;
            state.finished = false;
            state.statusText = state.chunks[i].audio ? `Chunk ${i + 1}` : `Fetching chunk ${i + 1}…`;
            break;
        }

        case 'CHUNK_ENDED': {
            if (state.playIdx < 0) return notify(prev); // not playing — ignore
            const next = state.playIdx + 1;
            if (state.chunks[next]?.audio) {
                state.playIdx = next;
            } else {
                // I1: this is the ONLY natural path to finished=true, and only
                // when every chunk is present. A session with skipped/errored
                // chunks must NOT mark finished — that would reset the saved
                // resume marker as if the item was fully heard.
                const total = state.totalChunks || state.chunks.length || 0;
                const got = downloadedCount();
                const complete = total > 0 && got >= total;
                state.chainPlaying = false;
                state.isPaused = false;
                if (complete) {
                    state.finished = true;
                    state.statusText = `Done · ${total} chunks`;
                } else {
                    state.statusText = `Stream ended early at chunk ${got}${total ? `/${total}` : ''}`;
                }
            }
            break;
        }

        case 'SET_PAUSED': {
            if (action.paused) {
                if (state.playIdx >= 0 && !state.finished) {
                    state.isPaused = true;
                    state.chainPlaying = false;
                }
            } else {
                // Resume: only valid if we have a current chunk and haven't finished
                if (state.playIdx >= 0 && !state.finished) {
                    state.isPaused = false;
                    state.chainPlaying = true;
                }
            }
            break;
        }

        // ── Error actions ───────────────────────────────────────────────────

        case 'STREAM_ERROR': {
            state.statusText = 'Error';
            state.error = action.msg;
            break;
        }

        // ── Cosmetic — always valid, no invariants affected ────────────────

        case 'STATUS': {
            state.statusText = action.text;
            break;
        }

        case 'STATS': {
            state.statsText = action.text;
            break;
        }

        case 'CLEAR_ERROR': {
            state.error = null;
            break;
        }
    }

    notify(prev);
}

// ── Derived helpers (pure functions of current state) ────────────────

/** Number of chunks that have their audio (download progress numerator). */
export function downloadedCount(s = state) {
    let n = 0;
    for (const c of s.chunks) if (c.state === 'downloaded') n++;
    return n;
}

/**
 * Display status for chunk `i`. Download state leads: a chunk with no audio
 * can never be reported 'active' — you cannot be playing what you don't have.
 */
export function chunkStatus(i) {
    const c = state.chunks[i];
    if (!c) return 'pending';
    if (c.state === 'errored') return 'error';
    if (c.state === 'downloading') return 'generating';
    if (c.state === 'downloaded') {
        if (i === state.playIdx && (state.chainPlaying || !state.isPaused)) return 'active';
        if (state.finished || (state.playIdx >= 0 && i < state.playIdx)) return 'done';
        return 'ready';
    }
    return 'pending';
}

export function progressPct() {
    return state.totalChunks ? Math.round((downloadedCount() / state.totalChunks) * 100) : 0;
}
