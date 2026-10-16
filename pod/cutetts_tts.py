#!/usr/bin/env python3
"""CuteTTS — specific backend for the generic OpenAI TTS server.

CuteTTS is a Qwen3-backbone TTS (~230M) with a diffusion head. It auto-detects
language from the text (no `lang` param). Model is loaded once at startup and
kept resident (warm).

Run:  python3 cutetts_tts.py
Env:  CUTETTS_MODEL_DIR  path to the CuteTTS model dir (default /models/CuteTTS)
      CUTETTS_DEVICE     torch device: auto|cpu|cuda (default auto)
      CUTETTS_REF_AUDIO  optional reference wav for voice_clone mode
"""

import os

import numpy as np
import torch

from openai_tts_server import TTSSynthesizer, run_server

MODEL_DIR = os.environ.get("CUTETTS_MODEL_DIR", "/models/CuteTTS")
DEVICE = os.environ.get("CUTETTS_DEVICE", "auto")
REF_AUDIO = os.environ.get("CUTETTS_REF_AUDIO", "")


class CuteTTSSynthesizer(TTSSynthesizer):
    name = "cutetts"
    voices = ["default"]

    def __init__(self):
        print(f"[cutetts] loading model from {MODEL_DIR} (device={DEVICE})...", flush=True)
        from cutetts import CuteTTS
        self.engine = CuteTTS.from_pretrained(MODEL_DIR, device=DEVICE)
        print(f"[cutetts] model loaded. sample_rate={self.engine.sample_rate}", flush=True)

    def synthesize(self, text, voice="", lang="", instruct=""):
        mode = "voice_clone" if REF_AUDIO else "tts"
        result = self.engine.generate(
            text,
            mode=mode,
            reference_audio=REF_AUDIO or None,
            show_progress=False,
        )
        wav = result.waveform.detach().cpu().numpy().squeeze()
        return np.asarray(wav, dtype=np.float32), result.sample_rate


if __name__ == "__main__":
    run_server([CuteTTSSynthesizer()])
