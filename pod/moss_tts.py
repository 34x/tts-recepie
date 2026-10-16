#!/usr/bin/env python3
"""MOSS-TTS-Nano — specific backend for the generic OpenAI TTS server.

Keeps MOSS-TTS-Nano model-specific logic (device picking, model loading,
attention resolution, inference) and delegates HTTP / auth / codec / locking
to the generic server (openai_tts_server.py). Same pattern as piper_tts.py /
supertonic_tts.py / cutetts_tts.py / kokoro_tts.py / qwen_torch_tts.py.

MOSS-TTS-Nano is a 0.1B autoregressive TTS (Apache-2.0, 20 languages incl
ru/de, 48 kHz stereo). It needs a SEPARATE audio codec model
(MOSS-Audio-Tokenizer-Nano) loaded via AutoModel.from_pretrained.

The model is lazy-loaded on first request and evicted after TTS_LAZY_TTL
seconds of inactivity (see openai_tts_server.py), so it does not hold VRAM
at startup.

Attention resolution follows the model's own recommended logic:
  * flash_attention_2  — only when device==cuda AND flash_attn is installed
                         AND dtype is fp16/bf16 AND GPU capability >= 8.
  * sdpa               — any other CUDA device (ROCm exposes as cuda but
                         flash_attn is not installed -> sdpa).
  * eager              — CPU.
The model's config.json bakes in flash_attention_2, so we override BOTH
attn_implementation and local_transformer_attn_implementation via a loaded
config before from_pretrained.

Env:
    MOSS_TTS_MODEL     model checkpoint dir (default MOSS-TTS-Nano)
    MOSS_TTS_CODEC     audio codec dir (default MOSS-Audio-Tokenizer-Nano)
    MOSS_TTS_DEVICE    force device: cpu / cuda / auto (default auto)
    MOSS_TTS_REF_AUDIO optional reference audio for voice_clone mode
    MOSS_TTS_MODE      inference mode: continuation / voice_clone (default continuation)
    MOSS_TTS_MAX_FRAMES max_new_frames (default 300; ~12.5 frames/s)

Run:  python3 moss_tts.py
"""

import logging
import os

import numpy as np

# Configure logging before importing openai_tts_server, so the eGPU auto-detect
# logs (tts.egpu) emitted at import time are not dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Must import openai_tts_server BEFORE torch/transformers: its module import
# runs auto_restrict_to_egpu(), which sets CUDA_VISIBLE_DEVICES to the eGPU on
# this host. Setting that env var after `import torch` does not reliably mask
# the second (iGPU) device — see README "MOSS-TTS-Local — eGPU device-index
# fix". openai_tts_server imports torch lazily, so this import does not itself
# trigger CUDA init.
from openai_tts_server import TTSSynthesizer, run_server, pick_device, resolve_attn_implementation

import torch
from transformers import AutoConfig, AutoModel

# ── torchaudio.save shim ──────────────────────────────────────────────────
# The model's inference() writes audio to disk via torchaudio.save(), which
# requires torchcodec. torchcodec's native lib fails to load against the ROCm
# torch build (ABI mismatch), so we shim torchaudio.save to use soundfile
# (already installed) instead. This avoids the torchcodec dependency entirely.
import soundfile as sf  # noqa: E402
import torchaudio  # noqa: E402


def _torchaudio_save(path, wav, sr):
    arr = wav.detach().cpu().numpy()
    if arr.ndim == 2:
        arr = arr.T  # [channels, samples] -> [samples, channels]
    sf.write(str(path), arr, int(sr))


torchaudio.save = _torchaudio_save

log = logging.getLogger("moss-tts")

# ── Config from env ────────────────────────────────────────────────────────
MODEL_DIR = os.environ.get(
    "MOSS_TTS_MODEL", "/models/OpenMOSS-Team/MOSS-TTS-Nano"
)
CODEC_DIR = os.environ.get(
    "MOSS_TTS_CODEC", "/models/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano"
)
FORCE_DEVICE = os.environ.get("MOSS_TTS_DEVICE", "auto").lower()
REF_AUDIO = os.environ.get("MOSS_TTS_REF_AUDIO", "") or None
MODE = os.environ.get("MOSS_TTS_MODE", "continuation").lower()
MAX_FRAMES = int(os.environ.get("MOSS_TTS_MAX_FRAMES", "300"))

