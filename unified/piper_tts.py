#!/usr/bin/env python3
"""Piper TTS — specific backend for the generic OpenAI TTS server.

Keeps Piper's model-specific logic (voice discovery, language detection,
text cleaning, Kokoro voice mapping) and delegates HTTP/codec to the generic
server.

Run:  python3 piper_tts.py
Env:  PIPER_VOICES_DIR     dir with Piper .onnx models under CONTAINER_CACHE_DIR
      PIPER_DEFAULT_VOICE  default voice (default en_US-lessac-medium)
"""

import logging
import os
import urllib.request
from pathlib import Path

import numpy as np
from piper import PiperVoice

import text_cleaner
from openai_tts_server import TTSSynthesizer, run_server

PIPER_HUB_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
VOICE_MAP = {
    "en_US-lessac-medium": "en/en_US/lessac/medium",
    "ru_RU-irina-medium": "ru/ru_RU/irina/medium",
    "de_DE-thorsten-medium": "de/de_DE/thorsten/medium",
    "es_ES-gpu-medium": "es/es_ES/gpu/medium",
    "fr_FR-siwis-medium": "fr/fr_FR/siwis/medium",
    "it_IT-giulia-medium": "it/it_IT/giulia/medium",
    "pl_PL-krzysiek-medium": "pl/pl_PL/krzysiek/medium",
}

log = logging.getLogger("piper-tts")

CONTAINER_CACHE_DIR = "/root/.cache"  # host dir mounted by start.py (TTS_MODELS_ROOT)

VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", f"{CONTAINER_CACHE_DIR}/piper"))

def _download_voice(voice_name: str) -> bool:
    rel_path = VOICE_MAP.get(voice_name)
    if not rel_path:
        return False
    try:
        dest_dir = VOICES_DIR / rel_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        for ext in [".onnx", ".onnx.json"]:
            filename = f"{voice_name}{ext}"
            dest_file = dest_dir / filename
            if dest_file.exists():
                continue
            url = f"{PIPER_HUB_URL}{rel_path}/{filename}"
            log.info(f"Downloading {filename} from {url}...")
            urllib.request.urlretrieve(url, dest_file)
        return True
    except Exception as e:
        log.error(f"Failed to download voice {voice_name}: {e}")
        return False
DEFAULT_VOICE = os.environ.get("PIPER_DEFAULT_VOICE", "en_US-lessac-medium")


# ── Voice management ─────────────────────────────────────────────────────
def _discover_voices() -> dict[str, Path]:
    voices: dict[str, Path] = {}
    if not VOICES_DIR.exists():
        log.warning(f"Voices directory not found: {VOICES_DIR}")
        return voices
    for onnx_file in VOICES_DIR.rglob("*.onnx"):
        voices[onnx_file.stem] = onnx_file
    return voices


VOICE_PATHS: dict[str, Path] = {}
_loaded_voices: dict[str, PiperVoice] = {}


def _get_voice(voice_name: str) -> PiperVoice:
    global VOICE_PATHS
    if voice_name in _loaded_voices:
        return _loaded_voices[voice_name]
    model_path = VOICE_PATHS.get(voice_name)
    if model_path is None:
        if voice_name in VOICE_MAP:
            log.info(f"Voice '{voice_name}' not found locally, attempting download...")
            if _download_voice(voice_name):
                VOICE_PATHS = _discover_voices()
                model_path = VOICE_PATHS.get(voice_name)
        if model_path is None:
            raise ValueError(f"Voice '{voice_name}' not found. Available: {', '.join(sorted(VOICE_PATHS.keys())[:20])}")
    config_path = model_path.with_suffix(".onnx.json")
    log.info(f"Loading Piper voice: {voice_name} ({model_path.name})")
    pv = PiperVoice.load(str(model_path), config_path=str(config_path) if config_path.exists() else None)
    _loaded_voices[voice_name] = pv
    return pv


# ── Kokoro → Piper voice mapping ─────────────────────────────────────────
KOKORO_TO_PIPER: dict[str, str] = {
    "af_sarah": "en_US-lessac-medium",
    "af_bella": "en_US-lessac-medium",
    "am_adam": "en_US-lessac-medium",
    "am_michael": "en_US-lessac-medium",
    "af_nicole": "en_US-lessac-medium",
    "bf_emma": "en_US-lessac-medium",
    "bm_george": "en_US-lessac-medium",
    "ALL": "en_US-lessac-medium",
}


