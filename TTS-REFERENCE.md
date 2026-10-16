# TTS & tts-ui — Complete Reference

Authoritative reference for every TTS engine in the project, the tts-ui proxy,
the dev harnesses, and all the hard-won findings. Read this before touching any
TTS code.

---

## 1. Landscape — which engine, when

| Engine | Quality | Speed | Languages | Instructions | Resource | Status |
|--------|---------|-------|-----------|--------------|----------|--------|
| **Qwen3-TTS** (PyTorch) | High | 0.76x realtime (GPU bf16) | en/zh (auto-detect) | Free voice-design | 14% VRAM (1.7B) | Dev-proven, not in pod |
| **Supertonic 3** | Piper-tier | >4x realtime (CPU) | 31 (incl. ru/de) | Preset styles + tags | ~0 VRAM | Dev-proven, not in pod |
| **Piper** (current default) | Baseline | fast | ru/de | none | ~0 | **In pod** (:5050) |
| **Kokoro** | — | too slow | — | — | — | Retired (crashed) |

**Recommended role split (not yet wired into the pod):**
- **Supertonic 3 replaces Piper** as the default fast/multilingual engine (ru/de, quick reads, low-stakes).
- **Qwen3-TTS stays** as the high-quality engine for en/zh when quality matters (or voice-design instructions).
- Piper gets retired once Supertonic 3 is wired in.

Both new engines are **dev-proven but NOT yet integrated into the pod**.
Integration is deferred until the dev harness proves out.

---

## 2. tts-ui — the OpenAI-compatible proxy

`tts-ui/` is a tiny stdlib HTTP server that proxies OpenAI-compatible
`/v1/audio/speech` to any engine and streams audio to the browser. It is
**engine-agnostic** — the engine URL is env-overridable, so switching engines
never touches the code.

### Files
| File | Purpose |
|------|---------|
| `tts-ui/server.py` | The proxy server (stdlib only, no deps) |
| `tts-ui/static/index.html` | The web UI |
| `tts-ui/Containerfile` | Prod image (`python:3.12-alpine`, `requests`) |
| `tts-ui/Containerfile.dev` | Dev wrapper (bind-mounts `/app`) |
| `tts-ui/dev-run.sh` | Dev container launcher (points at Piper) |

### Env vars (all optional, defaults shown)
| Var | Default | Purpose |
|-----|---------|---------|
| `TTS_URL` | `http://127.0.0.1:5050/v1/audio/speech` | Engine endpoint |
| `TTS_MODELS` | derived from `TTS_URL` | Engine's `/v1/models` URL for voice list |
| `TTS_VOICES` | empty | **Deprecated.** Legacy comma-separated voice list. Superseded by per-backend discovery (see below); only used by `dev-run.sh` for Piper. |
| `TTS_API_KEY` | empty | Optional Bearer key, forwarded to engine |
| `TTS_CHUNK_CHARS` | 500 | Target chunk size in chars |
| `TTS_CHUNK_HARD` | 1000 | Hard cap for pathological long sentences |
| `TTS_CHUNK_LOOKAHEAD` | 1 | Chunks synthesized ahead of streaming |
| `TTS_STREAM_CHUNK_CHARS` | 300 | Chunk size for the multipart streaming endpoint |
| `TTS_PROXY_TIMEOUT` | 120 | Upstream engine request timeout (s). **Raise for slow multi-chunk engines** (qwen uses 300-360) |
| `TTS_MAX_INPUT_CHARS` | 100000 | Hard input cap; truncation reported via `X-Input-Truncated` header |
| `PORT` | 8095 | Listen port |

### Endpoints
- `GET /` or `/index.html` — the UI
- `GET /config` — `{tts_url, models[], voices[]}` (proxies `TTS_MODELS` + per-backend voice discovery)
- `POST /api/tts` — chunked single-mp3 stream (concatenates chunks, gapless)
- `POST /api/speech/multipart` — W3C-style multipart/x-mixed-replace stream (each sentence its own part)

### Voice discovery (project contract — NOT an OpenAI endpoint)

`GET /v1/audio/voices` is defined by **the project's TTS backbone** (`openai_tts_server.py`),
not by the OpenAI API — the OpenAI spec has no voice-listing endpoint; voice names
there are a fixed enum (`alloy`, `echo`, ...). This endpoint is a deliberate,
project-owned extension namespaced under `/v1/audio/` for convenience only.

- Returns `{"voices": ["F1", ..., "M5"]}` — the backend's advertised voice list.
- `tts-ui` discovers voices per backend via `_discover_voices()` (mirrors
  `_discover_models()`), probing `<backend>/v1/audio/voices` in parallel and
  exposing the result in `/config` as `voices: {<label>: [<voice>, ...]}`.
