#!/usr/bin/env python3
"""Qwen3-TTS PyTorch — specific backend for the generic OpenAI TTS server.

Keeps Qwen3-TTS model-specific logic (device picking, model loading, VoiceDesign
vs Base clone) and delegates HTTP / auth / codec / locking to the generic server
(openai_tts_server.py). Same pattern as piper_tts.py / supertonic_tts.py /
cutetts_tts.py / kokoro_tts.py.

Supports BOTH Qwen3-TTS model families, selected by env:

  * VoiceDesign (e.g. Qwen3-TTS-12Hz-1.7B-VoiceDesign) — the default.
    Synthesis via generate_voice_design(text, instruct, language).
    No reference audio needed; `instruct` defaults to empty (no style).

  * Base (e.g. Qwen3-TTS-12Hz-0.6B-Base) — voice CLONING.
    Synthesis via generate_voice_clone(...) with a clone prompt built from
    a reference audio (QWEN_TTS_REF_AUDIO). Only used when the loaded model
    reports tts_model_type == "base".

The model is loaded ONCE at startup and kept resident, so the first request
pays the load cost and every subsequent request reuses the warm model.

GPU detection is baked in (pick_device): picks the CUDA device with the most
FREE VRAM (never a hardcoded index — ROCm enumerates GPUs in an order that
differs from rocm-smi/llama), uses bf16, and falls back to CPU with clear
logging if no GPU has enough free memory. flash-attn is NOT used (it is
CUDA/bf16-only and not installed under ROCm).

Env:
    QWEN_TTS_MODEL     model checkpoint dir (default 1.7B-VoiceDesign)
    QWEN_TTS_REF_AUDIO ref wav for Base clone voice (default /app/ref.wav)
    QWEN_TTS_REF_TEXT   optional ref transcript for Base clone (else x-vector only)
    QWEN_TTS_LANG      default language: auto/en/ru/de (default auto)
    QWEN_TTS_INSTRUCT  default voice-design instruction (default empty)

Run:  python3 qwen_torch_tts.py
"""

import logging
import os

import numpy as np
import soundfile as sf

# Must import openai_tts_server BEFORE torch: its module import runs
# auto_restrict_to_egpu(), which sets CUDA_VISIBLE_DEVICES to the eGPU on this
# host. Setting it after `import torch` does not mask the second (iGPU) device
# — see README "MOSS-TTS-Local — eGPU device-index fix". Enabled via
# TTS_AUTO_EGPU=1 in tts-dev-pod.yaml. openai_tts_server imports torch lazily,
# so this import does not itself trigger CUDA init.
from openai_tts_server import TTSSynthesizer, run_server, pick_device

import torch

try:
    from qwen_tts import Qwen3TTSModel
except Exception as e:  # noqa: BLE001 - fail fast with a clear message at startup
    raise SystemExit(f"qwen_tts import failed (is the package installed?): {e}")

log = logging.getLogger("qwen-torch-tts")

