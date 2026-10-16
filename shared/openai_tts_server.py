#!/usr/bin/env python3
"""Generic OpenAI-compatible TTS server (FastAPI).

Holds ALL the shared HTTP / codec / auth / routing scaffolding for a TTS
backend. A specific backend file imports this, subclasses `TTSSynthesizer`,
and calls `run_server([...])` — the only thing a backend must provide is a
`Synthesizer.synthesize(text, voice, lang, instruct) -> (wav, sr)`.

Endpoints (OpenAI-compatible, matches the tts-ui contract):
    GET  /health            -> {"status":"ok","engine":<default>}
    GET  /v1/models         -> list of registered synthesizers
    GET  /v1/audio/voices   -> voices of the default synthesizer (optional)
    POST /v1/audio/speech   -> audio/mpeg (mp3) or audio/wav

Model routing: the client does NOT need to pick a model. The server uses a
default synthesizer (the first registered, or `DEFAULT_MODEL` env). If a
request DOES send a valid `model` field it is honored — so a model picker can
be added client-side later without touching the server.

Multiple synthesizers can be registered in one process (e.g. several models
baked into a single container). Each synthesizer serializes its own synthesis
via a per-name lock, so different models can run concurrently.

Env:
    HOST            bind host        (default 0.0.0.0)
    PORT            listen port      (default 8095)
    DEFAULT_MODEL   synthesizer name to use as default (default: first registered)
    TTS_API_KEY     optional Bearer key
    OUT_SR          output sample rate; 0 = keep native (default 0)
    TTS_LAZY_TTL    seconds a lazy backend stays loaded after last use before
                    being evicted; 0 (default) = preload at startup, no TTL
    TTS_MAX_INPUT_CHARS
                    reject inputs longer than this many chars with HTTP 413;
                    a bound so one abandoned long request cannot monopolize the
                    per-model lock for minutes (default 2000; 0 = no bound)
    TTS_CACHE_MAX_ENTRIES
                    in-process LRU cache size for (model, text, voice, lang,
                    instruct, format) -> encoded audio; 0 disables (default 64).
                    Cache lives only in the container, so it vanishes on
                    recreate — no stale artifacts.
"""

import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8095"))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
OUT_SR = int(os.environ.get("OUT_SR", "0"))
TTS_LAZY_TTL = int(os.environ.get("TTS_LAZY_TTL", "0"))
# Bound on input size. A sync FastAPI endpoint cannot be cancelled when the
# client disconnects, so an abandoned long request keeps holding the per-model
# lock until synthesis finishes. Rejecting oversized inputs up front bounds how
# long any single request can monopolize the lock (0 = no bound).
TTS_MAX_INPUT_CHARS = int(os.environ.get("TTS_MAX_INPUT_CHARS", "2000"))
# In-process LRU cache of encoded audio keyed by (model, text, voice, lang,
# instruct, format). Lives only in the container, so it disappears on recreate.
# The UI already chunks text into <=800-char pieces, so repeated chunks (and
# repeated UI clicks on the same sentence) hit this cache and skip synthesis.
TTS_CACHE_MAX_ENTRIES = int(os.environ.get("TTS_CACHE_MAX_ENTRIES", "64"))


# ── eGPU detection (GPU TTS backends) ────────────────────────────────────
# ROCm enumerates GPUs in an order that differs from rocm-smi/llama, and the
# eGPU is not always torch index 0. Hardcoding a CUDA index is fragile (AGENTS.md
# "never hardcode a CUDA index"). Instead we detect the eGPU by LARGEST TOTAL
# VRAM and restrict torch to it via CUDA_VISIBLE_DEVICES — so the backend's own
# pick_device() (which picks by biggest FREE VRAM) always sees the eGPU as cuda:0.
#
# The env var must be set BEFORE torch initializes CUDA. torch initializes CUDA
# lazily, so setting it at import time (before any torch.cuda call) is safe.
# Detection itself must enumerate GPUs BEFORE the env var is set (else torch only
# sees the filtered set), so we enumerate in a fresh subprocess.