- Frontend resolves the dropdown as `backend.voices || discovered[label] || cfg.voices`.
- A backend that doesn't implement the endpoint (piper, kokoro, qwen) returns
  nothing → UI falls back to `Auto-detect`. A backend descriptor may also declare
  an explicit `voices` list to skip probing.

Single source of truth is the backend itself; there is no duplicated voice list in
pod config or UI env.

### Streaming architecture (important)
- Long text is split into sentence-ish chunks (`chunk_text`), synthesized
  concurrently with a small lookahead (`ChunkSynthesizer`), and streamed in order.
- **Chunked mp3 concatenation** relies on mp3 frames being self-contained —
  each chunk must be encoded with **no ID3v2 tag and no Xing/Info header**
  (see `-id3v2_version 0 -write_xing 0` in the engine servers). Otherwise
  interleaved metadata desyncs/clicks after part 0.
- **Failed chunks are patched with `SILENCE_MP3`** (~0.5s near-silence) so the
  stream stays valid instead of breaking.
- **Client disconnect detection** is via write errors (BrokenPipeError /
  ConnectionResetError), NOT socket polling — polling `recv()` misreads a
  half-close (FIN) as a disconnect and aborts the stream on mobile Safari/Brave.
- The multipart endpoint uses `application/octet-stream` (NOT
  `multipart/x-mixed-replace`) because Safari treats that as a document stream
  and fails the fetch. The client parses the boundary from the body's first line.
- **Chunked-encoding terminator must be written raw** (`b"0\r\n\r\n"`), not
  wrapped in a chunk frame — otherwise the response never terminates and the
  browser's fetch stream never closes.

### Known tts-ui issues
- **`/config` shows `models:[]` / `voices:[]` for Supertonic** — `MODELS_URL`
  points at `.../models` but Supertonic serves `/v1/models` (path mismatch).
  Cosmetic; the UI still works. **Voices are now discovered independently**
  via `/v1/audio/voices`, so the voice dropdown is populated regardless; only
  the model list remains affected. Fix: point `TTS_MODELS` at the `/v1/models` path.
- **Chrome silent-stream bug (FIXED, commit `dbb8707`)** — Chrome's
  `MediaSource.addSourceBuffer` throws for `audio/mpeg`; the fix guards with
  `if (!MediaSource.isTypeSupported(MIME)) throw ...` before committing to MSE.
  Without it, `sb` stays null, parts pile in `pendingParts` forever, and the
  stream "completes" (parts: N/N) with no audio.
- **UX issues** — user reported "There are some issues with UX but works" — not
  yet detailed.

---

## 3. Qwen3-TTS (PyTorch backend) — the quality tier

The current best-quality engine. Two model families, selected by env.

### Model families
| Family | Synthesis | Needs ref audio? | Notes |
|--------|-----------|------------------|-------|
| **1.7B VoiceDesign** (default) | `generate_voice_design(text, instruct, language)` | No | Free voice-design instructions; `instruct` defaults to empty |
| **0.6B Base** | `generate_voice_clone(...)` | Yes (`QWEN_TTS_REF_AUDIO`) | Voice cloning from a reference wav |

### Files
| File | Purpose |
|------|---------|
| `pod/Containerfile.qwen-tts` | Image build (rocm/pytorch base + qwen-tts) |
| `pod/qwen_torch_tts.py` | Qwen backend for the generic OpenAI server (the API) |

### Container image
```dockerfile
FROM docker.io/rocm/pytorch:rocm7.14.1_ubuntu24.04_py3.12_pytorch_release_2.12.0
RUN apt-get install -y sox ffmpeg        # sox REQUIRED (qwen_tts imports it at load)
RUN pip install "qwen-tts"
ENV QWEN_TTS_MODEL=/models/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
    QWEN_TTS_LANG=auto
CMD ["python", "/app/qwen_torch_tts.py"]
```
- **`sox` is mandatory** — `qwen_tts` imports the native `sox` module at load
  time for speaker-embedding extraction. Without it the container crashes on startup.
- **`ffmpeg`** encodes WAV→MP3 with no leading metadata (concat-safe).
- Base image ships torch 2.12 + ROCm libs. The `qwen-tts` pip package pulls
  transformers/accelerate/gradio/librosa/torchaudio/soundfile/sox/onnxruntime/einops.