# ── Config from env ────────────────────────────────────────────────────────
MODEL_DIR = os.environ.get("QWEN_TTS_MODEL", "/models/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
REF_AUDIO = os.environ.get("QWEN_TTS_REF_AUDIO", "/app/ref.wav")
REF_TEXT = os.environ.get("QWEN_TTS_REF_TEXT", "") or None
DEFAULT_LANG_CODE = os.environ.get("QWEN_TTS_LANG", "auto").lower()
DEFAULT_INSTRUCT = os.environ.get("QWEN_TTS_INSTRUCT", "")

# Map OpenAI-ish language codes to the strings the model expects.
# "auto" -> "Auto" (the model's own auto-detect). Unknown codes -> Auto.
LANG_MAP = {"auto": "Auto", "en": "English", "ru": "Russian", "de": "German"}
DEFAULT_LANGUAGE = LANG_MAP.get(DEFAULT_LANG_CODE, "Auto")


class VoiceRegistry:
    """Clone prompts for the Base model family. Built once at startup from a
    reference audio so we do not re-extract the speaker embedding per request.
    Only used when the loaded model is a Base (clone) model."""

    def __init__(self, model):
        self.model = model
        self.prompts: dict[str, list] = {}
        if not os.path.exists(REF_AUDIO):
            log.warning("REF_AUDIO %r not found; Base clone voice unavailable", REF_AUDIO)
            return
        try:
            data, sr = sf.read(REF_AUDIO, dtype="float32", always_2d=True)
            data = data.mean(axis=1) if data.ndim > 1 else data
        except Exception as e:  # noqa: BLE001
            log.warning("cannot read REF_AUDIO %r: %s", REF_AUDIO, e)
            return
        xvec_only = not bool(REF_TEXT)
        log.info("registering default clone voice from %s (x_vector_only=%s)", REF_AUDIO, xvec_only)
        try:
            self.prompts["default"] = self.model.create_voice_clone_prompt(
                ref_audio=(data, sr), ref_text=REF_TEXT, x_vector_only_mode=xvec_only
            )
        except Exception as e:  # noqa: BLE001
            log.warning("create_voice_clone_prompt failed: %s", e)

    def get(self, name: str):
        if name and name in self.prompts:
            return self.prompts[name]
        if name and name not in self.prompts:
            log.warning("unknown voice %r; falling back to 'default'", name)
        return self.prompts.get("default")


def _load_model():
    if not os.path.isdir(MODEL_DIR):
        raise SystemExit(f"model dir not found: {MODEL_DIR}")
    device, dtype = pick_device(min_free_gib=4.0)
    log.info("loading Qwen3-TTS model from %s (device=%s, dtype=%s)", MODEL_DIR, device, dtype)
    model = Qwen3TTSModel.from_pretrained(
        MODEL_DIR,
        device_map=device,
        dtype=dtype,
        # flash-attn is CUDA/bf16-only and not installed under ROCm; omit it
        # so transformers does not auto-enable it and crash on load.
    )
    # Detect which model family we loaded (voice_design vs base/clone).
    mtype = getattr(getattr(model, "model", None), "tts_model_type", None)
    log.info("model type: %s", mtype)
    return model, mtype


class QwenSynthesizer(TTSSynthesizer):
    name = "qwen-tts"
    lazy = True

    def __init__(self):
        super().__init__()
        self.voices = []
        log.info("Qwen3-TTS: model=%s lang=%s (lazy)", MODEL_DIR, DEFAULT_LANGUAGE)

    def load(self):
        global MODEL, MODEL_TYPE, VOICES
        if self.is_loaded():
            return
        MODEL, MODEL_TYPE = _load_model()
        VOICES = VoiceRegistry(MODEL) if MODEL_TYPE != "voice_design" else {}
        # Base (clone) models expose the registered clone voice(s); VoiceDesign
        # has no fixed voice list (style comes from `instruct`).
        self.voices = list(VOICES.prompts.keys()) if VOICES else []
        log.info("Qwen3-TTS: model=%s type=%s lang=%s voices=%s",
                 MODEL_DIR, MODEL_TYPE, DEFAULT_LANGUAGE, self.voices)
        super().load()

    def unload(self):
        global MODEL, MODEL_TYPE, VOICES
        if not self.is_loaded():
            return
        log.info("Qwen3-TTS: unloading model (freeing VRAM)")
        MODEL = None
        MODEL_TYPE = None
        VOICES = None
        self.voices = []
        torch.cuda.empty_cache()
        super().unload()

    def synthesize(self, text, voice="", lang="", instruct=""):
        language = LANG_MAP.get((lang or DEFAULT_LANG_CODE).lower(), "Auto")
        if MODEL_TYPE == "voice_design":
            # VoiceDesign: no reference audio; `instruct` is the voice-design instruction.
            wavs, sr = MODEL.generate_voice_design(
                text=text,
                language=language,
                instruct=instruct or DEFAULT_INSTRUCT,
                non_streaming_mode=True,
            )
        else:
            # Base (clone): needs a clone prompt from reference audio.
            prompt = VOICES.get(voice)
            if prompt is None:
                raise RuntimeError("Base model loaded but no clone voice available (set QWEN_TTS_REF_AUDIO)")
            wavs, sr = MODEL.generate_voice_clone(
                text=text,
                language=language,
                voice_clone_prompt=prompt,
                non_streaming_mode=True,
            )
        wav = wavs[0] if isinstance(wavs, list) else wavs
        return np.asarray(wav, dtype=np.float32), int(sr)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_server([QwenSynthesizer()])
