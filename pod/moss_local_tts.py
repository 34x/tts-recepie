#!/usr/bin/env python3
"""MOSS-TTS-Local-Transformer — specific backend for the generic OpenAI TTS server.

Keeps MOSS-TTS-Local model-specific logic (device picking, model loading,
attention resolution, generation) and delegates HTTP / auth / codec / locking
to the generic server (openai_tts_server.py). Same pattern as moss_tts.py /
piper_tts.py / supertonic_tts.py / cutetts_tts.py / kokoro_tts.py.

MOSS-TTS-Local-Transformer is a 1.7B autoregressive TTS (Apache-2.0, 20
languages incl ru/de, 24 kHz). It uses the FULL MOSS-Audio-Tokenizer codec
(not the Nano one) and the transformers 5.x remote code path (AutoProcessor +
model.generate with DelayGenerationConfig). It is GPU-oriented (bf16).

The model is loaded ONCE at startup and kept resident, so the first request
pays the load cost and every subsequent request reuses the warm model.

Attention resolution follows the model's own recommended logic:
  * flash_attention_2  — only when device==cuda AND flash_attn is installed
                         AND dtype is fp16/bf16 AND GPU capability >= 8.
  * sdpa               — any other CUDA device (ROCm exposes as cuda but
                         flash_attn is not installed -> sdpa).
  * eager              — CPU.

Env:
    MOSS_LOCAL_MODEL     model checkpoint dir (default MOSS-TTS-Local-Transformer)
    MOSS_LOCAL_CODEC     audio codec dir (default MOSS-Audio-Tokenizer)
    MOSS_LOCAL_DEVICE    force device: cpu / cuda / auto (default auto)
    MOSS_LOCAL_MAX_TOKENS max_new_tokens (default 4096; ~12.5 tokens/s)
    MOSS_LOCAL_N_VQ      RVQ inference depth (default 32)

Run:  python3 moss_local_tts.py
"""

import json
import logging
import os
import subprocess
import sys

# ── eGPU restriction MUST run before `import torch` ───────────────────────
# torch reads CUDA_VISIBLE_DEVICES at its first CUDA init. If we set it after
# `import torch`, the already-imported torch still sees BOTH GPUs and the
# backend picks cuda:1 (the eGPU at index 1) -> segfault. So we detect the
# eGPU (largest total VRAM) in a fresh subprocess and set the env var here,
# before torch is imported in this process.
if os.environ.get("TTS_AUTO_EGPU", "0") == "1" and "CUDA_VISIBLE_DEVICES" not in os.environ:
    _probe = r"""
import json, sys, torch
devs = []
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    devs.append({"index": i, "name": p.name, "total": p.total_memory})
print(json.dumps(devs))
"""
    try:
        _out = subprocess.run(
            [sys.executable, "-c", _probe], capture_output=True, timeout=60
        )
        _devs = json.loads(_out.stdout.decode().strip())
        if isinstance(_devs, list) and _devs:
            _best = max(_devs, key=lambda d: d["total"])
            os.environ["CUDA_VISIBLE_DEVICES"] = str(_best["index"])
            logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
            logging.getLogger("moss-local-tts").info(
                "eGPU pre-import: selected cuda:%s (%s) -> CUDA_VISIBLE_DEVICES=%s",
                _best["index"], _best["name"], _best["index"])
    except Exception:
        pass

import numpy as np
import torch
from transformers import AutoModel, AutoProcessor

# Configure logging before importing openai_tts_server, so the eGPU auto-detect
# logs (tts.egpu) emitted at import time are not dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from openai_tts_server import TTSSynthesizer, run_server, pick_device, resolve_attn_implementation

# Disable the broken cuDNN SDPA backend (per the model README).
torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_flash_sdp(True)
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_math_sdp(True)

log = logging.getLogger("moss-local-tts")

# ── Config from env ────────────────────────────────────────────────────────
MODEL_DIR = os.environ.get(
    "MOSS_LOCAL_MODEL", "/models/OpenMOSS-Team/MOSS-TTS-Local-Transformer"
)
CODEC_DIR = os.environ.get(
    "MOSS_LOCAL_CODEC", "/models/OpenMOSS-Team/MOSS-Audio-Tokenizer"
)
FORCE_DEVICE = os.environ.get("MOSS_LOCAL_DEVICE", "auto").lower()
MAX_TOKENS = int(os.environ.get("MOSS_LOCAL_MAX_TOKENS", "4096"))
N_VQ = int(os.environ.get("MOSS_LOCAL_N_VQ", "32"))

