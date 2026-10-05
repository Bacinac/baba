#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${BABA_DEMO_WORK:?BABA_DEMO_WORK is required}"
IMAGE="${BABA_DEMO_PYTHON_IMAGE:-}"
if [[ -z "$IMAGE" ]]; then
    IMAGE=$(docker build -q -f "$ROOT/docker/demo-python.Dockerfile" "$ROOT")
fi
mounts=(-v "$ROOT:$ROOT:ro" -v "$WORK:$WORK")
for key in BABA_DEMO_INPUT BABA_DEMO_STILLS; do
    if [[ -n "${!key:-}" ]]; then
        mounts+=(-v "${!key}:${!key}:ro")
    fi
done
if [[ -n "${BABA_DEMO_OUTPUT:-}" ]]; then
    mounts+=(-v "$BABA_DEMO_OUTPUT:$BABA_DEMO_OUTPUT")
fi
docker run --rm -i --network none --read-only --tmpfs /tmp \
    --user "$(id -u):$(id -g)" "${mounts[@]}" -w "$ROOT" "$IMAGE" "$@"
