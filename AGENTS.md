# TTS — Project Pipeline

## Overview
Text-to-speech stack: multiple interchangeable TTS engine backends, a
thin OpenAI-compatible proxy/UI, and a container-based deployment.

Two deployment modes:
- **Unified container** (default) — one image, supervisord manages all
  backends + UI. Managed by `start.py`.
- **Pod** (optional) — one container per backend + UI in a shared
  podman pod. Managed by `start_pod.py`.

## Repository layout

```
/
├── start.py              ← unified container orchestrator
├── start_pod.py          ← pod orchestrator
├── .env.example
├── unified/             ← unified container (supervisord)
│   ├── Containerfile
│   ├── pyproject.toml / uv.lock
│   ├── supervisord.conf
│   ├── entrypoint-backend.sh
│   ├── wait_health.py
│   ├── piper_tts.py
│   └── supertonic_tts.py
├── pod/                 ← multi-container pod mode
│   ├── tts-dev-pod.yaml.tmpl
│   ├── Containerfile.*
│   └── <engine>.py per backend
├── shared/              ← shared engine code
│   ├── openai_tts_server.py
│   └── text_cleaner.py
├── tts-ui/              ← UI (proxy + PWA)
└── refs/                ← assets
```

## Pipeline (request flow)

1. **UI** (`tts-ui/`) — PWA (static JS: `app.js`, `state.js`, `render.js`,
   `playback.js`, `library.js`, `api.js`). User types/pastes text, picks a
   model/voice, taps Speak.

2. **UI proxy** (`tts-ui/server.py`, Python). Serves static files,
   `/config` (backend + model discovery), and TTS endpoint(s):
   - `/api/audio/speech` — **active**: single request → one mp3 response.
     This is the only endpoint used by the current client (`chunkedPlayback.js`).
   - `/api/speech/multipart` — **deprecated, server-side only**. No client consumer;
     lives in `server.py` but no JS file calls it. Do not extend or debug this path.

3. **Backend engines** (one Python file per engine, e.g. `piper_tts.py`,
   `supertonic_tts.py`, `kokoro_tts.py`, …). Each exposes an
   OpenAI-compatible `/v1/audio/speech` (and `/v1/models`/`/v1/audio/voices`)
   on a distinct port. Pipers clean input via `shared/text_cleaner.py`
   (extensible, step-based `preprocess()`).

4. **Unified container** (`unified/Containerfile`, `unified/supervisord.conf`,
   `start.py`). Single image with all CPU backends + UI under supervisord.
   `start.py` builds, starts, stops, and tests the container.
   **Adding a backend** = synthesizer file in `unified/` + dep in
   `unified/pyproject.toml` + `[program:]` entry in `unified/supervisord.conf`
   + port in `TTS_BACKENDS`.
   Run: `python3 start.py build|start|stop|restart|status|logs|test`.

5. **Pod** (optional) (`pod/tts-dev-pod.yaml.tmpl`, `start_pod.py`).
   One container per backend, shared pod netns. `start_pod.py`
   auto-detects every container labeled `tts.backend: "true"`, builds the
   `TTS_BACKENDS` JSON, and injects it into the UI container.
   **Adding a backend** = one container block in the template + its
   `pod/Containerfile.*`. Run: `python3 start_pod.py start|status|stop|restart|logs`.

## Key contracts

| Endpoint | Owner | In | Out |
|---|---|---|---|
| `/v1/audio/speech` | every backend | `{input, voice, model, instruct?, response_format}` | mp3/wav audio |
| `/v1/models`, `/v1/audio/voices` | backends (some) | — | JSON lists |
| `/config` | UI proxy | — | `{backends, models, voices}` |
| `/api/audio/speech` | UI proxy (active) | JSON `{input, voice, model, instruct}` | mp3 |
| `/api/speech/multipart` | UI proxy (deprecated) | same JSON | multipart per-chunk audio. **No client consumer — do not use.** |

## Notes / constraints
- UI proxy is JSON-only POST; **no multipart upload support** on the server.
  No client-side multipart response parser exists either — `/api/speech/multipart`
  has no consumer and should not be extended.
- `shared/text_cleaner.py` is per-engine (Piper) — not used by the UI proxy.
- Long-form content is chunked + streamed; a 100k-char cap applies.
- Unified container: `unified/Containerfile` + `unified/pyproject.toml`.
- Pod: one `pod/Containerfile.*` per backend plus `tts-ui/Containerfile`.
- Env vars use a `TTS_` prefix; the internal Python constant drops it (`TTS_CHUNK_HARD` → `CHUNK_HARD`).
