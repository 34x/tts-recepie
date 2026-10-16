# TTS Backend Evaluation & Benchmarks

This document contains the scouting and evaluation data for various TTS backends. It is intended for development reference and performance tracking.

## Downloading models (hfdownloader)

`hfdownloader` is the tool for pulling HF repos to the local model store. It
creates the nested `user/repo` path automatically under the root you give it.

```bash
hfdownloader download <user/repo> --local-dir ${MODELS_DIR}
```

- `--local-dir` is the **root** (e.g. your `${MODELS_DIR}`).
  hfdownloader creates the nested `user/repo/` structure under it.
- Resumable — if a large download times out, re-run the same command to resume.
- Some models need a **second, separate codec/tokenizer repo** (e.g. MOSS-TTS
  needs `OpenMOSS-Team/MOSS-Audio-Tokenizer`). Download both.

## Quick comparison

Ordered by **CPU/GPU capability** — CPU-capable models first (evaluable without
GPU), then GPU-only. For GPU models, evaluation focuses on **speed + VRAM
(resource limits)**; for CPU models, on speed + quality.

| Model | CPU/GPU | Params | **Measured** (105 words) | Languages | Cloning | License | Notes |
|-------|---------|--------|-------------------------|----------|---------|---------|-------|
| **Piper** ✅ | CPU | — | **0.93s (RTF ~0.03)**, 239K | en/ru/de | No | MIT | **Fastest baseline**, CPU-native ONNX |
| **Sopro v2 Turbo** | CPU | 120M | — | en (weak mixed-lang) | Yes | Apache-2.0 | Claims: streams on CPU, ~300 ms TTFA, 0.24 RTF (M3) — unverified |
| **Kokoro** | CPU | 82M | — | 9 langs (no ru/de) | No (fixed voices) | Apache-2.0 | to try |
| **Kokoro-82M** ✅ | CPU | 82M | **40.5s (RTF ~1.29)**, 252K | 9 langs (no ru/de) | No (fixed voices) | Apache-2.0 | **Measured**: RTF ~1.29 on CPU (slower than realtime); ONNX sliding-window inference is the bottleneck |
| **Audio8 0.1B** ✅ | CPU (ONNX) | ~170M | **72.4s (RTF ~2.1)**, 3.0M WAV | multilingual (experimental) | Yes | audio8-community-license-v1.0 | **Measured**: RTF ~2.1 on CPU; returns **WAV not MP3** (3.0M for 105 words) |
| **CuteTTS** | Both | ~230M | **73.8s (RTF ~2.4)**, 244K | en/zh/fr/de/es | Yes | Apache-2.0 | **79× slower than Piper** on CPU; claims ~9x realtime on GPU (unverified here) |
| **Supertonic** ✅ | Both | — | **6.2s (RTF ~0.18)**, 280K | en/ru/de | Yes | MIT | 6.7× slower than Piper on CPU; GPU-capable |
| **MeloTTS** | CPU | ~? | — | en/zh/es/fr/ja/ko | No | MIT | Fast CPU; lightweight; no cloning |
| **MeloTTS-English** | CPU | ~? | — | en | No | MIT | to try |
| **StyleTTS 2** | CPU | ~? | — | en | Yes | MIT | High-quality; CPU-capable but slow |
| **Parler-TTS** | CPU | ~? | — | en/fr/de/es/it | No | Apache-2.0 | Controllable (style/emotion); no cloning |
| **Chatterbox-Nano** | CPU | ~? | — | en | Yes | Apache-2.0 | Lightweight variant of Chatterbox |
| **MOSS-TTS-Nano** | CPU | ~? | — | ? | Yes | Apache-2.0 | Lightweight MOSS variant |
| **XTTS v2** | CPU | ~? | — | 17 langs | Yes | **Coqui-NC** ⚠️ | Coqui non-commercial; CPU-capable but slow |
| **F5-TTS** | CPU | ~? | — | en/zh | Yes | MIT | Diffusion TTS; CPU-capable but slow |
| **Bark** | CPU | ~? | — | 100+ | No | MIT | Suno; CPU-capable but very slow |
| **Kyutai Pocket TTS** | CPU | ~? | — | en/fr | No | Apache-2.0 | Small, fast, on-device |
| **Qwen3-TTS** | GPU | ~? | — | en/zh | Yes | Apache-2.0 | to try |
| **Qwen3-TTS 0.6B** ✅ | GPU | 0.6B | **45.4s (RTF ~1.38)**, 264K | en/zh | Yes (Base) | Apache-2.0 | **Measured**: RTF ~1.38 on GPU (slower than realtime); docs claim 0.288 — gap unclosed |
| **Qwen3-TTS 1.7B** ✅ | GPU | 1.7B | **62.0s (RTF ~1.76)**, 281K | en/zh | Yes (VoiceDesign) | Apache-2.0 | **Measured**: RTF ~1.76 on GPU; the quality reference generator |
| **Audio8 0.6B** | GPU | 601M | — | 11 langs (incl de; no ru) | Yes | Apache-2.0 | ~1 GiB after loading; DualAR; ONNX INT4 variant |
| **MOSS-TTS-Local** ✅ | GPU | 1.7B | **60.8s (RTF ~1.83)**, 266K | **20 langs (incl ru/de)** | Yes | Apache-2.0 | **Measured**: RTF ~1.83 on GPU (slower than realtime); quality/clone tier |
| **Breeze TTS 2** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **Fish Audio S2 Pro** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **Fish Speech** | GPU | ~? | — | ? | Yes | Apache-2.0 | to try |
| **Fish Speech V1.5** | GPU | ~? | — | ? | Yes | Apache-2.0 | to try |
| **CosyVoice2-0.5B** | GPU | 0.5B | — | en/zh | Yes | Apache-2.0 | Alibaba; streaming |
| **IndexTTS2** | GPU | ~? | — | en/zh | Yes | Apache-2.0 | to try |
| **IndexTTS-2** | GPU | ~? | — | en/zh | Yes | Apache-2.0 | to try |
| **Chatterbox** | GPU | ~? | — | en | Yes | Apache-2.0 | — |
| **Chatterbox-Turbo** | GPU | ~? | — | en | Yes | Apache-2.0 | — |
| **MOSS-TTS** | GPU | 8B | — | 20 langs (incl ru/de) | Yes | Apache-2.0 | Flagship (heavier than Local) |
| **MOSS-TTS-Local-Transformer** | GPU | 1.7B | — | 20 langs (incl ru/de) | Yes | Apache-2.0 | = MOSS-TTS-Local |
| **Dia** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **Sesame CSM** | GPU | ~? | — | en | Yes | Apache-2.0 | Conversational |
| **Orpheus** | GPU | ~? | — | en | Yes | Apache-2.0 | — |
| **Spark-TTS** | GPU | ~? | — | en/zh | Yes | Apache-2.0 | — |
| **VibeVoice** | GPU | ~? | — | ? | Yes | Apache-2.0 | to try |
| **VibeVoice-1.5B** | GPU | 1.5B | — | ? | Yes | Apache-2.0 | to try |
| **VoxCPM** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **VoxCPM 1.5** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **VoxCPM 2** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **MOSS-TTS-Realtime** | GPU | 1.7B | — | 20 langs (incl ru/de) | Yes | Apache-2.0 | 180 ms TTFB; low-latency |
| **MOSS-SoundEffect** | GPU | ~? | — | — | No | Apache-2.0 | Sound effects, not speech |
| **MOSS-VoiceGenerator** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **MOSS-TTSD** | GPU | ~? | — | ? | Yes | Apache-2.0 | — |
| **Hume's TADA** | GPU | ~? | — | ? | Yes | Apache-2.0 | Hume AI; to try |
| **Voxtral TTS** | GPU | ~? | — | ? | Yes | Apache-2.0 | to try |