# Recommended hyperparams from the model README (MOSS-TTSLocal-1.7B).
AUDIO_TEMPERATURE = float(os.environ.get("MOSS_LOCAL_AUDIO_TEMP", "1.0"))
AUDIO_TOP_P = float(os.environ.get("MOSS_LOCAL_AUDIO_TOP_P", "0.95"))
AUDIO_TOP_K = int(os.environ.get("MOSS_LOCAL_AUDIO_TOP_K", "50"))
AUDIO_REP_PENALTY = float(os.environ.get("MOSS_LOCAL_AUDIO_REP_PENALTY", "1.1"))


def _load_model():
    if not os.path.isdir(MODEL_DIR):
        raise SystemExit(f"model dir not found: {MODEL_DIR}")
    if not os.path.isdir(CODEC_DIR):
        raise SystemExit(f"codec dir not found: {CODEC_DIR}")

    device, dtype = pick_device(force_device=FORCE_DEVICE, min_free_gib=6.0)
    attn = resolve_attn_implementation(device, dtype)
    log.info("loading MOSS-TTS-Local from %s (device=%s, dtype=%s, attn=%s)",
             MODEL_DIR, device, dtype, attn)

    processor = AutoProcessor.from_pretrained(
        MODEL_DIR, trust_remote_code=True, codec_path=CODEC_DIR
    )
    processor.audio_tokenizer = processor.audio_tokenizer.to(device)

    model = AutoModel.from_pretrained(
        MODEL_DIR,
        trust_remote_code=True,
        attn_implementation=attn,
        torch_dtype=dtype,
    ).to(device)
    model.eval()
    log.info("MOSS-TTS-Local loaded (device=%s, dtype=%s, attn=%s)", device, dtype, attn)
    return model, processor, device


class MossLocalSynthesizer(TTSSynthesizer):
    name = "moss-local-tts"
    lazy = True

    def __init__(self):
        super().__init__()
        self.voices = []
        log.info("MOSS-TTS-Local: model=%s codec=%s max_tokens=%d n_vq=%d (lazy)",
                 MODEL_DIR, CODEC_DIR, MAX_TOKENS, N_VQ)

    def load(self):
        global MODEL, PROCESSOR, DEVICE
        if self.is_loaded():
            return
        MODEL, PROCESSOR, DEVICE = _load_model()
        super().load()

    def unload(self):
        global MODEL, PROCESSOR, DEVICE
        if not self.is_loaded():
            return
        log.info("MOSS-TTS-Local: unloading model (freeing VRAM)")
        MODEL = None
        PROCESSOR = None
        DEVICE = None
        torch.cuda.empty_cache()
        super().unload()

    def synthesize(self, text, voice="", lang="", instruct=""):
        log.info("synthesize: start (text=%r, voice=%r, lang=%r, instruct=%r)",
                 text[:40], voice, lang, instruct[:40])

        # Instructions are NOT supported: MOSS-TTS-Local ignores the
        # `- Instruction:` field (verified empirically — no audible difference).
        # Keep the field empty.
        log.info("synthesize: building user message")
        conversations = [[PROCESSOR.build_user_message(text=text)]]
        log.info("synthesize: processing batch (mode=generation)")
        batch = PROCESSOR(conversations, mode="generation")
        input_ids = batch["input_ids"].to(DEVICE)
        attention_mask = batch["attention_mask"].to(DEVICE)
        log.info("synthesize: input_ids shape=%s on %s", tuple(input_ids.shape), DEVICE)

        log.info("synthesize: entering MODEL.generate (max_new_tokens=%d)", MAX_TOKENS)
        with torch.no_grad():
            # Minimal call exactly as the README quick-start shows. Passing
            # n_vq_for_inference/audio_* triggers _build_generation_config ->
            # MossTTSDelayConfig -> cache setup path that segfaults on ROCm.
            outputs = MODEL.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=MAX_TOKENS,
            )
        log.info("synthesize: MODEL.generate returned")

        log.info("synthesize: decoding outputs")
        for message in PROCESSOR.decode(outputs):
            audio = message.audio_codes_list[0]
            sr = int(PROCESSOR.model_config.sampling_rate)
            # audio is (samples,) float32; backbone expects mono float32.
            wav = audio.detach().cpu().numpy().reshape(-1)
            log.info("synthesize: done (sr=%d, samples=%d)", sr, wav.shape[0])
            return np.asarray(wav, dtype=np.float32), sr

        raise RuntimeError("MOSS-TTS-Local produced no audio output")


if __name__ == "__main__":
    run_server([MossLocalSynthesizer()])
