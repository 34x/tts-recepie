#!/usr/bin/env bash
# Dev container for tts-ui: mounted volume, separate from the stack.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="localhost/tts-dev:latest"
NAME="tts-dev"
HOST_PORT=18100

# Piper lives inside the pod's netns (no direct bridge access). Reach it through
# host.containers.internal → rootlessport on 127.0.0.1:5050.
PIPER_URL="${TTS_DEV_PIPER:-http://host.containers.internal:5050/v1/audio/speech}"

build() {
    podman build -t "$IMAGE" \
        --build-arg BASE=localhost/tts-ui:latest \
        -f "$DIR/Containerfile.dev" \
        "$DIR"
}

start() {
    if ! podman image exists "$IMAGE"; then
        build
    fi
    # sanity check piper is reachable before starting (urlopen raises on 4xx, so a
    # successful call = server up; the /v1/models path may not exist)
    podman run --rm "$IMAGE" python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('${PIPER_URL}', timeout=5)
except urllib.error.HTTPError as e:
    if 400 <= e.code < 500: pass  # server answered; fine
    else: raise
except Exception as e:
    print('piper not reachable at ${PIPER_URL}: ' + repr(e), file=sys.stderr); sys.exit(1)
print('ok')" >/dev/null || exit 1

    podman rm -f "$NAME" 2>/dev/null || true
    podman run -d --name "$NAME" \
        -e "TTS_URL=${PIPER_URL}" \
        -e TTS_VOICES=en_GB-alan-medium,ru_RU-irina-medium,de_DE-thorsten-medium,en_US-lessac-medium \
        -p "${HOST_PORT}:8095" \
        -v "${DIR}:/app:z" \
        "$IMAGE"
    echo "up at http://localhost:${HOST_PORT} (piper=${PIPER_URL})"
}

stop()  { podman rm -f "$NAME" 2>/dev/null || true; }
logs()  { podman logs -f --tail=100 "$NAME"; }

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    build)   build ;;
    logs)    logs ;;
    *) echo "Usage: $0 {start|stop|restart|build|logs}"; exit 1 ;;
esac
