#!/usr/bin/env python3
"""Supertonic 3 TTS — OpenAI-compatible server (matches tts-ui contract).

Endpoints:
  GET  /health          -> {"status":"ok"}
  GET  /v1/models       -> {"object":"list","data":[{"id":"supertonic-3",...}]}
  POST /v1/audio/speech -> audio/mpeg (mp3) or audio/wav

Request body (OpenAI TTS):
  {"model": "...", "input": "text", "voice": "M1", "response_format": "mp3"}

Supertonic is non-autoregressive ONNX and CPU-first, so it loads fast and
synthesizes much faster than realtime. Model is loaded once at startup and
kept resident (warm) for subsequent requests.
"""
import io
import os
import threading
import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from supertonic import TTS

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8097"))
VOICE = os.environ.get("SUPERTONIC_VOICE", "M1")
LANG = os.environ.get("SUPERTONIC_LANG", "en")
# Synthesis quality knob: more steps = better quality, slower. Default 8 in
# the SDK; we run 32 (env-overridable) since 8 sounds robotic on this model.
STEPS = int(os.environ.get("SUPERTONIC_STEPS", "32"))
# Output sample rate. Supertonic synthesizes at 44.1 kHz natively; resample to
# this rate before encoding. 22.05/24 kHz at 64k mp3 sounds cleaner than 44.1k.
OUT_SR = int(os.environ.get("SUPERTONIC_SAMPLE_RATE", "44100"))

app = FastAPI()
_synth_lock = threading.Lock()

# Load model once at startup (warm). Supertonic auto-downloads assets on first run.
print("[supertonic] loading model (auto_download)...", flush=True)
TTS_ENGINE = TTS(auto_download=True)
print(f"[supertonic] model loaded. voice={VOICE} lang={LANG}", flush=True)


def _resample(wav: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample float32 PCM to dst_sr via ffmpeg (linear/soxr)."""
    if src_sr == dst_sr:
        return wav
    import subprocess
    pcm = np.asarray(wav, dtype=np.float32).tobytes()
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "f32le", "-ar", str(src_sr), "-ac", "1", "-i", "pipe:0",
         "-ar", str(dst_sr), "-f", "f32le", "pipe:1"],
        input=pcm, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return wav
    out = np.frombuffer(proc.stdout, dtype=np.float32)
    return out.copy()


def _to_mp3(wav: np.ndarray, sr: int) -> bytes:
    """Encode float32 PCM to mp3 via ffmpeg (concat-safe flags)."""
    import subprocess
    pcm = np.asarray(wav, dtype=np.float32).tobytes()
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
         "-codec:a", "libmp3lame", "-b:a", "64k",
         "-id3v2_version", "0", "-write_xing", "0", "-f", "mp3", "pipe:1"],
        input=pcm, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return b""
    return proc.stdout


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "engine": "supertonic-3"})


@app.get("/v1/models")
def models() -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [{"id": "supertonic-3", "object": "model", "owned_by": "supertone"}],
    })


@app.post("/v1/audio/speech")
async def speech(req: Request) -> Response:
    body = await req.json()
    text = body.get("input", "")
    voice = body.get("voice") or VOICE
    fmt = body.get("response_format", "mp3")
    lang = body.get("lang", LANG)
    # Per-request steps override; falls back to env/default (32).
    steps = int(body.get("steps") or STEPS)
    if not text:
        return Response(status_code=400, content="missing 'input'")

    with _synth_lock:
        style = TTS_ENGINE.get_voice_style(voice_name=voice)
        wav, _ = TTS_ENGINE.synthesize(text, voice_style=style, lang=lang, total_steps=steps)
    arr = np.asarray(wav, dtype=np.float32).squeeze()   # (1,N) -> (N,)
    sr = TTS_ENGINE.sample_rate
    if sr != OUT_SR:
        arr = _resample(arr, sr, OUT_SR)
        sr = OUT_SR

    if fmt == "wav":
        buf = io.BytesIO()
        sf.write(buf, arr, sr, format="WAV")
        return Response(content=buf.getvalue(), media_type="audio/wav")

    mp3 = _to_mp3(arr, sr)
    if mp3:
        return Response(content=mp3, media_type="audio/mpeg")
    # ffmpeg unavailable -> fall back to wav
    buf = io.BytesIO()
    sf.write(buf, arr, sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
