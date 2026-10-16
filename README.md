# 🎙️ TTS — Your Private Speech Appliance

Turn any home computer into a high-performance, offline text-to-speech server. Serve studio-quality speech to your phone, tablet, or browser via a lightweight PWA.

**The Experience**: 
1. Run one command on your server.
2. Open the URL on your phone.
3. Type text, pick a voice, and **Listen**.

[Evaluation notes & benchmarks](EVALUATIONS.md) — compare models by latency and quality.

---

## 🚀 Quick Start (Zero-Config)

**Requirements**: Linux or macOS, [Podman](https://podman.io) or [Docker](https://www.docker.com/), Python 3.10+.

The **Unified Container** is the fastest way to get started. It bundles the UI and the best CPU-compatible engines (Piper, SuperTonic) into one image.

```bash
# 1. Clone the repository and enter the directory
git clone https://github.com/34x/tts-recepie.git
cd tts-recepie

# 2. Build the image and start the server (first run will build the image)
python3 start.py
```

3. **Open the UI**: Navigate to `http://localhost:18110` in your browser.
4. **Speak**: Type any text and hit **Speak**. The default English Piper voice will be downloaded automatically on the first request.
5. **Try SuperTonic**: Click the gear icon ⚙️, select **SuperTonic**, pick a voice, and hit **Speak** to experience studio-quality audio.

> 💡 **First Run**: The engines download their models automatically on the first request. This may take a few minutes (Piper: ~60MB, SuperTonic: ~380MB). 
> Run `python3 start.py logs` to watch the download progress.

### Useful Commands
```bash
python3 start.py status   # Check if the server is running
python3 start.py logs     # View synthesis and download logs
python3 start.py test     # Full smoke test (build + start + verify audio)
python3 start.py stop     # Stop and remove the server
```

---

## 📱 Access from your Phone (Remote Setup)

Since the UI is a PWA, you can use it as a native app on your phone. To connect your phone to your home server:

### Option A: Tailscale (Recommended)
1. Install **Tailscale** on both your server and your phone.
2. Open the browser on your phone and go to `http://<your-server-tailscale-ip>:18110`.
3. "Add to Home Screen" to use it as a standalone app.

### Option B: Local Network
If both devices are on the same Wi-Fi, simply use `http://<your-server-local-ip>:18110`.

---

## 🛠 Architecture

The project is designed as a modular pipeline:

**PWA UI** $\rightarrow$ **UI Proxy** $\rightarrow$ **TTS Backends** $\rightarrow$ **Audio**

- **PWA**: A static JS interface that handles text input and audio playback.
- **UI Proxy**: Chunks long text into sentences and streams them to the backends so you hear the first sentence immediately, without waiting for the whole book to generate.
- **Backends**: OpenAI-compatible servers that wrap various TTS engines.

### Request Lifecycle
1. **Chunking**: Long text is split on sentence boundaries (~500 chars).
2. **Synthesis**: Each chunk is sent to the backend as an OpenAI-style JSON request.
3. **Streaming**: The proxy uses `multipart/x-mixed-replace` to stream mp3 chunks to the browser in real-time.
4. **Caching**: Identical text/voice requests are served instantly from an in-memory LRU cache.

---

## 📚 Supported Backends

| Backend | Quality | Speed | Notes |
|---|---|---|---|
| **Piper** | ⭐⭐⭐ | ⚡⚡⚡ | Extremely fast, best for long-form text. |
| **SuperTonic 3**| ⭐⭐⭐⭐⭐| ⚡⚡ | Studio quality, highly natural. |
| **Kokoro-82M** | ⭐⭐⭐⭐ | ⚡⚡ | Great balance of size and quality. |
| **Qwen-TTS** | ⭐⭐⭐⭐ | ⚡ | Strong multilingual, voice-clone capable. |
| **MOSS-TTS** | ⭐⭐⭐⭐ | ⚡ | Massive variety of voices and languages. |

---

## ⚙️ Configuration

The appliance is zero-config by default, but you can override settings in a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `TTS_MODELS_ROOT` | `~/.cache/tts-models` | Root directory for all model weights |
| `TTS_RUNTIME` | `podman` $\rightarrow$ `docker` | Force a specific container runtime |
| `TTS_API_KEY` | *(none)* | Optional API key for the UI proxy |

---

## 🧪 Adding New Engines

### For Developers (Unified)
1. Create a synthesizer class in `unified/` inheriting from `TTSSynthesizer`.
2. Add the dependency to `unified/pyproject.toml`.
3. Add a `[program:]` block to `unified/supervisord.conf` and a port to `unified/Containerfile`.

### For Power Users (Pod Mode)
If you need GPU support (CUDA/ROCm) or process isolation, use the **Pod Mode**.
This runs each backend in its own container.

```bash
# Start the experimental pod
python3 start_pod.py start
# Open UI at http://localhost:18100
```
*See `start_pod.py` for more details on pod orchestration.*

---

## 📄 License

GPLv3 — see [LICENSE](LICENSE).
