#!/usr/bin/env python3
"""OpenAI-compatible TTS server using Piper.

Drop-in replacement for Kokoro TTS — same /v1/audio/speech endpoint.
Piper is much faster (near real-time on CPU) and supports 40+ languages
including Russian, Japanese, Chinese, etc.

Usage:
    python3 piper-tts-server.py [--host 0.0.0.0] [--port 5050]

Environment:
    PIPER_VOICES_DIR   Directory with Piper .onnx model files (default: /piper/voices)
    TTS_API_KEY        Optional API key for authentication (default: none)
"""

import argparse
import io
import json
import logging
import os
import wave
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Response, Depends
from pydantic import BaseModel
from piper import PiperVoice

import text_cleaner

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("piper-tts")

# ── Config ────────────────────────────────────────────────────────────────
VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/piper/voices"))
TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
DEFAULT_VOICE = os.environ.get("PIPER_DEFAULT_VOICE", "en_US-lessac-medium")


# ── Voice management ─────────────────────────────────────────────────────
def _discover_voices() -> dict[str, Path]:
    """Find all .onnx voice model files in the voices directory."""
    voices: dict[str, Path] = {}
    if not VOICES_DIR.exists():
        log.warning(f"Voices directory not found: {VOICES_DIR}")
        return voices
    for onnx_file in VOICES_DIR.rglob("*.onnx"):
        # Voice name is the stem, e.g. "en_US-lessac-medium"
        name = onnx_file.stem
        # Skip quantized variants as separate entries (they share the same name prefix)
        voices[name] = onnx_file
    return voices


VOICE_PATHS: dict[str, Path] = {}
_loaded_voices: dict[str, PiperVoice] = {}


def _get_voice(voice_name: str) -> PiperVoice:
    """Load a Piper voice model (cached)."""
    if voice_name in _loaded_voices:
        return _loaded_voices[voice_name]

    # Find the model file
    model_path = VOICE_PATHS.get(voice_name)
    if model_path is None:
        raise HTTPException(
                status_code=404,
                detail=f"Voice '{voice_name}' not found. Available: {', '.join(sorted(VOICE_PATHS)[:20])}",
            )

    config_path = model_path.with_suffix(".onnx.json")
    log.info(f"Loading Piper voice: {voice_name} ({model_path.name})")
    pv = PiperVoice.load(str(model_path), config_path=str(config_path) if config_path.exists() else None)
    _loaded_voices[voice_name] = pv
    return pv


# ── FastAPI app ──────────────────────────────────────────────────────────
app = FastAPI(title="Piper TTS", version="0.1.0")


@app.on_event("startup")
async def startup():
    global VOICE_PATHS
    VOICE_PATHS = _discover_voices()
    log.info(f"Found {len(VOICE_PATHS)} Piper voices: {', '.join(sorted(VOICE_PATHS))}")
    # Pre-load default voice
    if DEFAULT_VOICE in VOICE_PATHS:
        _get_voice(DEFAULT_VOICE)
        log.info(f"Pre-loaded default voice: {DEFAULT_VOICE}")


# ── Auth ──────────────────────────────────────────────────────────────────
def verify_key(authorization: str | None = Header(default=None)):
    if not TTS_API_KEY:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.removeprefix("Bearer ").removeprefix("bearer ")
    if token != TTS_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request model ───────────────────────────────────────────────────────
class TTSRequest(BaseModel):
    model: str = "piper"
    input: str
    voice: str = DEFAULT_VOICE
    # MP3 default: UI concatenates per-chunk WAV responses for long
    # text (>=4096 chars), and WAV concatenation is invalid (browser decodes
    # only the first chunk's header). MP3 concatenation is valid, so defaulting
    # to mp3 fixes truncated playback for long text. See textToSpeech path.
    response_format: str = "mp3"
    speed: float = 1.0


# ── Mapping from Kokoro voice names to Piper equivalents ─────────────────
KOKORO_TO_PIPER: dict[str, str] = {
    # Kokoro voice names → Piper equivalents
    "af_sarah": "en_US-lessac-medium",
    "af_bella": "en_US-lessac-medium",
    "am_adam": "en_US-lessac-medium",
    "am_michael": "en_US-lessac-medium",
    "af_nicole": "en_US-lessac-medium",
    "bf_emma": "en_US-lessac-medium",
    "bm_george": "en_US-lessac-medium",
    "ALL": "en_US-lessac-medium",  # default
}


def _resolve_voice(voice_name: str) -> str:
    """Map Kokoro-style voice names to Piper voice names."""
    # Direct match in Piper voices
    if voice_name in VOICE_PATHS:
        return voice_name
    # Kokoro → Piper mapping
    if voice_name in KOKORO_TO_PIPER:
        piper_voice = KOKORO_TO_PIPER[voice_name]
        if piper_voice in VOICE_PATHS:
            return piper_voice
    # Language prefix match (e.g. "ru" → "ru_RU-irina-medium")
    for piper_name in VOICE_PATHS:
        if piper_name.startswith(voice_name.split("_")[0] + "_"):
            return piper_name
    # Fallback to default
    return DEFAULT_VOICE


