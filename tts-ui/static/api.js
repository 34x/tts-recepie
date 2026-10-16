// ── api.js — network utilities (config, buffered) ───────────────────────

/**
 * Persist the model/voice selection so it survives reloads.
 * Stored as {backend, model, voice} — the model lives in the option's
 * dataset.model (backend label alone is not specific enough).
 */
const SELECTION_KEY = 'tts-ui.selection';

function saveSelection() {
    try {
        const modelSel = document.getElementById('model');
        localStorage.setItem(SELECTION_KEY, JSON.stringify({
            backend: modelSel.value,
            model: modelSel.selectedOptions[0]?.dataset.model || '',
            voice: document.getElementById('voice').value,
        }));
    } catch { /* storage unavailable/blocked — selection just won't persist */ }
}

function loadSavedSelection() {
    try { return JSON.parse(localStorage.getItem(SELECTION_KEY) || 'null'); } catch { return null; }
}

/** Restore the saved model/backend selection. Voice options depend on it. */
function restoreModel(saved) {
    if (!saved || saved.backend == null) return;
    const modelSel = document.getElementById('model');
    const want = (saved.model || '');
    for (const o of modelSel.options) {
        if (o.value === saved.backend && (o.dataset.model || '') === want) {
            modelSel.value = o.value;
            modelSel.selectedIndex = o.index;
            break;
        }
    }
}

/**
 * Restore the saved voice. MUST run AFTER updateVoices() for the restored
 * backend — voice options are rebuilt per model, so at the time the default
 * backend's voices are listed the saved voice of another backend is absent.
 */
function restoreVoice(saved) {
    if (!saved || saved.voice == null) return;
    const voiceSel = document.getElementById('voice');
    if ([...voiceSel.options].some(o => o.value === saved.voice)) {
        voiceSel.value = saved.voice;
    }
}

/**
 * Fetch /config and populate the model/voice selectors.
 * Returns a Promise that resolves when population is done (or rejects on network error).
 */
export function fetchConfig(onPopulated) {
    return fetch('/config').then(r => r.json()).then(cfg => {
        const backends = cfg.backends || [];
        const modelsByBackend = cfg.models || {};
        const voicesByBackend = cfg.voices || {};

        // Populate model selector
        const modelSel = document.getElementById('model');
        modelSel.replaceChildren();
        backends.forEach(b => {
            const models = modelsByBackend[b.label] || [];
            const badge = `${b.gpu ? ' [gpu]' : ' [cpu]'}${b.instructions ? ' [instructions]' : ''}`;
            if (!models.length) {
                const o = document.createElement('option');
                o.value = b.label;
                o.dataset.model = '';
                o.textContent = `default @ ${b.label}${badge}`;
                modelSel.appendChild(o);
            } else {
                models.forEach(m => {
                    const o = document.createElement('option');
                    o.value = b.label;
                    o.dataset.model = m;
                    o.textContent = `${m} @ ${b.label}${badge}`;
                    modelSel.appendChild(o);
                });
            }
        });

        // Update voices + instruct visibility on model change
        function updateInstruct() {
            const label = modelSel.value;
            const sel = backends.find(b => b.label === label) || backends[0];
            const show = !!(sel && sel.instructions);
            document.getElementById('instruct').style.display = show ? 'block' : 'none';
            document.getElementById('instruct-label').style.display = show ? 'block' : 'none';
        }

        function updateVoices() {
            const label = modelSel.value;
            const sel = backends.find(b => b.label === label) || backends[0];
            const voiceSel = document.getElementById('voice');
            voiceSel.replaceChildren();
            const def = document.createElement('option');
            def.value = '';
            def.textContent = 'Auto-detect';
            voiceSel.appendChild(def);
            (sel && sel.voices || voicesByBackend[label] || cfg.voices || []).forEach(v => {
                const o = document.createElement('option');
                o.value = o.textContent = v;
                voiceSel.appendChild(o);
            });
        }

        function updateBackendInfo() {
            const label = modelSel.value;
            const sel = backends.find(b => b.label === label) || backends[0];
            if (!sel) { document.getElementById('backend-info').textContent = ''; return; }
            const gpuTag = sel.gpu ? ' · GPU' : ' · CPU';
            document.getElementById('backend-info').textContent = `${sel.url}${gpuTag}`;
        }

        modelSel.addEventListener('change', () => { updateInstruct(); updateVoices(); updateBackendInfo(); saveSelection(); });
        updateInstruct();
        updateVoices();
        updateBackendInfo();

        // Restore order matters: set the model, rebuild its voice list, then
        // select the saved voice (which only exists in that backend's list).
        const saved = loadSavedSelection();
        restoreModel(saved);
        updateInstruct();
        updateVoices();
        restoreVoice(saved);
        updateBackendInfo();

        document.getElementById('voice').addEventListener('change', saveSelection);

        if (onPopulated) onPopulated(backends);
    }).catch(() => {});
}
