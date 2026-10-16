#!/usr/bin/env python3
"""Kokoro-82M TTS backend on the generic openai_tts_server backbone.

Specific file only — all HTTP/mp3/wav/auth/lock scaffolding lives in
openai_tts_server.py. This file defines the synthesizer and runs the server.

Usage:
    python3 kokoro_tts.py

Environment:
    KOKORO_MODEL_PATH   Path to kokoro ONNX model (default: /models/kokoro/kokoro-v1.0.int8.onnx)
    KOKORO_VOICES_PATH  Path to voices.bin (default: /models/kokoro/voices-v1.0.bin)
    KOKORO_DEFAULT_VOICE  Default voice (default: af_sarah)
    HOST / PORT         Bind host/port (defaults 0.0.0.0 / 8095)
"""

import logging
import os
import time

import numpy as np

from openai_tts_server import TTSSynthesizer, run_server

log = logging.getLogger("kokoro-tts")

MODEL_PATH = os.environ.get(
    "KOKORO_MODEL_PATH", "/models/kokoro/kokoro-v1.0.int8.onnx"
)
VOICES_PATH = os.environ.get(
    "KOKORO_VOICES_PATH", "/models/kokoro/voices-v1.0.bin"
)
DEFAULT_VOICE = os.environ.get("KOKORO_DEFAULT_VOICE", "af_sarah")

# Kokoro lang codes (subset of the 9 supported). Map generic lang → kokoro.
LANG_MAP = {
    "en": "en-us",
    "en-us": "en-us",
    "en-gb": "en-gb",
    "ja": "ja",
    "zh": "zh",
    "es": "es",
    "fr": "fr",
    "hi": "hi",
    "it": "it",
    "pt": "pt-br",
    "pt-br": "pt-br",
}


class KokoroSynthesizer(TTSSynthesizer):
    name = "kokoro"

    def __init__(self):
        from kokoro_onnx import Kokoro

        log.info(f"Loading Kokoro model: {MODEL_PATH}")
        t0 = time.time()
        self.engine = Kokoro(model_path=MODEL_PATH, voices_path=VOICES_PATH)
        self._voices = self.engine.get_voices()
        log.info(f"Kokoro loaded in {time.time() - t0:.1f}s ({len(self._voices)} voices)")

    @property
    def voices(self):
        return self._voices

    def synthesize(self, text, voice, lang, instruct):
        voice = voice or DEFAULT_VOICE
        if voice not in self._voices:
            log.warning(f"Unknown voice '{voice}', falling back to {DEFAULT_VOICE}")
            voice = DEFAULT_VOICE
        kokoro_lang = LANG_MAP.get((lang or "en").lower(), "en-us")
        audio, sr = self.engine.create(text=text, voice=voice, lang=kokoro_lang)
        return np.asarray(audio, dtype=np.float32).squeeze(), sr


if __name__ == "__main__":
    run_server([KokoroSynthesizer()])
