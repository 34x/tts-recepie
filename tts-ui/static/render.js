// ── render.js — DOM reconciliation from state (idempotent) ────────────────
import { chunkStatus, SPEECH_RATE, CHUNK_ICONS, ICONS, downloadedCount } from './state.js';
import { SVG_ICONS } from './icons.js';
import { deriveTitle, progressSuffix } from './navTitle.js';
import { renderDownloadBar, renderPlayBar } from './progressBar.js';

let _dom = {};

export function init() {
    const $ = id => document.getElementById(id);
    _dom = {
        modeInput: $('mode-input'),
        modeLibrary: $('mode-library'),
        modePlay: $('mode-play'),
        textEl: $('text'),
        goBtn: $('go'),
        clearBtn: $('clear'),
        backBtn: $('back-btn'),
        player: $('player'),
        errEl: $('err'),

        segDownload: $('seg-download'),
        segPlay: $('seg-play'),
        playStatus: $('play-status'),
        modelSel: $('model'),
        voiceSel: $('voice'),
        instructEl: $('instruct'),
        pasteStreamChk: $('pasteStream'),
        chunkListEl: $('chunk-list'),
        backendInfo: $('backend-info'),
        settingsBtn: $('settings-btn'),
        settingsClose: $('settings-close'),
        settingsPanel: $('settings-panel'),
        settingsOverlay: $('settings-overlay'),
        prevBtn: $('prev-chunk'),
        nextBtn: $('next-chunk'),
        playPauseBtn: $('play-pause'),
        libraryBtn: $('library-btn'),
        libraryBackBtn: $('library-back-btn'),
        libraryListEl: $('library-list'),
        libraryEmptyEl: $('library-empty'),
        libraryCountEl: $('library-count'),
        streamStats: $('stream-stats'),
    };
}

/**
 * Main sync — call after every dispatch. Idempotent: safe to call multiple
 * times with the same state without side effects.
 */
export function syncUI(state) {
    const d = _dom;

    // Mode visibility
    d.modeInput.classList.toggle('active', state.mode === 'input');
    d.modeLibrary.classList.toggle('active', state.mode === 'library');
    d.modePlay.classList.toggle('active', state.mode === 'play');

    syncTitle(state);

    // Title: elapsed / total time + stream info
    const totalSec = state.chunks.reduce((s, c) => s + c.chars / SPEECH_RATE, 0);
    let playedSec = 0;
    for (let i = 0; i < state.playIdx && i < state.chunks.length; i++) {
        playedSec += state.chunks[i].chars / SPEECH_RATE;
    }
    let progressLabel = `${playedSec.toFixed(1)}s / ${totalSec.toFixed(1)}s`;
    if (state.statsText) progressLabel += ` · ${state.statsText}`;
    if (state.pin) progressLabel += ` · ${state.pin}`;
    if (d.playStatus.textContent !== progressLabel) d.playStatus.textContent = progressLabel;


    // Segmented progress
    syncBars(state);

    // Play/pause button icon (inline SVG — platform-independent, theme-coloured)
    const icon = !state.chainPlaying || state.isPaused ? SVG_ICONS.play : SVG_ICONS.pause;
    if (d.playPauseBtn.innerHTML !== icon) {
        d.playPauseBtn.innerHTML = icon;
    }

    // Chunk rows
    syncChunkRows(state);

    // Stream stats bar
    syncStreamStats(state);
}

// ── Document title (nav / tab / lockscreen) ─────────────────────────────────

/**
 * Keep document.title in sync with what is on screen.
 *  - input   → derived from the textarea (first meaningful line)
 *  - play    → first chunk text + " · 12/40"
 *  - library → "Library"
 * Input mode has no state for the draft text, so callers that change it
 * (typing, reset, library load) may call syncTitle directly.
 */
export function syncTitle(state = null) {
    const s = state || { mode: 'input' };
    let title;
    if (s.mode === 'play') {
        const first = s.chunks?.[0]?.text || '';
        title = deriveTitle(first) + progressSuffix(s);
    } else if (s.mode === 'library') {
        title = 'Library';
    } else {
        title = deriveTitle(_dom.textEl?.value || '');
    }
    title = (title || 'TTS').trim();
    if (document.title !== title) document.title = title;
}

// ── Progress bars ──────────────────────────────────────────────────────────

function syncBars(state) {
    // Download bar: one segment per contiguous run of equal chunk state — an
    // honest picture of *which* chunks are ready, not just how many.
    renderDownloadBar(_dom.segDownload, state.chunks);
    // Play bar: char-weighted fill up to the active chunk + cursor on it.
    renderPlayBar(_dom.segPlay, state.chunks, state.playIdx);
}

// ── Library list ───────────────────────────────────────────────────────────

/**
 * Render the library list. Entries are most-recent-first.
 * `currentText` marks the entry matching the current input draft (if any).
 */
