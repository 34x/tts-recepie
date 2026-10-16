#!/usr/bin/env python3
"""Breeze TTS 2 backend (BreezeBlue) — voice design + voice clone.

Architecture: Qwen3 backbone (1B) + depth decoder (100M) + T5Gemma2 text encoder
+ Mimi codec. Uses breeze_infer's FastBreezeStreamingRuntime for multi-step
streaming inference (text encoder → backbone → depth decoder → codec).

GPU notes:
  * flash_attention_2 is NOT installed under ROCm — resolve_attn_implementation()
    falls back to "sdpa" on CUDA, "eager" on CPU.
  * CUDA Graph "fast path" (--fast-all) won't work on ROCm — disabled.
  * VRAM ~7.7 GiB sdpa inference.

Voice modes:
  * voice_design — natural language instruction (no ref audio needed)
  * voice_clone   — reference audio + transcript (uses ref_edit_tata template)

Language: en, zh only.

Env:
    BREEZE_TTS_MODEL        model checkpoint dir    (default: /models/BreezeBlue/Breeze-TTS-2)
    BREEZE_TTS_DEVICE       force device: cpu/cuda/auto (default: auto)
    BREEZE_TTS_REF_AUDIO    reference audio for clone (default: /app/ref.wav)
    BREEZE_TTS_LANG         default language        (default: auto)
    TTS_AUTO_EGPU           1 = auto-detect eGPU    (default: 0)
    TTS_LAZY_TTL            lazy eviction TTL (s)   (default: 300)
"""

import logging
import os
import pathlib

import numpy as np
import soundfile as sf

# Configure logging before importing openai_tts_server, so the eGPU auto-detect
# logs (tts.egpu) emitted at import time are not dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from openai_tts_server import TTSSynthesizer, run_server, pick_device, resolve_attn_implementation

logger = logging.getLogger("tts.breeze")

# ── globals (loaded lazily) ────────────────────────────────────────────────

_model = None
_tokenizer = None
_runtime = None
_voice_registry: dict[str, dict] = {}


def _get_model():
    global _model, _tokenizer, _runtime
    if _model is None:
        from breeze_infer.runtime import (
            load_runtime,
            update_generation_config_for_breeze,
        )
        from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig

        MODEL_DIR = os.environ.get(
            "BREEZE_TTS_MODEL",
            "/models/BreezeBlue/Breeze-TTS-2",
        )
        FORCE_DEVICE = os.environ.get("BREEZE_TTS_DEVICE", "auto").lower()
        if not os.path.isdir(MODEL_DIR):
            raise SystemExit(f"model dir not found (bind-mount it?): {MODEL_DIR}")

        device, dtype = pick_device(force_device=FORCE_DEVICE, min_free_gib=4.0)
        attn = resolve_attn_implementation(device, dtype)
        logger.info("loading Breeze TTS 2 from %s (device=%s, dtype=%s, attn=%s)",
                     MODEL_DIR, device, dtype, attn)
        tokenizer, model, audio_tokenizer = load_runtime(
            # load_runtime() does `ckpt_dir / "audio_tokenizer"` (Path division),
            # so it requires a pathlib.Path, not the str os.environ returns.
            pathlib.Path(MODEL_DIR),
            device=device,
            attn_implementation=attn,
        )
        update_generation_config_for_breeze(model)

        config = FastStreamingConfig(
            max_new_tokens=1500,
            max_seq_len=2048,
            fast_all=None,  # disabled — CUDA Graphs don't work on ROCm
            fast_text_encoder=False,
            fast_backbone_prefill=False,
            fast_backbone_decode=False,
            fast_depth_decoder=False,
            fast_codec=False,
            repetition_penalty=1.1,
        )
        _runtime = FastBreezeStreamingRuntime(
            model, audio_tokenizer, config, tokenizer=tokenizer
        )
        _model = model
        _tokenizer = tokenizer
        logger.info("Breeze TTS 2 loaded (sample_rate=%dHz)", _runtime.sample_rate)
    return _model, _tokenizer, _runtime


