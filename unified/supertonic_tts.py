#!/usr/bin/env python3
"""Supertonic 3 TTS — specific backend for the generic OpenAI TTS server.

Supertonic is non-autoregressive ONNX and CPU-first. The ONNX model is
loaded lazily on the first ``synthesize()`` call so that server startup
(and the /v1/models, /v1/audio/voices endpoints) are immediate.

Run:  python3 supertonic_tts.py
Env:  SUPERTONIC_VOICE        default voice (default M3)
      SUPERTONIC_LANG         default language (default en)
      SUPERTONIC_STEPS        synthesis diffusion steps (default 50)
      SUPERTONIC_SPEED        speech rate multiplier (default 1.0)
"""

import os

import numpy as np

from openai_tts_server import TTSSynthesizer, run_server

VOICE = os.environ.get("SUPERTONIC_VOICE", "M3")
LANG = os.environ.get("SUPERTONIC_LANG", "en")
STEPS = int(os.environ.get("SUPERTONIC_STEPS", "10"))
SPEED = float(os.environ.get("SUPERTONIC_SPEED", "1.0"))
# Parent cache dir the ONNX model is written to (supertonic appends
# "/supertonic3" and rmtrees/renames onto it, see supertonic.loader). Fallback
# default for SUPERTONIC_CACHE_DIR: start.py bind-mounts this from the host
# ($TTS_MODELS_ROOT) so models persist across container rebuilds. An explicit
# SUPERTONIC_CACHE_DIR set by the operator is respected (see _ensure_engine).
# Must be the *parent* dir, not the leaf.
CONTAINER_CACHE_DIR = "/root/.cache"


class SupertonicSynthesizer(TTSSynthesizer):
    name = "supertonic-3"
    voices = ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"]

    def __init__(self):
        self._engine = None

    def _ensure_engine(self):
        """Load the Supertonic engine on first use (downloads if needed).

        Supertonic auto-downloads its ONNX model under
        ``<CONTAINER_CACHE_DIR>/supertonic3``. ``SUPERTONIC_CACHE_DIR`` overrides
        the *parent* cache dir (supertonic.config.get_model_cache_dir, consulted
        at call time) and start.py bind-mounts CONTAINER_CACHE_DIR from the host
        ($TTS_MODELS_ROOT), so pointing it there makes the model persist across
        container rebuilds.
        """
        if self._engine is not None:
            return
        if "SUPERTONIC_CACHE_DIR" not in os.environ:
            os.environ["SUPERTONIC_CACHE_DIR"] = CONTAINER_CACHE_DIR + "/supertonic3"

        print("[supertonic] loading model (auto_download)...", flush=True)
        from supertonic import TTS
        self._engine = TTS(auto_download=True, model_dir=os.environ["SUPERTONIC_CACHE_DIR"])
        print(f"[supertonic] model loaded. voice={VOICE} lang={LANG}", flush=True)

    def synthesize(self, text, voice="", lang="", instruct=""):
        self._ensure_engine()
        engine = self._engine
        style = engine.get_voice_style(voice_name=voice or VOICE)
        wav, _ = engine.synthesize(
            text,
            voice_style=style,
            lang=lang or LANG,
            total_steps=STEPS,
            speed=SPEED,
        )
        arr = np.asarray(wav, dtype=np.float32).squeeze()
        return arr, engine.sample_rate


if __name__ == "__main__":
    run_server([SupertonicSynthesizer()])
