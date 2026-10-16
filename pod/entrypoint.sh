#!/bin/bash
# Piper TTS container entrypoint.
#
# On first run, /piper/voices is empty (fresh volume mount). Download the
# three default voices. On subsequent runs, voices are already cached on
# the host via the volume mount — skip the download.
#
# To force a re-download, delete the host cache:
#   Refer to your .env file for the host path.
#
set -euo pipefail

VOICES_DIR="${PIPER_VOICES_DIR:-/piper/voices}"

# Voice set: English (US), Russian, German
DEFAULT_VOICES=(
    "en_US-lessac-medium"     # English, US female, clear
    "ru_RU-irina-medium"      # Russian, female
    "de_DE-thorsten-medium"   # German, male
)

# Check whether the cache already has all three voices present.
all_present=true
for v in "${DEFAULT_VOICES[@]}"; do
    if [ ! -f "${VOICES_DIR}/${v}.onnx" ] || [ ! -f "${VOICES_DIR}/${v}.onnx.json" ]; then
        all_present=false
        break
    fi
done

if $all_present; then
    echo "[entrypoint] All ${#DEFAULT_VOICES[@]} voices present in ${VOICES_DIR}, skipping download."
else
    echo "[entrypoint] Downloading missing voices into ${VOICES_DIR}..."
    missing=()
    for v in "${DEFAULT_VOICES[@]}"; do
        if [ ! -f "${VOICES_DIR}/${v}.onnx" ] || [ ! -f "${VOICES_DIR}/${v}.onnx.json" ]; then
            missing+=("$v")
        fi
    done
    python3 -m piper.download_voices \
        --download-dir "${VOICES_DIR}" \
        "${missing[@]}"
    echo "[entrypoint] Download complete."
fi

exec python3 /app/server.py --host 0.0.0.0 --port 5050
