#!/usr/bin/env bash
# Run the model downloader inside a throwaway container.
# Reads BABA_MODELS_HOST from .env so models land on the right host path.
# Any args passed to this script are forwarded to download_models.py —
# e.g. `./tools/download_models.sh --minimal` or `--face-only`.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    . ./.env
    set +o allexport
fi

MODELS_HOST="${BABA_MODELS_HOST:-$PWD/models}"
mkdir -p "$MODELS_HOST"

echo "downloading models into ${MODELS_HOST}"

docker run --rm \
    -v "${MODELS_HOST}:/models" \
    -v "$PWD/tools:/tools:ro" \
    python:3.14-slim \
    sh -c "pip install --quiet huggingface_hub onnx numpy && python /tools/download_models.py --models-dir /models $*"