### Server env vars
| Var | Default | Purpose |
|-----|---------|---------|
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8096` | Listen port |
| `QWEN_TTS_MODEL` | 1.7B VoiceDesign path | Model checkpoint dir |
| `QWEN_TTS_REF_AUDIO` | `/app/ref.wav` | Ref wav for Base clone voice |
| `QWEN_TTS_REF_TEXT` | empty | Optional ref transcript for Base clone (else x-vector only) |
| `QWEN_TTS_LANG` | `auto` | Default language: auto/en/ru/de |
| `QWEN_TTS_INSTRUCT` | empty | Default voice-design instruction |
| `TTS_API_KEY` | empty | Optional Bearer key |

### Endpoints
`GET /v1/models`, `GET /health`, `POST /v1/audio/speech` (mp3 or wav).

### GPU detection (critical design)
`pick_device()` in the server/smoke selects the CUDA device with the **most FREE
VRAM** — never a hardcoded index. Under ROCm, torch enumerates GPUs in an order
that differs from rocm-smi/llama, so `cuda:0` is not reliable. It:
1. Lists every device (name/total/used/free).
2. Picks the one with the most **free** memory (not total — the eGPU may be busy).
3. Requires ≥4 GB free (else CPU fallback, fp32).
4. Uses **bf16** on GPU, **fp32** on CPU.

### Key findings (do not re-investigate)
- **`--ipc=host` is REQUIRED** when running the container directly via `podman
  run`. Without it, the ROCm HSA runtime fails with "Memory critical error"
  (exit 139). This is NOT transient — it's exactly because the flag was omitted.
  (AGENTS.md now mandates `--ipc=host` for all direct podman runs.)
- **`--no-flash-attn` always** — flash-attn is not installed under ROCm;
  transformers auto-enables it and crashes on load. Omit `attn_implementation`.
- **torchao int8 REJECTED** — saves VRAM (11% vs 14%) but slower (0.53x vs
  0.76x realtime) due to dequant overhead on ROCm.
- **flash-attn REJECTED** — segfaults (exit 139) on gfx1201 even with ROCm
  Triton backend.
- **bf16 is the best PyTorch path** — 0.76x realtime, 14% VRAM, warm.
- **1.7B VoiceDesign** works on GPU (cuda:0 R9700, bf16), 0.76x realtime, 14%
  VRAM, model kept warm (loaded once at startup).
- **0.6B Base** works too, 0.83x realtime.
- **Model kept warm** — loaded once at startup, reused for all requests.

### Weights location
Host safetensors snapshot, single file after download:
`${MODELS_DIR}/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`
(and `.../Qwen3-TTS-12Hz-0.6B-Base`). Bind-mounted into the container at
`/models/Qwen/<model>`.

---

## 4. Supertonic 3 — the utility tier (Piper replacement)

Non-autoregressive ONNX, CPU-first, 31 languages (incl. ru/de). Loads fast,
synthesizes much faster than realtime. Piper-tier quality.

### Files
| File | Purpose |
|------|---------|
| `pod/Containerfile.supertonic` | Image build (python:3.12-slim) |
| `pod/supertonic_openai_server.py` | OpenAI-compatible server |
| `unified/supertonic_tts.py` | Supertonic engine (unified container) |

### Container image
```dockerfile
FROM docker.io/library/python:3.12-slim
RUN apt-get install -y ffmpeg libsndfile1
RUN pip install "supertonic[serve]"
ENV HOST=0.0.0.0 PORT=8097
CMD ["python", "supertonic_openai_server.py"]
```
- `ffmpeg` + `libsndfile1` for mp3/wav encoding.
- `supertonic[serve]` pulls onnxruntime + the SDK + serve extra.
- No ROCm/GPU needed — pure CPU.

### Server env vars
| Var | Default | Purpose |
|-----|---------|---------|
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8097` | Listen port |
| `SUPERTONIC_VOICE` | `M3` | Default voice style (one of `F1`–`F5`, `M1`–`M5`) |
| `SUPERTONIC_LANG` | `en` | Default language (**NOT `auto`** — supertonic rejects auto) |
| `SUPERTONIC_STEPS` | `32` | Diffusion steps (1–100). Higher = better quality, slower. |
| `SUPERTONIC_SPEED` | `1.0` | Speed multiplier |

### Voice quality notes (measured)
- **Voice choice dominates perceived quality.** Female voices (`F1`–`F5`) are
  markedly more robust to low step counts; `M1` is the most step-sensitive and
  sounds "technical"/robotic below ~50 steps.
- **`F3` + 64 steps** was judged the best-sounding combination by ear.
- Steps are CPU-costly — roughly linear cost per step. 64 vs 32 roughly doubles
  synthesis time; raise `SUPERTONIC_STEPS` only when quality demands it.

### Model cache (bind-mounted)
- Model assets live at `/root/.cache/supertonic3` inside the container (~386 MB).
- The pod bind-mounts host `~/.cache/supertonic3` → `/root/.cache/supertonic3`
  so `TTS(auto_download=True)` does **not** re-download on every container start
  (was ~60 s + network per restart; now instant and offline).