_EGPU_PROBE = r"""
import json, sys, torch
devs = []
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    devs.append({"index": i, "name": p.name, "total": p.total_memory})
print(json.dumps(devs))
"""


def probe_gpus() -> list[dict]:
    """Return a list of {index, name, total} for every CUDA device torch sees.

    Runs torch in a fresh subprocess so CUDA is not initialized in this process
    before we set CUDA_VISIBLE_DEVICES. Returns [] if no CUDA device is found or
    the probe fails.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-c", _EGPU_PROBE],
            capture_output=True, timeout=60,
        )
        data = json.loads(out.stdout.decode().strip())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def detect_egpu_index() -> int | None:
    """Return the torch device index of the eGPU (largest total VRAM), or None.

    Returns None if no CUDA device is found or the probe fails (caller should
    then leave the env var unset).
    """
    devs = probe_gpus()
    if not devs:
        return None
    best = max(devs, key=lambda d: d["total"])
    return int(best["index"])


def auto_restrict_to_egpu():
    """If enabled and CUDA_VISIBLE_DEVICES is not already set, restrict torch to
    the eGPU. Opt-in via TTS_AUTO_EGPU=1 (set on GPU backends only — CPU backends
    skip the probe entirely)."""
    log = logging.getLogger("tts.egpu")
    if os.environ.get("TTS_AUTO_EGPU", "0") != "1":
        return
    if "CUDA_VISIBLE_DEVICES" in os.environ:
        log.info("CUDA_VISIBLE_DEVICES already set to %s — skipping eGPU probe",
                 os.environ["CUDA_VISIBLE_DEVICES"])
        return
    devs = probe_gpus()
    if not devs:
        log.warning("eGPU auto-detect: no CUDA device found — leaving CUDA_VISIBLE_DEVICES unset")
        return
    for d in devs:
        log.info("GPU cuda:%s: %s (total=%.1f GB)", d["index"], d["name"], d["total"] / 1024**3)
    best = max(devs, key=lambda d: d["total"])
    os.environ["CUDA_VISIBLE_DEVICES"] = str(best["index"])
    log.info("eGPU auto-detect: selected cuda:%s (%s) — restricted torch to CUDA_VISIBLE_DEVICES=%s",
             best["index"], best["name"], best["index"])


auto_restrict_to_egpu()



# ── Shared GPU device selection (GPU TTS backends) ───────────────────────
# The biggest-FREE-VRAM device picker and attention resolver are shared by all
# GPU backends (moss, moss-local, qwen). They live HERE in the generic server so
# a backend never re-implements GPU detection. torch is imported lazily inside
# the functions so CPU-only backends (piper, supertonic, cutetts, kokoro) that
# import this module do not need torch installed.

def _fmt_mem(gib: float) -> str:
    return f"{gib:.1f} GB"


def pick_device(force_device: str = "auto", min_free_gib: float = 4.0,
                cpu_dtype=None, gpu_dtype=None) -> tuple:
    """Return (device, dtype) — CUDA on the biggest-FREE-VRAM GPU if it fits, else CPU.

    The CUDA index is NOT hardcoded: under ROCm torch enumerates GPUs in an order
    that differs from rocm-smi/llama, so we never assume cuda:0. We pick by FREE
    memory (not total), because the eGPU may already be occupied by other work in
    this shared host/session and a big-total card can still OOM.

    Prints every available device with name/total/used/free, then the selection.

    Args:
        force_device: "auto" (default) | "cpu" | "cuda". "cpu"/"cuda" force the
            device (with a CPU fallback if cuda is forced but unavailable).
        min_free_gib: minimum free VRAM (GiB) required to select a GPU; below
            this the backend falls back to CPU. Default 4.0.
        cpu_dtype / gpu_dtype: dtypes to use for CPU / GPU. Defaults to
            torch.float32 / torch.bfloat16.
    """
    import torch
    if cpu_dtype is None:
        cpu_dtype = torch.float32
    if gpu_dtype is None:
        gpu_dtype = torch.bfloat16
    log = logging.getLogger("tts.device")

    if force_device == "cpu":
        log.info("[device] forced CPU (dtype=%s)", cpu_dtype)
        return "cpu", cpu_dtype
    if force_device == "cuda":
        if not torch.cuda.is_available():
            log.warning("[device] forced cuda but CUDA unavailable -> CPU (dtype=%s)", cpu_dtype)
            return "cpu", cpu_dtype
        return "cuda", gpu_dtype

    if not torch.cuda.is_available():
        log.info("[device] CUDA not available -> CPU (dtype=%s)", cpu_dtype)
        return "cpu", cpu_dtype

    n = torch.cuda.device_count()
    log.info("[device] CUDA available: %d device(s) detected:", n)
    best_idx, best_free = 0, -1.0
    for i in range(n):
        p = torch.cuda.get_device_properties(i)
        total = p.total_memory / 1024**3
        _, free = torch.cuda.mem_get_info(i)
        free_gib = free / 1024**3
        used_gib = total - free_gib
        log.info("  cuda:%d: %s | total=%s used=%s free=%s",
                 i, p.name, _fmt_mem(total), _fmt_mem(used_gib), _fmt_mem(free_gib))
        if free_gib > best_free:
            best_free, best_idx = free_gib, i

    if best_free < min_free_gib:
        log.info("[device] max free %s < min %.1f GB -> CPU fallback (dtype=%s)",
                 _fmt_mem(best_free), min_free_gib, cpu_dtype)
        return "cpu", cpu_dtype

    name = torch.cuda.get_device_properties(best_idx).name
    log.info("[device] selected cuda:%d (%s, free=%s) -> dtype=%s",
             best_idx, name, _fmt_mem(best_free), gpu_dtype)
    return f"cuda:{best_idx}", gpu_dtype


def resolve_attn_implementation(device: str, dtype) -> str:
    """Pick the attention implementation for the given device/dtype.

    Mirrors the model's own recommended logic:
      * flash_attention_2 — only when device==cuda AND flash_attn is installed
                            AND dtype is fp16/bf16 AND GPU capability >= 8.
      * sdpa              — any other CUDA device (ROCm exposes as cuda but
                            flash_attn is not installed -> sdpa).
      * eager             — CPU.
    """
    import torch
    import importlib.util
    # pick_device() returns "cuda:<idx>" (e.g. "cuda:1"), so match on the
    # prefix, not the bare "cuda" string — otherwise CUDA always falls through
    # to eager.
    is_cuda = device.startswith("cuda")
    if (
        is_cuda
        and importlib.util.find_spec("flash_attn") is not None
        and dtype in {torch.float16, torch.bfloat16}
    ):
        major, _ = torch.cuda.get_device_capability()
        if major >= 8:
            return "flash_attention_2"

    if is_cuda:
        return "sdpa"

    return "eager"

class TTSSynthesizer:
    """Base class a specific backend implements.

    `name` is the model id exposed in /v1/models and used for routing.
    `voices` is an optional list of available voice names.
    `synthesize()` must return (float32 mono wav, sample_rate).

    Lifecycle (lazy loading):
      * `lazy` — if True, the backend is NOT loaded at startup. `load()` is
        called on first use and the model is evicted (unloaded) after
        `TTS_LAZY_TTL` seconds of inactivity. If False (default), the backend
        loads in `__init__` and stays resident forever.
      * `load()` — idempotent; must be safe to call multiple times. Default
        just marks the backend loaded (for backends that load in `__init__`).
      * `unload()` — release the model (free VRAM). Default is a no-op.
      * `is_loaded()` — whether the model is currently resident.
    """

    name: str = "tts"
    voices: list[str] = []
    lazy: bool = False

    def __init__(self):
        self._loaded = not self.lazy

    def load(self):
        self._loaded = True

    def unload(self):
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def synthesize(self, text: str, voice: str = "", lang: str = "", instruct: str = "") -> tuple[np.ndarray, int]:
        raise NotImplementedError


class LazyTTSSynthesizer:
    """Wraps a lazy backend to load-on-first-use and evict after a TTL.

    All lifecycle transitions (load / synthesize / unload) are serialized
    under a single lock so a request can never race an eviction. The wrapper
    records `_last_used` on every synthesize; a background thread (started by
    `run_server`) calls `maybe_evict()` periodically and unloads any backend
    idle for >= TTS_LAZY_TTL seconds.

    The wrapper is transparent to the HTTP layer: it exposes the same
    `name` / `voices` / `synthesize()` interface as the wrapped backend.
    """

    def __init__(self, inner: TTSSynthesizer, ttl: int):
        self.inner = inner
        self.ttl = ttl
        self.lock = threading.Lock()
        self._last_used = 0.0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def voices(self) -> list[str]:
        return self.inner.voices

    def synthesize(self, text, voice="", lang="", instruct=""):
        with self.lock:
            if not self.inner.is_loaded():
                log = logging.getLogger("tts.lazy")
                log.info("lazy %s: loading on first use", self.inner.name)
                self.inner.load()
            self._last_used = time.monotonic()
            return self.inner.synthesize(text, voice, lang, instruct)

    def maybe_evict(self):
        """Unload the backend if it has been idle for >= ttl seconds."""
        if not self.inner.is_loaded():
            return
        idle = time.monotonic() - self._last_used
        if idle >= self.ttl:
            with self.lock:
                # Re-check under the lock: a request may have started since the
                # idle check above.
                if not self.inner.is_loaded():
                    return
                idle = time.monotonic() - self._last_used
                if idle >= self.ttl:
                    log = logging.getLogger("tts.lazy")
                    log.info("lazy %s: idle %.0fs >= ttl %ds — unloading",
                             self.inner.name, idle, self.ttl)
                    self.inner.unload()


class TTSRequest(BaseModel):
    model: str = ""
    input: str
    voice: str = ""
    response_format: str = "mp3"
    lang: str = ""
    instruct: str = ""


# ── Shared codecs ──────────────────────────────────────────────────────────

def _resample(wav: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample float32 PCM to dst_sr via ffmpeg (linear/soxr)."""
    if src_sr == dst_sr:
        return wav
    pcm = np.asarray(wav, dtype=np.float32).tobytes()
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "f32le", "-ar", str(src_sr), "-ac", "1", "-i", "pipe:0",
         "-ar", str(dst_sr), "-f", "f32le", "pipe:1"],
        input=pcm, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return wav
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _to_mp3(wav: np.ndarray, sr: int) -> bytes | None:
    """Encode float32 PCM to mp3 via ffmpeg with concat-safe flags.

    No leading metadata (ID3v2) and no Xing/Info TOC frame so tts-ui can
    concatenate chunk byte-streams into one gapless audio/mpeg track.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
             "-id3v2_version", "0", "-write_xing", "0",
             "-b:a", "64k", "-f", "mp3", "pipe:1"],
            input=wav.tobytes(), capture_output=True, timeout=60)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except FileNotFoundError:
        pass
    return None


def _to_wav(wav: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.asarray(wav, dtype=np.float32), sr, format="WAV")
    return buf.getvalue()


# ── Encoded-audio LRU cache ───────────────────────────────────────────────

class AudioCache:
    """Bounded in-process LRU cache of encoded audio bytes.

    Keyed by (model, text, voice, lang, instruct, format) so identical
    requests skip both synthesis AND encoding. Insertion / lookup are
    serialized by a lock; the cache is intentionally small and process-local
    (lives only in the container, vanishes on recreate — no stale artifacts).

    A cached value is a (content_type, body) pair.
    """

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._store: "OrderedDict[tuple, tuple[str, bytes]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> tuple[str, bytes] | None:
        if self.max_entries <= 0:
            return None
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: tuple, value: tuple[str, bytes]):
        if self.max_entries <= 0:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)


# ── App factory ───────────────────────────────────────────────────────────

def build_app(synthesizers: list[TTSSynthesizer]) -> FastAPI:
    if not synthesizers:
        raise ValueError("build_app requires at least one synthesizer")

    by_name = {s.name: s for s in synthesizers}
    default = by_name.get(DEFAULT_MODEL) or synthesizers[0]
    locks = {s.name: threading.Lock() for s in synthesizers}
    cache = AudioCache(TTS_CACHE_MAX_ENTRIES)
    log = logging.getLogger("tts.cache")
    app = FastAPI(title="OpenAI TTS", version="0.1.0")

    def verify_key(authorization: str | None = Header(default=None)):
        if not TTS_API_KEY:
            return
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        token = authorization.removeprefix("Bearer ").removeprefix("bearer ")
        if token != TTS_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": default.name}

    @app.get("/v1/models")
    def models(dep=Depends(verify_key)):
        return {
            "object": "list",
            "data": [{"id": s.name, "object": "model", "owned_by": "local", "created": 0}
                     for s in synthesizers],
        }

    @app.get("/v1/audio/voices")
    def voices(dep=Depends(verify_key)):
        return {"voices": default.voices}

    @app.get("/cache/stats")
    def cache_stats(dep=Depends(verify_key)):
        with cache._lock:
            size = len(cache._store)
        return {
            "entries": size,
            "max_entries": cache.max_entries,
            "hits": cache.hits,
            "misses": cache.misses,
        }

    @app.post("/v1/audio/speech")
    def speech(req: TTSRequest, dep=Depends(verify_key)):
        text = req.input.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Input text is empty")
        if TTS_MAX_INPUT_CHARS and len(text) > TTS_MAX_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Input text too long: {len(text)} chars "
                    f"(max {TTS_MAX_INPUT_CHARS})."
                ),
            )

        # Route by model if a valid one is given, else the default.
        synth = by_name.get(req.model) or default
        fmt = (req.response_format or "mp3").lower()

        # Cache key: everything that can change the rendered bytes.
        key = (synth.name, text, req.voice, req.lang, req.instruct, fmt)
        cached = cache.get(key)
        if cached is not None:
            ctype, body = cached
            log.info(
                "cache hit model=%s chars=%d hits=%d misses=%d",
                synth.name, len(text), cache.hits, cache.misses,
            )
            return Response(content=body, media_type=ctype)
        log.info(
            "cache miss model=%s chars=%d hits=%d misses=%d",
            synth.name, len(text), cache.hits, cache.misses,
        )

        with locks[synth.name]:
            wav, sr = synth.synthesize(text, req.voice, req.lang, req.instruct)

        arr = np.asarray(wav, dtype=np.float32).squeeze()
        if OUT_SR and sr != OUT_SR:
            arr = _resample(arr, sr, OUT_SR)
            sr = OUT_SR

        if fmt in ("mp3", "mpeg"):
            mp3 = _to_mp3(arr, sr)
            if mp3 is not None:
                cache.put(key, ("audio/mpeg", mp3))
                return Response(content=mp3, media_type="audio/mpeg")
        body = _to_wav(arr, sr)
        cache.put(key, ("audio/wav", body))
        return Response(content=body, media_type="audio/wav")

    return app


def run_server(synthesizers: list[TTSSynthesizer]):
    import uvicorn

    # Wrap lazy backends (when a TTL is configured) so they load on first use
    # and are evicted after TTS_LAZY_TTL seconds of inactivity. Non-lazy
    # backends (and lazy ones with TTL=0) are used as-is.
    if TTS_LAZY_TTL > 0:
        wrapped = [
            LazyTTSSynthesizer(s, TTS_LAZY_TTL) if s.lazy else s
            for s in synthesizers
        ]
        lazy_wrappers = [w for w in wrapped if isinstance(w, LazyTTSSynthesizer)]
        if lazy_wrappers:
            log = logging.getLogger("tts.lazy")
            log.info("lazy loading enabled: ttl=%ds backends=%s",
                     TTS_LAZY_TTL, [w.name for w in lazy_wrappers])

            def _evict_loop():
                while True:
                    time.sleep(2)
                    for w in lazy_wrappers:
                        try:
                            w.maybe_evict()
                        except Exception:  # noqa: BLE001 - never kill the loop
                            log.exception("lazy eviction error for %s", w.name)

            threading.Thread(target=_evict_loop, daemon=True, name="tts-lazy-evict").start()
    else:
        wrapped = synthesizers

    app = build_app(wrapped)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    raise SystemExit("openai_tts_server.py is a library — run a specific backend file instead.")