class BreezeSynthesizer(TTSSynthesizer):
    """Breeze TTS 2 synthesizer — voice design + voice clone."""

    name = "breeze-tts-2"
    lazy = True  # lazy-load to save VRAM

    def __init__(self):
        super().__init__()
        self._ref_audio_path = os.environ.get(
            "BREEZE_TTS_REF_AUDIO", "/app/ref.wav"
        )
        self._default_lang = os.environ.get("BREEZE_TTS_LANG", "auto")
        self.voices: list[str] = []
        if not self.lazy:
            self.load()

    def load(self):
        global _voice_registry
        if self._loaded:
            return
        _get_model()

        # Register default voice (from reference audio)
        if os.path.isfile(self._ref_audio_path):
            self.voices = ["default"]
            _voice_registry["default"] = {
                "audio_path": self._ref_audio_path,
            }
            logger.info(
                "registered voice 'default' from %s", self._ref_audio_path
            )

        self._loaded = True

    def unload(self):
        global _model, _tokenizer, _runtime, _voice_registry
        if not self._loaded:
            return
        logger.info("unloading Breeze TTS 2")
        _model = None
        _tokenizer = None
        _runtime = None
        _voice_registry = {}
        self._loaded = False

    def synthesize(
        self, text: str, voice: str = "", lang: str = "", instruct: str = ""
    ) -> tuple[np.ndarray, int]:
        """Synthesize speech.

        Args:
            text: text to synthesize
            voice: "default" (uses ref audio) or "[S0]"–"[S9]" (speaker tokens)
            lang: "en", "zh", or "auto"
            instruct: voice design instruction (natural language)
        """
        from breeze_infer.templates import get_template, prepare_inputs
        from breeze_infer.runtime import set_all_seeds

        model, tokenizer, runtime = _get_model()

        # ── resolve voice → speaker token ─────────────────────────────────
        if voice == "default":
            speaker = "[S0]"
        elif voice.startswith("[S") and voice.endswith("]"):
            speaker = voice
        else:
            speaker = "[S0]"

        # ── resolve language ──────────────────────────────────────────────
        lang_map = {
            "en": "English",
            "eng": "English",
            "english": "English",
            "zh": "Chinese",
            "zh-cn": "Chinese",
            "chinese": "Chinese",
            "auto": "auto",
        }
        lang_full = lang_map.get(lang.lower(), lang) if lang else self._default_lang

        # ── determine mode ────────────────────────────────────────────────
        is_clone = voice == "default" or (
            voice.startswith("[S") and voice.endswith("]")
            and voice in _voice_registry
        )

        if is_clone:
            voice_info = _voice_registry.get(voice, {})
            ref_audio_path = voice_info.get("audio_path", self._ref_audio_path)

            # Breeze templates expect a file path — write ref to temp if needed
            if not os.path.isfile(ref_audio_path):
                raise ValueError(
                    f"Reference audio not found: {ref_audio_path} "
                    "(set BREEZE_TTS_REF_AUDIO or mount ref.wav)"
                )

            request = {
                "id": "synth-clone",
                "text": text,
                "instruction": instruct or "Speak clearly and naturally.",
                "speaker": speaker,
                "ref_audio_path": ref_audio_path,
                "ref_text": "",  # not used for simple clone
            }
            template = get_template("ref_edit_tata")
        else:
            # Voice design mode — no reference audio
            request = {
                "id": "synth-design",
                "text": text,
                "instruction": instruct or "Speak clearly and naturally.",
                "speaker": speaker,
            }
            template = get_template("tts_instruction")

        # ── prepare inputs ────────────────────────────────────────────────
        inputs = prepare_inputs(
            tokenizer,
            runtime.audio_tokenizer,
            model,
            [request],
            template,
            guidance_scale=1.0,
            guidance_scale_ref=None,
            guidance_scale_ins=None,
        )

        # ── iterate audio chunks ──────────────────────────────────────────
        chunks: list[np.ndarray] = []
        for chunk in runtime.iter_audio_chunks(
            inputs, request_id="synth", seed=42
        ):
            # chunk.audio is float32 PCM (from FastStreamingChunk)
            chunks.append(chunk.audio)

        if not chunks:
            raise RuntimeError("Breeze TTS 2 generated no audio chunks")

        # Concatenate all chunks
        wav = np.concatenate(chunks, axis=0)
        sr = runtime.sample_rate
        return wav, sr


# ── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_server([BreezeSynthesizer()])