# Recommended hyperparams from the model README.
AUDIO_TEMPERATURE = float(os.environ.get("MOSS_TTS_AUDIO_TEMP", "1.0"))
AUDIO_TOP_P = float(os.environ.get("MOSS_TTS_AUDIO_TOP_P", "0.95"))
AUDIO_TOP_K = int(os.environ.get("MOSS_TTS_AUDIO_TOP_K", "50"))
AUDIO_REP_PENALTY = float(os.environ.get("MOSS_TTS_AUDIO_REP_PENALTY", "1.1"))

def _load_model():
    if not os.path.isdir(MODEL_DIR):
        raise SystemExit(f"model dir not found: {MODEL_DIR}")
    if not os.path.isdir(CODEC_DIR):
        raise SystemExit(f"codec dir not found: {CODEC_DIR}")

    device, dtype = pick_device(force_device=FORCE_DEVICE, min_free_gib=4.0)
    attn = resolve_attn_implementation(device, dtype)
    log.info("loading MOSS-TTS-Nano from %s (device=%s, dtype=%s, attn=%s)",
             MODEL_DIR, device, dtype, attn)

    # The model's config.json bakes in flash_attention_2 for BOTH the global
    # and local transformer. Override both via a loaded config so we never
    # request flash_attn under ROCm (where it is not installed).
    config = AutoConfig.from_pretrained(MODEL_DIR, trust_remote_code=True)
    config.attn_implementation = attn
    config.local_transformer_attn_implementation = attn

    model = AutoModel.from_pretrained(
        MODEL_DIR,
        config=config,
        trust_remote_code=True,
        torch_dtype=dtype,
    )
    model.to(device)
    model.eval()
    log.info("MOSS-TTS-Nano loaded (device=%s, dtype=%s, attn=%s)", device, dtype, attn)
    return model, device


class MossSynthesizer(TTSSynthesizer):
    name = "moss-tts"
    lazy = True

    def __init__(self):
        super().__init__()
        self.voices = []
        log.info("MOSS-TTS: model=%s codec=%s mode=%s max_frames=%d (lazy)",
                 MODEL_DIR, CODEC_DIR, MODE, MAX_FRAMES)

    def load(self):
        global MODEL, DEVICE
        if self.is_loaded():
            return
        MODEL, DEVICE = _load_model()
        super().load()

    def unload(self):
        global MODEL, DEVICE
        if not self.is_loaded():
            return
        log.info("MOSS-TTS: unloading model (freeing VRAM)")
        MODEL = None
        DEVICE = None
        import torch
        torch.cuda.empty_cache()
        super().unload()

    def synthesize(self, text, voice="", lang="", instruct=""):
        # `inference()` writes to disk; we use the returned waveform tensor and
        # encode in-memory instead of the file path.
        result = MODEL.inference(
            text=text,
            output_audio_path="/tmp/moss_out.wav",
            mode=MODE,
            prompt_audio_path=REF_AUDIO,
            audio_tokenizer_pretrained_name_or_path=CODEC_DIR,
            device=DEVICE,
            max_new_frames=MAX_FRAMES,
            audio_temperature=AUDIO_TEMPERATURE,
            audio_top_p=AUDIO_TOP_P,
            audio_top_k=AUDIO_TOP_K,
            audio_repetition_penalty=AUDIO_REP_PENALTY,
        )
        waveform = result["waveform"]
        sr = int(result["sample_rate"])
        # waveform is (channels, samples) float32; backbone expects mono float32.
        wav = waveform.detach().cpu().numpy()
        if wav.ndim == 2 and wav.shape[0] > 1:
            wav = wav.mean(axis=0)
        else:
            wav = wav.reshape(-1)
        return np.asarray(wav, dtype=np.float32), sr


if __name__ == "__main__":
    run_server([MossSynthesizer()])
