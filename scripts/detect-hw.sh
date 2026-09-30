#!/usr/bin/env bash
# Detect the host GPU and write the matching BABA_VARIANT value into .env.
#
# Runs without docker — it only needs the kernel-level surfaces that
# vendor drivers expose on the host (nvidia-smi, /dev/dri, lspci). The
# value it writes drives `BABA_VARIANT` in docker-compose.yml — which
# image stage gets built for every BABA service, and therefore which
# decoder/inference plugins ship inside the containers.
#
# Usage:
#   scripts/detect-hw.sh             # write to ./.env
#   scripts/detect-hw.sh /path/.env  # write to a specific file
#   scripts/detect-hw.sh --dry-run   # print what would be written

set -euo pipefail

ENV_FILE="${1:-.env}"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

detect_variant() {
    # 1) NVIDIA — has nvidia-smi and at least one GPU we can talk to.
    if command -v nvidia-smi >/dev/null 2>&1; then
        if nvidia-smi -L >/dev/null 2>&1; then
            echo "nvidia"
            return
        fi
    fi

    # 2) Intel Arc — discrete GPU shows up as a Display controller from
    #    "Intel" with an Arc-family device ID, and a render node under
    #    /dev/dri. We don't tag every iGPU here because QSV plugin is
    #    primarily for the Arc decode engine; older iGPUs run fine on the
    #    software decoder.
    if command -v lspci >/dev/null 2>&1; then
        if lspci -nn 2>/dev/null | grep -iE "display controller|3d controller|vga compatible controller" \
            | grep -qiE "intel.*(arc|battlemage|alchemist|xe)"; then
            if [[ -d /dev/dri ]]; then
                echo "intel"
                return
            fi
        fi
    fi

    # 3) AMD ROCm — TODO. We don't ship an AMD plugin yet; falling through
    #    to cpu is honest until we have something to call.

    echo "cpu"
}

VARIANT="$(detect_variant)"
echo "Detected hardware variant: $VARIANT"

if [[ "$VARIANT" == "nvidia" ]]; then
    DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
    NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
    echo "  GPU: $NAME"
    echo "  Driver: $DRIVER"
elif [[ "$VARIANT" == "intel" ]]; then
    NAME=$(lspci -nn 2>/dev/null | grep -iE "intel.*(arc|xe)" | head -1 || echo "Intel GPU")
    echo "  GPU: $NAME"
fi

if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- would write to $ENV_FILE ---"
    echo "BABA_VARIANT=$VARIANT"
    exit 0
fi

touch "$ENV_FILE"
# Remove any pre-existing BABA_VARIANT (and stale per-service overrides
# from the old multi-key layout) before appending the new value. Done
# with a temp file so we don't depend on sed -i behaviour differences
# across BSD/GNU sed.
grep -vE "^(BABA_VARIANT|BABA_(DETECTOR|INGESTOR|EMBEDDER|API|TRACKER|RECORDER|EVENT_MANAGER)_VARIANT)=" \
    "$ENV_FILE" > "$ENV_FILE.tmp" || true
echo "BABA_VARIANT=$VARIANT" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"

echo "Wrote BABA_VARIANT=$VARIANT to $ENV_FILE"
