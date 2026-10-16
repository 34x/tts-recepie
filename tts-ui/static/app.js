// ── app.js — TTS PWA entry point (event wiring, effects, SW) ─────────────
import { getState, dispatch, subscribe, __actions, ICONS } from './state.js';
import { SVG_ICONS } from './icons.js';
import * as render from './render.js';
import * as playback from './playback.js';
import { fetchConfig } from './api.js';
import { startChunked, seekChunked, endChunked, pumpCovers } from './chunkedPlayback.js';
import { library } from './library.js';
import { cleanText } from './textNormalizer.js';
import * as theme from './theme.js';
import { createSettingsStore } from './settings.js';

// ── Init ────────────────────────────────────────────────────────────────

const player = document.getElementById('player');
render.init();
playback.init(player);
theme.init();
document.body.classList.add('mode-chunked'); // chunked is the only transport

// ── Persisted settings ──────────────────────────────────────────────────
// Panel toggles are remembered as one JSON blob in localStorage. Applied here
// before any interaction so the panel reflects the saved state.
const settingsStore = createSettingsStore();
const savedSettings = settingsStore.get();
for (const key of ['pasteStream', 'normalizeText']) {
    const el = document.getElementById(key);
    if (el) {
        el.checked = savedSettings[key];
        el.addEventListener('change', () => settingsStore.set({ [key]: el.checked }));
    }
}

// ── Keyboard avoidance (mobile) ─────────────────────────────────────────
// The layout uses 100dvh; mobile browsers do not reliably shrink that when the
// on-screen keyboard opens, so the Speak bar would land under the keyboard.
// Derive the covered height from visualViewport and expose it as --kb-inset,
// which #mode-input reserves as bottom padding (see style.css).
function updateKeyboardInset() {
    const vv = window.visualViewport;
    if (!vv) return;
    const covered = window.innerHeight - vv.height - vv.offsetTop;
    document.body.style.setProperty('--kb-inset', `${Math.max(0, Math.round(covered))}px`);
}
if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', updateKeyboardInset);
    window.visualViewport.addEventListener('scroll', updateKeyboardInset);
    updateKeyboardInset();
}

// Set all static icons from single source (state.js ICONS const)
document.getElementById('back-btn').textContent = ICONS.back;
document.getElementById('settings-btn').textContent = ICONS.settings;
document.getElementById('settings-close').textContent = ICONS.close;
document.getElementById('clear').textContent = ICONS.clear;
document.getElementById('prev-chunk').innerHTML = SVG_ICONS.prev;
document.getElementById('next-chunk').innerHTML = SVG_ICONS.next;
document.getElementById('download-btn').textContent = ICONS.download;
document.getElementById('library-btn').querySelector('.icon-btn-icon').textContent = ICONS.library;

// Subscribe: sync DOM on every state change (pixels only — no audio side effects)
subscribe((state) => { render.syncUI(state); });

// Theme toggle (cycle auto → light → dark); document title on typing.
document.getElementById('theme-btn').addEventListener('click', () => theme.cycle());
document.getElementById('text').addEventListener('input', () => render.syncTitle());

// ── Debug panel ───────────────────────────────────────────────────────────
const debugEl = document.getElementById('debug-panel');

subscribe((state) => {
    debugEl.textContent = __actions.join('\n') + '\n\n' + JSON.stringify(state, null, 2);
});

// ── Error toast (with auto-dismiss) ─────────────────────────────────────

let errTimeout = null;
function showError(msg) {
    const el = document.getElementById('err');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(errTimeout);
    errTimeout = setTimeout(() => {
        el.classList.remove('show');
        dispatch({ type: 'CLEAR_ERROR' });
    }, 5000);
}

// ── Settings panel ───────────────────────────────────────────────────────

const settingsPanel = document.getElementById('settings-panel');
const settingsOverlay = document.getElementById('settings-overlay');

function openSettings() {
    settingsPanel.classList.add('open');
    settingsOverlay.classList.add('show');
}
function closeSettings() {
    settingsPanel.classList.remove('open');
    settingsOverlay.classList.remove('show');
}

document.getElementById('settings-btn').addEventListener('click', openSettings);
document.getElementById('settings-close').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', closeSettings);