export function renderLibrary(entries, currentText = '') {
    const d = _dom;
    const draft = currentText.trim();

    d.libraryListEl.replaceChildren();

    for (const entry of entries) {
        const row = document.createElement('div');
        row.className = 'library-row';
        row.dataset.id = entry.id;
        if (draft && entry.text.trim() === draft) row.classList.add('current');

        const body = document.createElement('button');
        body.className = 'library-row-body';
        body.type = 'button';

        const preview = document.createElement('span');
        preview.className = 'library-row-text';
        preview.textContent = entry.text;

        const meta = document.createElement('span');
        meta.className = 'library-row-meta';
        const kb = (entry.chars / 1024).toFixed(1);
        meta.textContent = `${entry.chars}c · ${kb}KB · ${formatAge(entry.lastUsedAt)}`;

        body.appendChild(preview);
        body.appendChild(meta);

        const del = document.createElement('button');
        del.className = 'icon-btn library-row-delete';
        del.type = 'button';
        del.title = 'Delete';
        del.dataset.action = 'delete';
        del.dataset.id = entry.id;
        del.textContent = ICONS.delete;

        row.appendChild(body);
        row.appendChild(del);
        d.libraryListEl.appendChild(row);
    }

    const empty = entries.length === 0;
    d.libraryEmptyEl.style.display = empty ? '' : 'none';
    d.libraryCountEl.textContent = entries.length ? `${entries.length}` : '';
}

function formatAge(ts) {
    const sec = Math.max(0, Math.round((Date.now() - ts) / 1000));
    if (sec < 60) return 'just now';
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.round(min / 60);
    if (hr < 24) return `${hr}h ago`;
    return `${Math.round(hr / 24)}d ago`;
}

// ── Stream stats ─────────────────────────────────────────────────────────────

let _lastStorageEstimate = null;
let _storageEstimatePending = false;

function _fmtBytes(n) {
    if (n < 1024) return n + 'B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB';
    return (n / (1024 * 1024)).toFixed(2) + 'MB';
}

function syncStreamStats(state) {
    const el = _dom.streamStats;
    if (!el) return;

    if (state.mode !== 'play') { el.textContent = ''; return; }

    const totalChars = state.chunks.reduce((s, c) => s + c.chars, 0);
    const parts = [
        `${totalChars}c`,
        `${_fmtBytes(state.totalBytes)}`,
        `${downloadedCount(state)}/${state.totalChunks || '?'} ch`,
    ];

    // localStorage estimate (best-effort, throttled to 2s)
    if (navigator.storage && navigator.storage.estimate) {
        if (!_storageEstimatePending && (! _lastStorageEstimate || performance.now() - (_lastStorageEstimate._ts || 0) > 2000)) {
            _storageEstimatePending = true;
            navigator.storage.estimate().then(est => {
                _lastStorageEstimate = { usage: est.usage, quota: est.quota, _ts: performance.now() };
                _storageEstimatePending = false;
                // re-render with fresh data
                el.textContent = _buildStatsText(state);
            }).catch(() => { _storageEstimatePending = false; });
        }
        if (_lastStorageEstimate) {
            parts.push(`LS ${_fmtBytes(_lastStorageEstimate.usage)}/${_fmtBytes(_lastStorageEstimate.quota)}`);
        }
    }

    el.textContent = parts.join(' \u00b7 ');
}

function _buildStatsText(state) {
    const totalChars = state.chunks.reduce((s, c) => s + c.chars, 0);
    const parts = [
        `${totalChars}c`,
        `${_fmtBytes(state.totalBytes)}`,
        `${downloadedCount(state)}/${state.totalChunks || '?'} ch`,
    ];
    if (_lastStorageEstimate) {
        parts.push(`LS ${_fmtBytes(_lastStorageEstimate.usage)}/${_fmtBytes(_lastStorageEstimate.quota)}`);
    }
    return parts.join(' \u00b7 ');
}

// ── Chunk rows ─────────────────────────────────────────────────────────────

function syncChunkRows(state) {
    const list = _dom.chunkListEl;
    const n = state.chunks.length;

    // Create/destroy rows to match chunk count
    if (list.children.length !== n) {
        rebuildRows(state);
    }

    // Update each row's state class + meta text (index from data attribute)
    for (const row of list.children) {
        const i = Number(row.dataset.index);
        if (!state.chunks[i]) continue;

        const status = chunkStatus(i);
        const cls = 'chunk-row ' + status;
        if (row.className !== cls) row.className = cls;

        // Meta line: chars · [KB] · ~duration [· gen time] [· status glyph]
        const metaEl = row.querySelector('.chunk-meta');
        const chunk = state.chunks[i];
        const audio = chunk.audio;
        let s = `${chunk.chars}c`;
        if (audio) s += ` · ${audio.kb}KB`;
        s += ` · ~${(chunk.chars / SPEECH_RATE).toFixed(1)}s`;
        if (audio && audio.genTime !== null && audio.genTime !== undefined) s += ` · ⏱${audio.genTime.toFixed(1)}s`;
        const icon = CHUNK_ICONS[status] || '';
        if (icon) s += ` · ${icon}`;
        if (metaEl && metaEl.textContent !== s) metaEl.textContent = s;
    }
}

function rebuildRows(state) {
    const list = _dom.chunkListEl;
    list.replaceChildren();

    for (let i = 0; i < state.chunks.length; i++) {
        const chunk = state.chunks[i];
        const row = document.createElement('div');
        row.className = 'chunk-row pending';
        row.dataset.index = String(i);

        const body = document.createElement('div');
        body.className = 'chunk-body';

        const txt = document.createElement('span');
        txt.className = 'chunk-text';
        txt.textContent = chunk.text;

        const meta = document.createElement('span');
        meta.className = 'chunk-meta';
        meta.textContent = `${chunk.chars}c · ~${(chunk.chars / SPEECH_RATE).toFixed(1)}s`;

        body.appendChild(txt);
        body.appendChild(meta);
        row.appendChild(body);
        list.appendChild(row);
    }
}