### Non-permissive license ⚠️

| Model | Params | Languages | Cloning | License | Notes |
|-------|--------|----------|---------|---------|-------|
| **OmniVoice** | ~0.6B (Qwen3-0.6B) | **600+** | Yes | **CC-BY-NC** ⚠️ | Claims RTF 0.025 (40x realtime) — unverified; broadest coverage |
| **ICE-012 Audio** | ~? (28-layer, 1024 hidden) | **590** | Yes | **CC-BY-NC** ⚠️ | Streaming; voice controls (gender/age/pitch/accent/style/speed) |

---

## Detailed Analysis

### 1. Qwen3-TTS 0.6B — speed gap
Measured 0.83x realtime (0.6B Base, PyTorch bf16 on ROCm) vs official docs' claim of 0.288 RTF.
Likely causes: absence of `torch.compile` and CUDA Graph, ROCm vs NVIDIA performance gap.

### 2. CuteTTS (OPPOer/CuteTTS)
~230M params, ultra-low latency (~40 ms TTFA), strong zero-shot cloning. 
Runs on GPU/CPU/Apple Silicon. Apache-2.0.

### 3. Kokoro-82M (hexgrad/Kokoro-82M)
82M params, very fast and cheap, but language coverage (no ru/de) is limited. 
Measured RTF ~1.29 on CPU.

### 4. OmniVoice (k2-fsa/OmniVoice)
600+ languages, RTF as low as 0.025. 
**Constraint:** CC-BY-NC model license (non-commercial).

### 5. Sopro v2 Turbo (samuel-vitorino/sopro-v2-turbo)
120M params, streams on CPU (~300 ms TTFA). English-centric. Apache-2.0.

### 6. Audio8 TTS Preview
0.1B (experimental license) and 0.6B (Apache-2.0). 
0.6B is a solid GPU-tier candidate (11 langs, ~1 GiB).

### 7. MOSS-TTS-Local (OpenMOSS-Team/MOSS-TTS-Local-Transformer)
1.7B params, supports 20 languages including **ru and de**. 
Strongest permissive-license candidate for Russian.

---

## Benchmark Methodology & Results

**Test text** (~105 words):
"When we look at the world, we see it through the lens of our own experience... [etc]"

**Setup:** All backends running in dedicated dev containers. CPU backends run CPU-only; GPU backends run on ROCm (bf16).

### Results
| Backend | Tier | Time | RTF | File size | Notes |
|---------|------|------|-----|-----------|-------|
| **Piper** | CPU | **0.93s** | **~0.03** | 239K | **Fastest** — CPU-native ONNX |
| **Supertonic** | GPU (ONNX) | 6.2s | ~0.18 | 280K | 6.7× slower than Piper |
| **CuteTTS** | CPU (PyTorch) | **73.8s** | **~2.4** | 244K | **79× slower than Piper** |
| **Kokoro-82M** | CPU (ONNX) | 40.5s | ~1.29 | 252K | **43× slower than Piper** |
| **Audio8 0.1B** | CPU (ONNX) | 72.4s | ~2.1 | 3.0M WAV | **78× slower than Piper** |
| **Qwen3-TTS 0.6B** | GPU (PyTorch) | 45.4s | ~1.38 | 264K | **49× slower than Piper** |
| **Qwen3-TTS 1.7B** | GPU (PyTorch) | 62.0s | ~1.76 | 281K | **67× slower than Piper** |
| **MOSS-TTS-Local** | GPU (PyTorch) | 60.8s | ~1.83 | 266K | **65× slower than Piper** |

**RTF calculation:** RTF = generation time / audio duration.