// ── Speak action ─────────────────────────────────────────────────────────

// Library entry currently being synthesized/played (null for ad-hoc text).
// Used to remember the last-played chunk per entry.
let activeEntryId = null;

function buildPayload() {
    const modelSel = document.getElementById('model');
    return {
        input: document.getElementById('text').value,
        voice: document.getElementById('voice').value,
        backend: modelSel.value,
        model: modelSel.selectedOptions[0]?.dataset.model || '',
        instruct: document.getElementById('instruct').value,
    };
}

async function speak(resumeAt = 0) {
    // Normalize BEFORE chunking and BEFORE saving to the library, so every
    // backend synthesizes the same cleaned text and the library stores the
    // cleaned version (never the raw, emoji-laden one). When the setting is
    // off, fall back to the raw text unchanged. Cleaning is deterministic and
    // idempotent, so the stored per-entry resume index (lastChunk) stays valid
    // across sessions.
    const raw = document.getElementById('text').value;
    const text = settingsStore.get().normalizeText ? cleanText(raw.trim()) : raw.trim();
    if (!text) { showError('Enter some text first.'); return; }

    // Auto-save every synthesis to the library (button + paste paths both
    // funnel through here). Dedupe/refresh are handled by the store, which
    // also preserves any remembered resume position.
    try {
        library.save(text);
        activeEntryId = library.idFor(text);
    } catch (e) { console.warn('library:', e); }

    startChunked({
        text,
        ...buildPayload(), // voice, model, instruct from the UI controls
        getResumeAt: () => resumeAt, // start at requested chunk (client concern)
    });
}

document.getElementById('go').addEventListener('click', speak);

// ── Clear / back to input ────────────────────────────────────────────────

function resetToInput() {
    endChunked();          // clear session + abort any in-flight chunk fetches
    playback.stopAll();

    // Revoke all blob URLs from the chunk audio store
    for (const c of getState().chunks) {
        if (c.audio?.url) URL.revokeObjectURL(c.audio.url);
    }

    dispatch({ type: 'RESET' });
    activeEntryId = null;
    document.getElementById('text').value = '';
    document.getElementById('text').focus();
    render.syncTitle();
}

document.getElementById('clear').addEventListener('click', resetToInput);
document.getElementById('back-btn').addEventListener('click', resetToInput);

// ── Library ──────────────────────────────────────────────────────────────

function openLibrary() {
    render.renderLibrary(library.list(), document.getElementById('text').value);
    dispatch({ type: 'GOTO_LIBRARY' });
}

document.getElementById('library-btn').addEventListener('click', openLibrary);
document.getElementById('library-back-btn').addEventListener('click', () => {
    dispatch({ type: 'GOTO_INPUT' });
});

// Tap a row → load text and start playback, exactly like input's Speak.
document.getElementById('library-list').addEventListener('click', (e) => {
    const del = e.target.closest('[data-action="delete"]');
    if (del) {
        library.remove(del.dataset.id);
        render.renderLibrary(library.list(), document.getElementById('text').value);
        return;
    }

    const row = e.target.closest('.library-row');
    if (!row) return;
    const entry = library.get(row.dataset.id);
    if (!entry) return;
    document.getElementById('text').value = entry.text;
    speak(entry.lastChunk || 0);
});

// ── Navigation controls (imperative: call playback directly) ─────────────

document.getElementById('prev-chunk').addEventListener('click', () => {
    const s = getState();
    if (s.playIdx > 0) goToChunk(s.playIdx - 1);
});

document.getElementById('next-chunk').addEventListener('click', () => {
    const s = getState();
    const total = s.totalChunks || s.chunks.length;
    if (s.playIdx < total - 1) goToChunk(s.playIdx + 1);
});

// Shared: jump playback to a chunk. Chunked mode is the only one that can
// target a NOT-yet-loaded chunk (retargets the fetch cursor to it → end).
//
// Retarget rule (the pump always fills [start → end], skipping cached chunks):
//   - target not downloaded            → retarget (it must be fetched)
//   - target downloaded, BEFORE pump   → retarget (chunks between target and the
//     old pump start are a hole the old pump will never fill; e.g. seek 12 → 3
//     would otherwise play 3 then die at the gap 4…11)
//   - target downloaded, inside pump   → don't retarget (pump already covers it,
//     so no in-flight request is wasted)
function goToChunk(idx) {
    const s = getState();
    if (s.transport === 'chunked' && (!s.chunks[idx]?.audio || !pumpCovers(idx))) {
        seekChunked(idx);
    } else {
        playback.playAt(idx);
    }
}

