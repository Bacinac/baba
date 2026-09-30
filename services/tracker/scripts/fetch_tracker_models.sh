#!/usr/bin/env bash
# Pulls the OSNet x0.25 ReID weights for the tracker into BABA_MODELS_HOST.
#
# OSNet (Omni-Scale Network, Zhou et al. 2019) is the de-facto lightweight
# person ReID model used by DeepSORT / StrongSORT / BoT-SORT-Lite. The
# x0.25 variant is ~1 MB ONNX, ~250k params, runs sub-2 ms per crop on
# CPU and ~0.3 ms on a modern GPU — small enough to live inline with the
# tracker without GPU contention. msmt17 fine-tune gives better
# generalisation across surveillance distances than the imagenet-only
# pre-train.
#
# Original implementation by Kaiyang Zhou is MIT-licensed
# (https://github.com/KaiyangZhou/deep-person-reid). The ONNX export
# mirrored here on Hugging Face is the same weights, MIT, freely
# redistributable in a commercial BABA image.
#
# Output:
#   ${BABA_MODELS_HOST}/osnet_x0_25_msmt17.onnx  (~1 MB, MIT)
set -euo pipefail

MODELS_HOST="${BABA_MODELS_HOST:-}"
if [[ -z "$MODELS_HOST" ]]; then
    if [[ -f .env ]]; then
        # shellcheck disable=SC2046
        export $(grep -E '^BABA_MODELS_HOST=' .env | xargs)
        MODELS_HOST="${BABA_MODELS_HOST:-}"
    fi
fi
if [[ -z "$MODELS_HOST" ]]; then
    echo "error: BABA_MODELS_HOST not set" >&2
    exit 1
fi
mkdir -p "$MODELS_HOST"

OSNET_OUT="$MODELS_HOST/osnet_x0_25_msmt17.onnx"
if [[ ! -f "$OSNET_OUT" ]]; then
    echo "downloading OSNet x0.25 (msmt17) → $OSNET_OUT"
    curl -fL -o "$OSNET_OUT" \
        "https://huggingface.co/anriha/osnet_x0_25_msmt17/resolve/main/osnet_x0_25_msmt17.onnx"
    echo "done: $(du -h "$OSNET_OUT" | cut -f1)"
else
    echo "osnet already present: $OSNET_OUT"
fi

echo "tracker model ready. Restart tracker to pick it up."
