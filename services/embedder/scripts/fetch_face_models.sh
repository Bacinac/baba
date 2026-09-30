#!/usr/bin/env bash
# Pulls the face recognition stack ONNX weights into BABA_MODELS_HOST.
# Both picks are commercially clean — see ../../core/src/baba_core/face.py
# for the license audit notes.
#
# Output:
#   ${BABA_MODELS_HOST}/face_yunet.onnx      (~1 MB, MIT, opencv-bundled)
#   ${BABA_MODELS_HOST}/face_auraface.onnx   (~261 MB, Apache 2.0, fal)
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

# Weights are pinned to an exact HF commit (not a mutable branch) and verified
# by SHA256 after download. `/models` is mounted read-only into the services, so
# these files are trusted inputs: an unpinned `resolve/main` could serve a
# different tensor tomorrow, and no checksum meant a truncated or swapped
# download loaded silently. A mismatch aborts loud — the file is removed so a
# rerun re-fetches rather than trusting a bad blob.
#
# To bump a model: change its revision + sha256 together (get the sha from
# `sha256sum` of the freshly downloaded file), never one without the other.
fetch_verified() {  # <out_path> <url> <sha256> <human_label>
    local out="$1" url="$2" want="$3" label="$4" got
    if [[ -f "$out" ]]; then
        got=$(sha256sum "$out" | cut -d' ' -f1)
        if [[ "$got" == "$want" ]]; then
            echo "$label already present and verified: $out"
            return 0
        fi
        echo "warning: $out present but sha256 mismatch — re-downloading" >&2
    fi
    echo "downloading $label → $out"
    curl -fL -o "$out" "$url"
    got=$(sha256sum "$out" | cut -d' ' -f1)
    if [[ "$got" != "$want" ]]; then
        rm -f "$out"
        echo "error: $label sha256 mismatch (want $want, got $got) — removed" >&2
        exit 1
    fi
    echo "done: $(du -h "$out" | cut -f1), sha256 verified"
}

# YuNet — face detector. opencv's HF mirror, 2023mar weights. Tiny (~1 MB),
# MIT, gives 5 landmarks.
fetch_verified \
    "$MODELS_HOST/face_yunet.onnx" \
    "https://huggingface.co/opencv/face_detection_yunet/resolve/3cc26e7f1014a5ee5d74a42acee58bafc9d0a310/face_detection_yunet_2023mar.onnx" \
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4" \
    "YuNet"

# AuraFace v1 — ArcFace ResNet100 retrained on commercial-clean data by fal.
# This is the *only* widely-distributed face embedder with an explicit
# Apache 2.0 license on the WEIGHTS (not just the code). InsightFace's
# buffalo_l / glintr100 / antelopev2 etc. all carry research-only
# constraints inherited from MS1M / Glint / WebFace training data — we
# can't ship those in a commercial BABA image.
fetch_verified \
    "$MODELS_HOST/face_auraface.onnx" \
    "https://huggingface.co/fal/AuraFace-v1/resolve/af6d057c9b0ec4071d4c49c80e3539258798b609/glintr100.onnx" \
    "a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60" \
    "AuraFace v1 (~261 MB)"

echo "face models ready. Restart api + embedder to pick them up."
