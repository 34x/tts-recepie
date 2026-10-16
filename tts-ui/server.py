#!/usr/bin/env python3
"""Tiny TTS UI server — proxies OpenAI-compatible /v1/audio/speech to an engine
(e.g. piper-tts in the pod) and streams the raw audio bytes straight to the
browser. Engine URL is env-overridable so a different TTS backend can be
plugged in without touching the code.

Long texts are split into sentence-ish chunks and synthesized sequentially
with a small lookahead, each chunk's audio bytes concatenated onto a single
mp3 response — playback starts after the first chunk and is gapless.

Env:
    TTS_BACKENDS       JSON array of backend descriptors, first = default.
                       Each: {"label": str, "url": str}  (url = /v1/audio/speech endpoint)
                       Optional "voices": [str] per backend (unused for routing today).
    TTS_API_KEY        Optional Bearer key, forwarded to the engine
    TTS_CHUNK_CHARS    Target chunk size in chars (default: 500)
    TTS_CHUNK_HARD     Hard cap for pathological long sentences (default: 1000)
    TTS_CHUNK_LOOKAHEAD  Chunks synthesized ahead of streaming (default: 1)
    PORT               Listen port (default: 8095)
    UI_TTS_CACHE_MAX_ENTRIES  in-process LRU cache size for encoded chunk
                              audio; 0 disables (default 200). See _cache_key.
"""

from collections import OrderedDict

import concurrent.futures
import json
import mimetypes
import os
import queue
import re
import socket
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_BACKENDS = [
    {"label": "piper", "url": "http://127.0.0.1:5050/v1/audio/speech"},
]
BACKENDS = json.loads(os.environ.get("TTS_BACKENDS", json.dumps(DEFAULT_BACKENDS)))
BACKEND_URLS = {b["label"]: b["url"] for b in BACKENDS}
# Per-backend model discovery timeout (seconds). Each backend's /v1/models is
# probed on /config with this cap so a slow/dead backend doesn't stall the UI.
MODELS_TIMEOUT = float(os.environ.get("TTS_MODELS_TIMEOUT", "1.0"))

TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
PORT = int(os.environ.get("PORT", "8095"))
# Upstream engine request timeout (seconds). Env-driven so the qwen backend can
# tolerate slow multi-chunk synthesis without a rebuild; default 120 preserves
# Piper's existing behaviour. Raised via TTS_PROXY_TIMEOUT for the dev UI launch.
TTS_PROXY_TIMEOUT = int(os.environ.get("TTS_PROXY_TIMEOUT", "120"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CHUNK_CHARS = int(os.environ.get("TTS_CHUNK_CHARS", "500"))
CHUNK_HARD = int(os.environ.get("TTS_CHUNK_HARD", "1000"))
CHUNK_LOOKAHEAD = int(os.environ.get("TTS_CHUNK_LOOKAHEAD", "1"))
STREAM_CHUNK_CHARS = int(os.environ.get("TTS_STREAM_CHUNK_CHARS", "200"))
# Long random-looking default so raw MP3 payload bytes cannot coincidentally
# form the boundary string (mp3 frames start with 0xFFFB — short boundaries risk false matches).
STREAM_BOUNDARY = os.environ.get("TTS_STREAM_BOUNDARY", "tts-chunk-a1b2c3d4e5f6")
# Hard cap on input length (chars). Inputs longer than this are truncated —
# the truncation is reported via the X-Input-Truncated response header so the
# client can surface it instead of silently stopping mid-text. Default 100k
# covers long-form content (e.g. 1-2 hour lectures); raise via env if needed.
MAX_INPUT_CHARS = int(os.environ.get("TTS_MAX_INPUT_CHARS", "100000"))
# In-process LRU cache of encoded chunk audio, keyed by
# (model, text, voice, instruct, format) — see _cache_key for the cross-backend
# assumption. Lives only in this process; vanishes on restart. 0 disables.
UI_TTS_CACHE_MAX_ENTRIES = int(os.environ.get("UI_TTS_CACHE_MAX_ENTRIES", "200"))

# ~0.5 s of near-silence as a full-rate mp3 frame block (mp3 frames are
# self-contained and concatenate cleanly — same property as typical streaming APIs.
# textToSpeech path relies on). Used to patch over a failed chunk.
# Real 1.5s silent mp3 (static/silence.mp3) prepended as part 0 so the
# browser's first-play() startup latency is absorbed by silence instead of
# clipping the first word. Loaded once at startup.
try:
    with open(os.path.join(STATIC_DIR, "silence.mp3"), "rb") as _f:
        LEAD_SILENCE_MP3 = _f.read()
except OSError:
    LEAD_SILENCE_MP3 = b""

SILENCE_MP3 = bytes.fromhex(
    "fffbf064c0000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _discover_models(backend: dict) -> list[str]:
    """Fetch a backend's model list from its /v1/models endpoint.

    Returns the model ids (or [] if the backend doesn't advertise any). The
    request is bounded by MODELS_TIMEOUT so a slow/dead backend can't stall
    the UI. A backend that declares an explicit `models` list in its descriptor
    (e.g. audio8, which has no /v1/models) is used verbatim without probing.
    """
    declared = backend.get("models")
    if declared:
        return list(declared)
    base = backend.get("url", "")
    if not base:
        return []
    # /v1/audio/speech -> /v1/models
    models_url = base.rsplit("/v1/audio/speech", 1)[0] + "/v1/models"
    req_headers = {}
    if TTS_API_KEY:
        req_headers["Authorization"] = f"Bearer {TTS_API_KEY}"
    try:
        req = urllib.request.Request(models_url, headers=req_headers, method="GET")
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data", []) if isinstance(data, dict) else data
        return [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []


def _discover_voices(backend: dict) -> list[str]:
    """Fetch a backend's voice list from its /v1/audio/voices endpoint.

    This is a *project* discovery contract (the rudy TTS backbone), not an
    OpenAI API endpoint — the OpenAI spec has no voice listing, voice names are
    a fixed enum there. Returns the voice names (or [] if the backend doesn't
    advertise them, e.g. piper). Bounded by MODELS_TIMEOUT like _discover_models.
    A backend that declares an explicit `voices` list in its descriptor is used
    verbatim without probing.
    """
    declared = backend.get("voices")
    if declared:
        return list(declared)
    base = backend.get("url", "")
    if not base:
        return []
    # /v1/audio/speech -> /v1/audio/voices
    voices_url = base.rsplit("/v1/audio/speech", 1)[0] + "/v1/audio/voices"
    req_headers = {}
    if TTS_API_KEY:
        req_headers["Authorization"] = f"Bearer {TTS_API_KEY}"
    try:
        req = urllib.request.Request(voices_url, headers=req_headers, method="GET")
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("voices", []) if isinstance(data, dict) else data
        return [v for v in items if isinstance(v, str)]
    except Exception:
        return []


def _proxy(url: str, data: bytes | None = None, headers: dict | None = None):
    """Forward an HTTP request to the engine, return (status, content_type, body)."""
    req_headers = {"Content-Type": "application/json"}
    if TTS_API_KEY:
        req_headers["Authorization"] = f"Bearer {TTS_API_KEY}"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TTS_PROXY_TIMEOUT) as resp:
            return resp.status, resp.headers.get("Content-Type", "application/octet-stream"), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, "application/json", e.read()
    except Exception as e:
        return 502, "application/json", json.dumps({"detail": f"upstream error: {e}"}).encode()


# A `.` only ends a sentence when followed by whitespace/end — otherwise it's
# part of a token (e.g. "Apache2.0", "1.2.3", "$5.00"). `?`/`!`/`…`/newline always
# terminate. This prevents splitting mid-token like "Apache2." + "0".
_SENTENCE_RE = re.compile(r"(?:[^.!?\n…]|\.(?!\s|$))+(?:[!?\n…]+|\.(?=\s|$))?")


# Markdown-ish heuristic: ATX headers (`# …`, `###### …`) at line start. When a
# text has several of them it is treated as markdown and split at section
# boundaries first — chunks then follow the document's own structure instead of
# arbitrary sentence groupings. Header-like lines inside fenced code blocks are
# ignored (a `# comment` in a code sample is not a section).
_HEADER_RE = re.compile(r"^#{1,6}[ \t]+\S")
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
_LINE_RE = re.compile(r"^.*$", re.MULTILINE)


def _markdown_sections(text: str) -> list[str]:
    """Split markdown-ish `text` at header lines (headers start each section).

    Returns [] when fewer than 2 headers are found (not markdown enough to
    justify overriding sentence grouping). Preamble before the first header is
    kept as its own section when non-empty.
    """
    starts: list[int] = []
    fenced = False
    for m in _LINE_RE.finditer(text):
        line = m.group()
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced and _HEADER_RE.match(line):
            starts.append(m.start())
    if len(starts) < 2:
        return []

    # Contiguous slices: each section runs to the next header, so the
    # whitespace between sections (blank lines) stays with the preceding
    # one. Stripping each section and joining without a separator would
    # delete it, which is what glued paragraphs together. Preamble before
    # the first header becomes its own slice when it has content.
    bounds = starts + [len(text)]
    sections: list[str] = []
    pre = text[: starts[0]]
    if pre.strip():
        sections.append(pre)
    for i, start in enumerate(starts):
        sec = text[start:bounds[i + 1]]
        if sec.strip():
            sections.append(sec)
    return sections


def _sentence_slices(text: str) -> list[str]:
    """Split `text` into contiguous sentence slices.

    Each slice runs up to (and includes) the whitespace before the next
    sentence, so concatenating the slices reproduces `text` exactly. The
    sentence regex stops at the terminator; without this, inter-sentence
    whitespace (notably newlines) would match no sentence and be dropped by
    the caller's join, gluing "a.\\n\\nB" into "a.B".
    """
    out: list[str] = []
    pos = 0
    for m in _SENTENCE_RE.finditer(text):
        gap = text[pos:m.start()]
        piece = m.group()
        if gap:
            if out:
                out[-1] += gap
            else:
                piece = gap + piece
        out.append(piece)
        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        if out:
            out[-1] += tail
        else:
            out.append(tail)
    return out


def _sentence_chunks(text: str, target: int, hard: int) -> list[str]:
    """Group sentences of `text` into ~target-char chunks."""
    sentences = _sentence_slices(text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > hard:
            # pathological: force-split this sentence on its own
            if buf:
                chunks.append(buf)
                buf = ""
            while len(s) > hard:
                cut = max(s.rfind(",", 0, hard), s.rfind(" ", 0, hard), hard)
                chunks.append(s[: cut + 1] if cut < hard else s[:hard])
                s = s[cut + 1 if cut < hard else hard:]
            if s.strip():
                buf = s
            continue
        if buf and len(buf) + len(s) > target:
            chunks.append(buf)
            buf = s
        else:
            buf += s
    if buf.strip():
        chunks.append(buf)
    return chunks


def chunk_text(text: str, target: int = CHUNK_CHARS, hard: int = CHUNK_HARD) -> list[str]:
    """Split text into sentence-ish chunks of ~target chars.

    Markdown-ish text (≥2 ATX headers) is first split at section boundaries,
    then each section is grouped into sentence-ish chunks — so chunks follow
    the document structure instead of arbitrary sentence runs. Plain text and
    short/non-markdown input fall through to pure sentence grouping.

    Never splits inside a sentence unless a single sentence exceeds `hard`,
    in which case it force-splits at the last comma/space before the cap.
    """
    sections = _markdown_sections(text)
    if not sections:
        return _sentence_chunks(text, target, hard)
    chunks: list[str] = []
    for sec in sections:
        chunks.extend(_sentence_chunks(sec, target, hard))
    return chunks


class AudioCache:
    """Bounded in-process LRU cache of encoded audio bytes.

    Keyed by (model, text, voice, instruct, format); see _cache_key for the
    cross-backend assumption baked into the key. A cached value is a
    (content_type, body) pair. Insertion / lookup are serialized by a lock;
    the cache is process-local and vanishes on restart.
    """

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._store: "OrderedDict[tuple, tuple[str, bytes]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> tuple[str, bytes] | None:
        if self.max_entries <= 0:
            return None
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: tuple, value: tuple[str, bytes]):
        if self.max_entries <= 0:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)


CACHE = AudioCache(UI_TTS_CACHE_MAX_ENTRIES)


def _cache_key(payload: dict) -> tuple:
    """Cache key for a chunk request.

    NOTE: the backend URL is deliberately NOT part of the key. We assume
    different backends with the same voice/model/instruct/fmt/text will
    produce exactly the same files (byte-identical encoded output). If that
    assumption ever breaks, include the backend URL in the key.
    """
    return (
        payload.get("model", ""),
        payload.get("input", ""),
        payload.get("voice", ""),
        payload.get("instruct", ""),
        payload.get("response_format", "mp3"),
    )


class ChunkSynthesizer:
    """Pipelines chunk synthesis with a small lookahead while the handler
    streams finished chunks out."""

    def __init__(self, chunks: list[str], payload_base: dict, url: str):
        self.chunks = chunks
        self.payload_base = payload_base
        self.url = url
        self.q: queue.Queue = queue.Queue()      # results, keyed by idx
        self.work: queue.Queue = queue.Queue()   # pending chunk indexes
        for idx in range(len(chunks)):
            self.work.put(idx)
        self.aborted = threading.Event()

    def start(self, n_workers: int):
        for _ in range(max(1, n_workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        """Each lookahead worker pulls chunk indexes from the work queue."""
        while not self.aborted.is_set():
            try:
                idx = self.work.get_nowait()
            except queue.Empty:
                return
            payload = dict(self.payload_base, input=self.chunks[idx])
            key = _cache_key(payload)
            cached = CACHE.get(key)
            if cached is not None:
                ctype, audio = cached
                self.q.put((idx, 200, ctype, audio, 0.0))
                continue
            t0 = time.monotonic()
            status, ctype, audio = _proxy(self.url, json.dumps(payload).encode())
            elapsed = time.monotonic() - t0
            if status == 200:
                CACHE.put(key, (ctype, audio))
            self.q.put((idx, status, ctype, audio, elapsed))

    def stream(self, write):
        """In-order chunk pipeline.

        Yields (idx, status) per synthesized chunk; `write(idx, status, audio)`
        fires for each ready part so the caller can emit it (or patch with
        silence). Lookahead workers fill the queue concurrently.

        Client disconnects are detected via write errors (BrokenPipeError /
        ConnectionResetError) raised by `write`, not by polling the socket —
        polling recv() misreads a half-close (FIN) as a disconnect, which
        aborts the stream on mobile Safari/Brave.
        """
        pending = {}          # idx -> future result, for in-order delivery
        next_idx = 0
        ctype_override = None
        for idx in range(len(self.chunks)):
            # workers push results straight into the queue; drain what's ready
            result = None
            while result is None:
                if pending:
                    result = pending.pop(next_idx, None)
                if result is None:
                    # block briefly for this chunk's result
                    try:
                        item = self.q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if not isinstance(item, tuple):
                        continue  # stray queue item
                    idx_r, status, ctype, audio, elapsed = item
                    if idx_r == next_idx:
                        result = (status, ctype, audio, elapsed)
                    else:
                        pending[idx_r] = (status, ctype, audio, elapsed)
            status, ctype, audio, elapsed = result
            next_idx += 1
            write(next_idx - 1, status, audio, elapsed)
        # All chunks were delivered in order; signal completion to the reader.
        yield ("done", 200)
        self.aborted.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        """Disable Nagle and use an unbuffered write stream.

        The multipart stream writes a small metadata part (all chunk texts)
        before any audio. Without TCP_NODELAY the kernel coalesces that write
        with the first audio bytes (produced seconds later by the synthesizer),
        so the client can't render the chunk list until audio starts arriving.
        TCP_NODELAY + unbuffered wfile forces the metadata part out immediately,
        so chunk text appears ~seconds before the first sound.
        """
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.wfile = self.connection.makefile("wb", buffering=0)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _resolve_backend(self, body: dict) -> tuple[str, dict]:
        """Return (url, backend) for the requested label, falling back to the
        first-listed backend for unknown/empty labels."""
        label = body.get("backend") or (BACKENDS[0]["label"] if BACKENDS else "")
        if not BACKENDS:
            return "", {}
        backend = next((b for b in BACKENDS if b["label"] == label), BACKENDS[0])
        return backend["url"], backend

    def do_GET(self):
        if self.path == "/config":
            # Expose the backend list so the UI can render a selector. The
            # first-listed backend is the default. Each backend's model list is
            # probed concurrently (bounded by MODELS_TIMEOUT) so the UI can
            # populate a model selector without a per-backend round-trip.
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(BACKENDS) or 1) as ex:
                futures = {b["label"]: ex.submit(_discover_models, b) for b in BACKENDS}
                models = {label: f.result() for label, f in futures.items()}
                vfutures = {b["label"]: ex.submit(_discover_voices, b) for b in BACKENDS}
                voices = {label: f.result() for label, f in vfutures.items()}
            payload = {"backends": BACKENDS, "models": models, "voices": voices}
            self._send(200, "application/json", json.dumps(payload).encode())
            return
        if self.path == "/api/cache/stats":
            with CACHE._lock:
                size = len(CACHE._store)
            self._send(200, "application/json", json.dumps({
                "entries": size,
                "max_entries": CACHE.max_entries,
                "hits": CACHE.hits,
                "misses": CACHE.misses,
            }).encode())
            return
        # Generic static file serving: any non-API, non-config GET resolves
        # against STATIC_DIR. Prevents path traversal via normpath + prefix check.
        rel = self.path.split("?")[0].lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR)):
            self._send(403, "text/plain", b"forbidden")
            return
        if not os.path.isfile(full):
            self._send(404, "text/plain", b"not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            self._send(200, ctype, f.read())

    def _payload_for(self, backend: dict, text: str, body: dict) -> dict:
        """Build the per-backend /v1/audio/speech payload for `text` (whole
        text for the chunk endpoints, one chunk for /api/audio/speech).

        Per-backend descriptor logic:
        - `format`:   response format override (audio8 needs wav/pcm), default mp3.
        - `send_model False`: backends without a model field get none.
        - `instructions`: only these backends receive `instruct`.
        """
        payload = {
            "input": text,
            "voice": body.get("voice") or "",
            "response_format": backend.get("format", "mp3"),
        }
        if backend.get("send_model", True):
            payload["model"] = body.get("model") or ""
        if backend.get("instructions"):
            payload["instruct"] = body.get("instruct") or ""
        return payload

    def _resolution(self, body: dict) -> tuple[list, str, dict]:
        """Return (chunks, text, backend) shared by both endpoints, resolving
        the backend + optional instruction."""
        chunks = chunk_text(str(body.get("input", ""))[:MAX_INPUT_CHARS], STREAM_CHUNK_CHARS)
        text = "".join(chunks)
        url, backend = self._resolve_backend(body)
        payload = self._payload_for(backend, text, body)
        return chunks, text, payload, backend, url

    def _chunk_speech(self, body: dict):
        """Single-chunk OpenAI-shaped passthrough endpoint.

        In:  {"input": <ONE chunk's text>, voice?, model?, instruct?, backend?}
        Out: raw audio bytes in the backend's declared `format`, or a non-200
        JSON error. NO chunking here — the caller (chunked client mode) makes
        one request per chunk; the client owns chunking, ordering and retry.
        """
        text = str(body.get("input", ""))[:MAX_INPUT_CHARS]
        if not text.strip():
            self._send(400, "application/json", b'{"detail":"input text is empty"}')
            return
        url, backend = self._resolve_backend(body)
        payload = self._payload_for(backend, text, body)
        key = _cache_key(payload)
        cached = CACHE.get(key)
        if cached is not None:
            ctype, audio = cached
            self._send(200, ctype, audio)
            return
        status, ctype, audio = _proxy(url, json.dumps(payload).encode())
        if status == 200:
            CACHE.put(key, (ctype, audio))
            self._send(200, ctype, audio)
        else:
            # Surface the failure verbatim — the client retries per chunk;
            # no silence patching (unlike the stream endpoints).
            self._send(status, ctype, audio)

    def _stream_multipart(self, body: dict):
        """W3C-style streaming endpoint.

        Same chunk pipeline as /api/tts but each synthesized chunk is emitted
        as a separate multipart/x-mixed-replace part (own headers + raw audio),
        so clients can start playing after the first sentence instead of
        waiting for the whole track to concatenate into one mp3 stream.
        """
        text = str(body.get("input", ""))[:MAX_INPUT_CHARS]
        truncated = len(str(body.get("input", ""))) > MAX_INPUT_CHARS
        if not text.strip():
            self._send(400, "application/json", b'{"detail":"input text is empty"}')
            return
        chunks, text, payload, _backend, backend_url = self._resolution(body)
        n_chunks = len(chunks)

        self.send_response(200)
        # NOT multipart/x-mixed-replace: Safari treats that content type as a
        # document stream (MJPEG mechanism) and does not deliver it to XHR/
        # fetch — the request fails with "load failed". The client parses the
        # boundary from the body's first line, so a plain content type works
        # and avoids Safari's special multipart handling.
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        # Expose the multipart boundary as a header so byte-stream clients can
        # construct the parser from metadata instead of scraping the body's
        # first line. The body still starts with "--<boundary>\r\n", so
        # first-line scraping remains a valid fallback.
        self.send_header("X-Stream-Boundary", STREAM_BOUNDARY)
        self.send_header("X-Part-Count", str(n_chunks + 1))
        self.send_header("X-Input-Chars", str(len(text)))
        if truncated:
            self.send_header("X-Input-Truncated", "1")
        self.end_headers()

        # Transfer-Encoding chunked requires explicit framing for every write.
        def send(data: bytes):
            if data:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        # Emit metadata part before any audio so the client can render the
        # full chunk list immediately with per-chunk status.
        # Index 0 is reserved for the lead silence part (empty text placeholder)
        # so chunks[i] always corresponds to X-Part-Index: i.
        all_chunks = [""] + chunks
        meta = json.dumps({"chunks": all_chunks, "char_counts": [0] + [len(c) for c in chunks]}).encode()
        # Content-Length lets the client emit each part as soon as its bytes
        # arrive, instead of waiting for the next boundary to close the body.
        send((f"--{STREAM_BOUNDARY}\r\n"
              f"Content-Type: application/json\r\n"
              f"Content-Length: {len(meta)}\r\n"
              f"\r\n").encode())
        send(meta)
        send(b"\r\n")

        synth = ChunkSynthesizer(chunks, payload, backend_url)
        synth.start(CHUNK_LOOKAHEAD)

        def emit(idx: int, status: int, audio: bytes, elapsed: float = 0.0):
            # Part 0 lead silence is emitted together with the first real
            # chunk (idx 0) so the browser's first-play() startup latency is
            # absorbed by silence AND chunk 1 is already buffered when the
            # silence ends — no wait after it.
            if idx == 0 and LEAD_SILENCE_MP3:
                head = (f"--{STREAM_BOUNDARY}\r\n"
                        f"Content-Type: audio/mpeg\r\n"
                        f"X-Part-Index: 0\r\n"
                        f"Content-Length: {len(LEAD_SILENCE_MP3)}\r\n"
                        f"\r\n").encode()
                send(head)
                send(LEAD_SILENCE_MP3)
                send(b"\r\n")
                print(f"[multipart] emitted part 0 (lead silence, {len(LEAD_SILENCE_MP3)} bytes)", flush=True)
            # Real chunks are offset by +1: part 0 is the lead silence.
            # When the backend fails (status != 200) we still emit a part so
            # the stream stays well-formed, but we tag it with X-Part-Error so
            # the client can surface the failure instead of silently playing
            # the silence patch. Without this, a dead backend looks like a
            # stream of 0-byte/silence parts with no visible error.
            err_hdr = "" if status == 200 else f"X-Part-Error: {status}\r\n"
            time_hdr = f"X-Part-Time: {elapsed:.3f}\r\n"
            body = audio if status == 200 else SILENCE_MP3
            head = (f"--{STREAM_BOUNDARY}\r\n"
                    f"Content-Type: audio/mpeg\r\n"
                    f"X-Part-Index: {idx + 1}\r\n"
                    f"{time_hdr}"
                    f"{err_hdr}"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n").encode()
            send(head)
            send(body)
            send(b"\r\n")
            if status == 200:
                print(f"[multipart] emitted part {idx + 1} ({len(audio)} bytes, {elapsed:.2f}s)", flush=True)
            else:
                print(f"[multipart] emitted part {idx + 1} (BACKEND ERROR HTTP {status}, patched with silence)", flush=True)

        try:
            for _event in synth.stream(emit):
                pass   # emit() does the per-part writing; loop drives the pipeline
            print(f"[multipart] stream loop done", flush=True)
            send((f"--{STREAM_BOUNDARY}--\r\n").encode())
            # Chunked-encoding terminator must be written RAW — send() wraps
            # its payload in a chunk frame, which would turn `0\r\n\r\n` into
            # a 3-byte chunk and leave the response unterminated (browser
            # fetch stream never closes -> stall watchdog fires).
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            synth.aborted.set()

    def do_POST(self):
        # Respect the client's keep-alive preference. The streaming endpoint
        # terminates with a proper chunked-end marker, so the connection can
        # stay open; closing it mid-stream (the old behaviour) truncated the
        # multipart body after part 0 and desynced every client parser.
        if self.headers.get("Connection", "").lower() == "close":
            self.close_connection = True
        if self.path == "/api/speech/multipart":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, "application/json", b'{"detail":"invalid json"}')
                return
            self._stream_multipart(body)
            return
        if self.path in ("/api/audio/speech", "/v1/audio/speech"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, "application/json", b'{"detail":"invalid json"}')
                return
            self._chunk_speech(body)
            return
        if self.path == "/api/tts":
            self._send(404, "text/plain", b"not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, "application/json", b'{"detail":"invalid json"}')
            return

        text = str(body.get("input", ""))[:MAX_INPUT_CHARS]
        if not text.strip():
            self._send(400, "application/json", b'{"detail":"input text is empty"}')
            return
        _chunks, text, payload, _backend, backend_url = self._resolution(body)

        chunks = chunk_text(text)
        if len(chunks) <= 1:
            # POC fast path: single request, buffered response (also covers
            # engines that don't concatenate mp3 cleanly)
            key = _cache_key(payload)
            cached = CACHE.get(key)
            if cached is not None:
                ctype, audio = cached
                self._send(200, ctype, audio)
                return
            status, ctype, audio = _proxy(backend_url, json.dumps(payload).encode())
            if status == 200:
                CACHE.put(key, (ctype, audio))
            self._send(status if status != 200 else 200, ctype, audio)
            return

        # Chunked path: stream chunks onto one mp3 response.
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(data: bytes):
            if not data:
                return
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def emit(idx, status, audio, elapsed=0.0):
            if status == 200:
                write_chunk(audio)
            else:
                # skip-failure policy: patch with silence, keep the mp3 stream valid
                write_chunk(SILENCE_MP3)

        synth = ChunkSynthesizer(chunks, payload, backend_url)
        synth.start(CHUNK_LOOKAHEAD)
        try:
            for _event in synth.stream(emit):
                pass   # emit() does the per-part writing; loop drives the pipeline
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            synth.aborted.set()
        finally:
            synth.aborted.set()


if __name__ == "__main__":
    print(f"tts-ui: backends={[b['label'] for b in BACKENDS]} default={BACKENDS[0]['label'] if BACKENDS else ''} "
          f"chunks={CHUNK_CHARS} lookahead={CHUNK_LOOKAHEAD} listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