def _resolve_voice(voice_name: str) -> str:
    if voice_name in VOICE_PATHS or voice_name in VOICE_MAP:
        return voice_name
    if voice_name in KOKORO_TO_PIPER:
        piper_voice = KOKORO_TO_PIPER[voice_name]
        if piper_voice in VOICE_PATHS or piper_voice in VOICE_MAP:
            return piper_voice
    for piper_name in VOICE_PATHS:
        if piper_name.startswith(voice_name.split("_")[0] + "_"):
            return piper_name
    return DEFAULT_VOICE


# ── Language detection ───────────────────────────────────────────────────
LANG_TO_VOICE: dict[str, str] = {
    "en": "en_US-lessac-medium",
    "ru": "ru_RU-irina-medium",
    "de": "de_DE-thorsten-medium",
}

_SCRIPT_HINTS: list[tuple[str, str]] = [
    ("ru", "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"),
    ("de", "äöüßÄÖÜ"),
]

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        from langdetect import DetectorFactory, detect_langs
        DetectorFactory.seed = 0
        _detector = detect_langs
    return _detector


def _detect_language(text: str) -> str | None:
    try:
        detect_langs = _get_detector()
        langs = detect_langs(text)
        if langs:
            return langs[0].lang
    except Exception as e:
        log.warning(f"langdetect failed, falling back to heuristic: {e}")
    for lang, chars in _SCRIPT_HINTS:
        if any(c in chars for c in text):
            return lang
    return None


def _auto_voice(text: str) -> str:
    lang = _detect_language(text)
    voice = LANG_TO_VOICE.get(lang) if lang else None
    if voice is None:
        log.info(f"TTS: no voice for detected lang={lang!r}, using default {DEFAULT_VOICE}")
        return DEFAULT_VOICE
    return voice


class PiperSynthesizer(TTSSynthesizer):
    name = "piper"

    def __init__(self):
        global VOICE_PATHS
        VOICE_PATHS = _discover_voices()
        
        # Ensure default voice is available
        if DEFAULT_VOICE not in VOICE_PATHS and DEFAULT_VOICE in VOICE_MAP:
            log.info(f"Default voice {DEFAULT_VOICE} missing, downloading...")
            if _download_voice(DEFAULT_VOICE):
                VOICE_PATHS = _discover_voices()

        # Combine locally discovered voices and those available for download
        all_available = set(VOICE_PATHS.keys()) | set(VOICE_MAP.keys())
        self.voices = sorted(all_available)
        log.info(f"Found {len(self.voices)} Piper voices available (local + hub): {', '.join(self.voices)}")
        if DEFAULT_VOICE in VOICE_PATHS:
            _get_voice(DEFAULT_VOICE)
            log.info(f"Pre-loaded default voice: {DEFAULT_VOICE}")

    def synthesize(self, text, voice="", lang="", instruct=""):
        # 1. Priority: Explicit voice selection from UI
        # 2. Priority: Auto-detection based on text (if no explicit voice or if voice is default)
        # 3. Priority: Default voice
        
        voice_name = voice if voice else DEFAULT_VOICE
        
        if voice_name == DEFAULT_VOICE:
            auto_voice = _auto_voice(text)
            if auto_voice in VOICE_PATHS or auto_voice in VOICE_MAP:
                voice_name = auto_voice
        
        # Final resolution to ensure the voice exists in either local or hub
        voice_name = _resolve_voice(voice_name)
        pv = _get_voice(voice_name)

        raw_text = text
        text = text_cleaner.preprocess(text)
        if text != raw_text:
            log.info(f"TTS: cleaned input via {len(text_cleaner.active_steps())} steps")

        from piper.config import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=1.0)

        audio_int16 = b""
        for chunk in pv.synthesize(text, syn_config=syn_config):
            audio_int16 += chunk.audio_int16_bytes
        sample_rate = pv.config.sample_rate

        arr = np.frombuffer(audio_int16, dtype=np.int16).astype(np.float32) / 32768.0
        return arr, sample_rate


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_server([PiperSynthesizer()])