# ── Language detection ───────────────────────────────────────────────────
# Map ISO 639-1 language codes (langdetect output) → Piper voice names.
# Only languages with an installed voice should be listed here.
LANG_TO_VOICE: dict[str, str] = {
    "en": "en_US-lessac-medium",
    "ru": "ru_RU-irina-medium",
    "de": "de_DE-thorsten-medium",
}

# Character-range heuristic fallback (used when langdetect is unavailable
# or returns a language with no installed voice). Keyed by ISO 639-1 code.
_SCRIPT_HINTS: list[tuple[str, str]] = [
    # Cyrillic → Russian
    ("ru", "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"),
    # German umlauts / ß
    ("de", "äöüßÄÖÜ"),
]

# Cache the detector (langdetect's DetectorFactory is stateful; reuse it)
_detector = None


def _get_detector():
    """Lazily import and initialize the langdetect detector."""
    global _detector
    if _detector is None:
        from langdetect import DetectorFactory, detect_langs
        DetectorFactory.seed = 0  # deterministic results
        _detector = detect_langs
    return _detector


def _detect_language(text: str) -> str | None:
    """Detect the language of `text`, returning an ISO 639-1 code or None.

    Fallback chain:
      1. langdetect (if installed) — most robust
      2. Character-range heuristic — Cyrillic → ru, umlauts → de
      3. None (caller falls back to default voice)
    """
    # 1. langdetect
    try:
        detect_langs = _get_detector()
        langs = detect_langs(text)
        if langs:
            # Highest-confidence language
            return langs[0].lang
    except Exception as e:
        log.warning(f"langdetect failed, falling back to heuristic: {e}")

    # 2. Character-range heuristic
    for lang, chars in _SCRIPT_HINTS:
        if any(c in chars for c in text):
            return lang

    return None


def _voice_for_language(lang: str | None) -> str | None:
    """Return the Piper voice for a detected language, or None if no match."""
    if lang is None:
        return None
    return LANG_TO_VOICE.get(lang)


def _auto_voice(text: str) -> str:
    """Pick a voice by detecting the language of `text`.

    Falls back to the default voice when detection fails or the detected
    language has no installed voice.
    """
    lang = _detect_language(text)
    voice = _voice_for_language(lang)
    if voice is None:
        log.info(f"TTS: no voice for detected lang={lang!r}, using default {DEFAULT_VOICE}")
        return DEFAULT_VOICE
    return voice


# ── Endpoints ────────────────────────────────────────────────────────────
@app.get("/v1/models")
async def list_models(dep=Depends(verify_key)):
    return {
        "object": "list",
        "data": [{"id": "piper", "object": "model", "owned_by": "local", "created": 0}],
    }


@app.get("/v1/audio/voices")
async def list_voices(dep=Depends(verify_key)):
    """List available Piper voices."""
    return {"voices": sorted(VOICE_PATHS.keys())}


@app.post("/v1/audio/speech")
async def create_speech(req: TTSRequest, dep=Depends(verify_key)):
    """OpenAI-compatible TTS endpoint.

    Maps Kokoro voice names to Piper equivalents automatically.
    Text exceeding Piper's sentence limit is chunked automatically
    by Piper's built-in phonemizer.
    """
    text = req.input
    if not text.strip():
        raise HTTPException(status_code=400, detail="Input text is empty")

    # Auto-detect language and pick the matching voice. Falls back to the
    # requested voice (then default) when detection yields no match.
    auto_voice = _auto_voice(text)
    voice_name = auto_voice if auto_voice in VOICE_PATHS else _resolve_voice(req.voice)
    pv = _get_voice(voice_name)

    # Preprocess text (markdown/HTML cleanup) before synthesis.
    # The pipeline is easily extensible and fully toggleable via
    # PIPER_CLEAN* env vars — see text_cleaner.py.
    raw_text = text
    text = text_cleaner.preprocess(text)
    if text != raw_text:
        log.info(f"TTS: cleaned input via {len(text_cleaner.active_steps())} steps")

    log.info(f"TTS: voice={req.voice}→{voice_name} speed={req.speed} text='{text[:60]}...'")

    # Speed: Piper uses length_scale (higher = slower), so invert speed
    length_scale = 1.0 / max(req.speed, 0.25)  # clamp to avoid extreme values

    # Synthesize audio using Piper
    try:
        from piper.config import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=length_scale)

        audio_int16 = b""
        for chunk in pv.synthesize(text, syn_config=syn_config):
            audio_int16 += chunk.audio_int16_bytes
        sample_rate = pv.config.sample_rate

        # Wrap raw PCM into WAV
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_int16)

        audio_bytes = wav_buf.getvalue()
        log.info(f"TTS: generated {len(audio_bytes)} bytes WAV (voice={voice_name}, sr={sample_rate})")

    except Exception as e:
        log.error(f"TTS generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if req.response_format == "mp3":
        try:
            import subprocess
            proc = subprocess.run(
                ["ffmpeg", "-f", "wav", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
                input=audio_bytes,
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return Response(content=proc.stdout, media_type="audio/mpeg")
        except FileNotFoundError:
            pass

    return Response(content=audio_bytes, media_type="audio/wav")


@app.get("/health")
async def health():
    return {"status": "ok", "voices": len(VOICE_PATHS)}


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Piper TTS Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)