#!/usr/bin/env python3
"""
Unified TTS backend container — build, run, and manage.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONTAINER_NAME = "tts-backend"
IMAGE_NAME = "localhost/tts-backend:latest"
CONTAINERFILE = PROJECT_DIR / "unified" / "Containerfile"
UI_PORT = 18110  # external port for UI (distinct from main pod's 18100)
BACKEND_PORTS = {"piper": 8095, "supertonic": 8096}  # internal ports
CONTAINER_CACHE_DIR = "/root/.cache"  # internal mount for persistent backend data


def get_runtime():
    """Resolve the container runtime: TTS_RUNTIME env -> podman -> docker."""
    runtime = os.environ.get("TTS_RUNTIME")
    if runtime in ["podman", "docker"]:
        return runtime

    for cmd in ["podman", "docker"]:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, check=True)
            return cmd
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    print("ERROR: No container runtime (podman or docker) found.", file=sys.stderr)
    sys.exit(1)


RUNTIME_BIN = get_runtime()


def podman(*args, check=True, capture=False):
    cmd = [RUNTIME_BIN, *args]
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if check and r.returncode != 0:
            print(f"ERROR: {' '.join(cmd)}\n{r.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        return r.stdout.strip()
    else:
        subprocess.run(cmd)
        if check:
            sys.exit(0)


def resolve_models_root():
    """Resolve the root directory for all model weights.
    Fallback: .env -> .env.example -> ~/.cache/tts-models
    """
    # 1. Check .env (loaded into os.environ usually)
    val = os.environ.get("TTS_MODELS_ROOT")

    # 2. Check .env.example as fallback
    if not val and Path(".env.example").exists():
        with open(".env.example") as f:
            for line in f:
                if line.startswith("TTS_MODELS_ROOT="):
                    val = line.split("=", 1)[1].strip()
                    break

    # 3. Hardcoded default
    if not val:
        val = str(Path.home() / ".cache" / "tts-models")

    return Path(val).expanduser().resolve()


MODELS_ROOT = resolve_models_root()


def build():
    """Build the container image locally (does not start it)."""
    print(f"Building {IMAGE_NAME} ...")
    cmd = [
        RUNTIME_BIN,
        "build",
        "-f",
        str(CONTAINERFILE),
        "-t",
        IMAGE_NAME,
        str(PROJECT_DIR),
    ]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"ERROR: {cmd}\n(exit code {r.returncode})", file=sys.stderr)
        sys.exit(1)
    print(f"Built {IMAGE_NAME}")


def image_exists():
    """Return True if IMAGE_NAME is already present locally."""
    r = subprocess.run(
        [RUNTIME_BIN, "image", "inspect", IMAGE_NAME], capture_output=True
    )
    return r.returncode == 0


def volumes():
    """Single bind mount for all persistent backend data.
    Host $TTS_MODELS_ROOT (see resolve_models_root) is mounted at
    CONTAINER_CACHE_DIR; both backends keep their weights under it."""
    MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    return ["-v", f"{MODELS_ROOT}:{CONTAINER_CACHE_DIR}:z"]


def start():
    # Build first if the image isn't there yet.
    if not image_exists():
        print(f"Image {IMAGE_NAME} not found — building it first ...")
        build()

    # Stop existing if any
    stop()

    cmd = [
        RUNTIME_BIN,
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-p",
        f"{UI_PORT}:8099",  # UI listens on 8099 inside container
    ]

    vols = volumes()
    print("Mounted volumes")
    for vol in vols:
        print(vol)
    cmd += vols
    cmd += [IMAGE_NAME]

    print(f"Starting {CONTAINER_NAME} (UI at http://localhost:{UI_PORT}) ...")
    subprocess.run(cmd)

    print(f"\n✅ UI: http://localhost:{UI_PORT}")
    print(f"🚀 First run? Models are downloading in the background.")
    print(f"👉 Run `python3 start.py logs` to watch the progress.")

    print(f"\nQuick tests:")
    print(
        f'  Piper:      curl -X POST http://127.0.0.1:{UI_PORT}/api/audio/speech -H \'Content-Type: application/json\' -d \'{{"input":"Hi","backend":"piper","voice":"en_US-lessac-medium"}}\''
    )
    print(
        f'  Supertonic: curl -X POST http://127.0.0.1:{UI_PORT}/api/audio/speech -H \'Content-Type: application/json\' -d \'{{"input":"Hi","backend":"supertonic","voice":"M3"}}\''
    )


def stop():
    try:
        subprocess.run(
            [RUNTIME_BIN, "rm", "-f", CONTAINER_NAME], capture_output=True, check=True
        )
        print(f"Stopped {CONTAINER_NAME}")
    except subprocess.CalledProcessError:
        pass  # not running


def status():
    out = podman(
        "ps",
        "-a",
        "--filter",
        f"name=^{CONTAINER_NAME}$",
        "--format",
        "{{.Names}}\t{{.Status}}",
        capture=True,
        check=False,
    )
    if out:
        print(out)
    else:
        print(f"{CONTAINER_NAME}: not running")


def logs(follow=False):
    cmd = [RUNTIME_BIN, "logs"]
    if follow:
        cmd.append("-f")
    cmd.append(CONTAINER_NAME)
    subprocess.run(cmd)


def restart():
    stop()
    start()


def test():
    """Full smoke test: build + start + verify + stop."""
    build()
    start()

    # Wait for startup
    print("Waiting for services to come up ...")
    time.sleep(10)

    import urllib.request

    # Check /config — verify both backends, models, and voices
    url = f"http://127.0.0.1:{UI_PORT}/config"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            cfg = json.loads(r.read())
            backends = cfg.get("backends", [])
            models = cfg.get("models", {})
            voices = cfg.get("voices", {})
            print(f"Backends: {len(backends)}")
            for b in backends:
                label = b["label"]
                print(
                    f"  {label}: models={models.get(label, [])} voices={voices.get(label, [])}"
                )
    except Exception as e:
        print(f"WARNING: /config not reachable: {e}", file=sys.stderr)

    # Test TTS (piper)
    print("Testing TTS (piper) ...")
    data = json.dumps(
        {"input": "Hello world", "backend": "piper", "voice": "en_US-lessac-medium"}
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{UI_PORT}/api/audio/speech",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            audio = r.read()
            print(f"  piper: OK ({len(audio)} bytes)")
    except Exception as e:
        print(f"  piper: FAILED ({e})", file=sys.stderr)

    # Test TTS (supertonic)
    print("Testing TTS (supertonic) ...")
    data = json.dumps(
        {"input": "Hello world", "backend": "supertonic", "voice": "M3"}
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{UI_PORT}/api/audio/speech",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            audio = r.read()
            print(f"  supertonic: OK ({len(audio)} bytes)")
    except Exception as e:
        print(f"  supertonic: FAILED ({e})", file=sys.stderr)

    stop()
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=["build", "start", "stop", "restart", "status", "logs", "test"],
        help="Action to perform (default: start)",
    )
    args = parser.parse_args()

    if args.command == "build":
        build()
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "restart":
        restart()
    elif args.command == "status":
        status()
    elif args.command == "logs":
        logs(follow=True)
    elif args.command == "test":
        test()


if __name__ == "__main__":
    main()