document.getElementById('play-pause').addEventListener('click', () => {
    const s = getState();
    if (s.isPaused || player.paused) {
        playback.resume();
    } else {
        playback.pause();
    }
});

// Speed button — stub for future integration
const SPEEDS = [1, 1.25, 1.5, 0.75];
let speedIdx = 0;
document.getElementById('speed-btn').addEventListener('click', () => {
    speedIdx = (speedIdx + 1) % SPEEDS.length;
    player.playbackRate = SPEEDS[speedIdx];
    document.getElementById('speed-btn').textContent = `${SPEEDS[speedIdx]}×`;
});

// ── Download MP3 ────────────────────────────────────────────────────────────

document.getElementById('download-btn').addEventListener('click', async () => {
    const s = getState();
    const blobs = [];
    try {
        // Concat every downloaded chunk blob in order.
        for (const c of s.chunks) {
            if (c.audio?.url) blobs.push(await (await fetch(c.audio.url)).blob());
        }
    } catch (e) {
        showError(`Download failed: ${e.message}`);
        return;
    }
    if (!blobs.length) { showError('Nothing to download yet.'); return; }

    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(blobs, { type: 'audio/mpeg' }));
    a.download = `tts-${Date.now()}.mp3`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
});

// ── Chunk row tap-to-seek ────────────────────────────────────────────────

document.getElementById('chunk-list').addEventListener('click', (e) => {
    const row = e.target.closest('.chunk-row');
    if (!row) return;
    const idx = Number(row.dataset.index);
    // Chunked mode: allow seeking to a NOT-yet-loaded chunk (retargets the
    // fetch cursor). Other modes: only chunks we already have audio for.
    goToChunk(idx);
});

// ── Progress bar tap-to-seek ────────────────────────────────────────────────

document.getElementById('seg-wrap').addEventListener('click', (e) => {
    const bar = e.target.closest('.bar');
    if (!bar) return;
    const rect = bar.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    const s = getState();
    const total = s.totalChunks || s.chunks.length || 0;
    if (!total) return;
    const idx = Math.max(0, Math.min(total - 1, Math.round(frac * (total - 1))));
    goToChunk(idx);
});

// Scroll active chunk into view on playIdx change
subscribe((state, prev) => {
    if (state.playIdx !== prev.playIdx && state.playIdx >= 0) {
        const row = document.querySelector(`.chunk-row[data-index="${state.playIdx}"]`);
        if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
});

// Remember the last-played chunk per library entry. Persist on every chunk
// change (survives a tab close mid-play); clear it once the stream finishes
// so a completed entry reopens from the start.
subscribe((state, prev) => {
    if (!activeEntryId) return;
    if (state.playIdx !== prev.playIdx && state.playIdx >= 0) {
        try { library.setLastChunk(activeEntryId, state.playIdx); } catch (e) { console.warn('library:', e); }
    }
    if (state.finished && !prev.finished) {
        try { library.setLastChunk(activeEntryId, 0); } catch (e) { console.warn('library:', e); }
    }
});

// ── Keyboard shortcuts ───────────────────────────────────────────────────

document.getElementById('text').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) speak();
});

document.addEventListener('keydown', e => {
    const s = getState();
    if (s.mode === 'play') {
        if (e.key === 'Escape') resetToInput();
        if (e.key === ' ') { e.preventDefault(); document.getElementById('play-pause').click(); }
    }
});

// ── Paste to stream ──────────────────────────────────────────────────────

document.getElementById('text').addEventListener('paste', () => {
    setTimeout(() => {
        if (document.getElementById('pasteStream').checked && document.getElementById('text').value.trim()) {
            speak();
        }
    }, 0);
});

// ── Load config ──────────────────────────────────────────────────────────

fetchConfig();

// ── Service worker ───────────────────────────────────────────────────────

if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js').catch(e => console.warn('SW:', e));
    });
}