- Pre-seed the host path once from a container that already has the assets:
  `podman cp <ctr>:/root/.cache/supertonic3/. ~/.cache/supertonic3/`

### Endpoints
`GET /health`, `GET /v1/models`, `POST /v1/audio/speech` (mp3 or wav).

### Key findings
- **`LANG` default is `en`, not `auto`** — supertonic rejects `auto`.
- **`voice = body.get("voice") or VOICE`** — empty voice from tts-ui must fall
  back to the default (M1).
- **`arr.squeeze()` needed** — wav is shape `(1, N)`.
- **`float(np.asarray(duration).item())`** — duration is a numpy scalar.
- Realtime factors (CPU): en 0.27x, ru 0.24x, de 0.22x — all >4x realtime.
- 44.1kHz output, 31 languages.
- Model auto-downloads assets on first run (`TTS(auto_download=True)`), loaded
  once at startup and kept warm.

---

## 5. Piper — CPU-first multilingual engine

- In the unified container: `unified/piper_tts.py` (port 8095).
- In pod mode: `pod/piper-tts-server.py` (port 5050).
- Voices: `en_GB-alan-medium`, `ru_RU-irina-medium`, `de_DE-thorsten-medium`.
- Voices persist in `~/.cache/piper/voices/` (bind-mounted).
- Each voice needs `.onnx` + `.onnx.json`.
- **Text cleaner**: `shared/text_cleaner.py` — step-based `preprocess()` runs
  inside the engine containers only.
- **Audio cache**: if TTS stops working, check Settings → Audio →
  disable "Cache audio" (mobile browsers can fail silently).

---

## 6. Ports

| Port | Owner | Notes |
|------|-------|-------|
| 8095 | piper (unified) / piper (pod) | TTS engine |
| 8096 | supertonic (unified) | TTS engine |
| 8097 | supertonic (pod) | TTS engine |
| 8099 | ui (unified) | Proxy + PWA |
| 8094 | ui (pod) | Proxy + PWA |
| 18110 | unified container host port | Exposed by `start.py` |
| 18100 | pod host port | Exposed by `start_pod.py` |

---

## 7. Hard rules & gotchas (from AGENTS.md + experience)

- **NEVER install anything on the host** — all deps live in containers.
- **Do NOT touch/restart the running pod or any stack container during
  development/experiments.** The "no touching stack" rule is for experiments.
- **No custom networks** — default only; reachability via
  `host.containers.internal`.
- **`--ipc=host` ALWAYS** when running a container directly via `podman run`
  (ROCm HSA requirement).
- **Use `127.0.0.1`, not `localhost`, in healthcheck curl** (host-side DNS/IPv6 quirk).
- **GPU detection by biggest free VRAM, never hardcoded index** — torch
  enumerates GPUs differently from rocm-smi/llama.
- **`--no-flash-attn` always** for Qwen3-TTS (flash-attn not installed under ROCm).
- **`sox` required** in the qwen-torch image (qwen_tts imports it at load).
- **mp3 concat-safety**: encode with `-id3v2_version 0 -write_xing 0` so chunks
  concatenate gaplessly.
- **`:z` (not `:Z`) on SELinux bind mounts** — stable relabel across restarts.

---

## 8. Open items / next steps

1. ~~Integration decision~~ — **Done**: both engines in unified container.
2. ~~Wire into the pod~~ — **Done**: pod mode is optional via `start_pod.py`.
3. **GPU mode tuning for Qwen3-TTS** — needs measurement at real-world VRAM
   loads.
4. **Language handling** — tts-ui doesn't pass `lang`; Qwen3-TTS needs a small
   tts-ui change for ru/de (Supertonic handles it natively).
5. **`Containerfile.audio8` / `audio8-0.6b`** — reference deleted
   `onnx_runtime_*` model dirs; either restore those dirs or remove these
   Containerfiles.

---

## 9. Commit history (TTS work, prior to re-initialization)

| Commit | What |
|--------|------|
| `6156dc5` | tts: add OpenAI-compatible Qwen3-TTS backend over llama-tts (CPU-first) |
| `3576a02` | tts: add PyTorch Qwen3-TTS backend (1.7B VoiceDesign) with GPU detection |
| `dbb8707` | tts-ui: fix silent multipart stream on Chrome (MediaSource+audio/mpeg) |
| `631ff12` | tts-ui: raise input cap to 100k, surface truncation, add chunk/char diagnostics |
| `7567d1d` | tts-ui: document streaming architecture/pitfalls |
| `8c1ecfd` | tts-ui: fix paste/manual-tap race (abort in-flight stream, reset player error state) |
| `f216ced` | tts-ui: use 100dvh so header isn't clipped under mobile browser chrome |
| `8ce6603` | tts-ui: fill viewport, textarea absorbs remaining height (no page scroll) |
