#!/usr/bin/env bash
# Downloads facebook/dinov2-small from HuggingFace and exports it to ONNX
# in the models directory ($BABA_MODELS_HOST on the host, /models inside
# containers). Run this once after a fresh checkout; the embedder falls
# back to a stub backend until the file is in place.
#
# Why a one-shot script instead of build-time bake-in: the export pulls
# ~700 MB of pytorch weights and ~2 GB of transient state. Doing this at
# build time would bloat the embedder image and slow CI/CD. Keeping it
# external means the runtime image stays slim (onnxruntime only).
#
# Output: ${BABA_MODELS_HOST}/dinov2-vits14.onnx (~85 MB)
#
# Note: ONNX is exported with `dynamo=False` so weights are inlined into
# a single .onnx file rather than externalized to a sibling .onnx.data.
# Onnxruntime handles both, but a single file is simpler to ship/copy.
set -euo pipefail

MODELS_HOST="${BABA_MODELS_HOST:-}"
if [[ -z "$MODELS_HOST" ]]; then
    # Try to pull from .env if we're run from the repo root.
    if [[ -f .env ]]; then
        # shellcheck disable=SC2046
        export $(grep -E '^BABA_MODELS_HOST=' .env | xargs)
        MODELS_HOST="${BABA_MODELS_HOST:-}"
    fi
fi
if [[ -z "$MODELS_HOST" ]]; then
    echo "error: BABA_MODELS_HOST not set (export it or run from repo root with .env)" >&2
    exit 1
fi

mkdir -p "$MODELS_HOST"

OUT_FILE="$MODELS_HOST/dinov2-vits14.onnx"
if [[ -f "$OUT_FILE" ]]; then
    echo "already present: $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
    echo "delete it first if you want to re-export"
    exit 0
fi

echo "exporting facebook/dinov2-small → $OUT_FILE"
echo "(first run will download ~700 MB of pytorch weights; allow ~5 min)"

# Run the export inside a one-shot python:3.14 container so the host
# doesn't need pytorch / transformers / optimum installed.
docker run --rm \
    -v "$MODELS_HOST":/out \
    -v "${HOME}/.cache/huggingface":/root/.cache/huggingface \
    python:3.14-slim bash -c '
set -ex
# CPU-only torch is plenty for a one-shot export; cuda wheels just
# inflate the install.
pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch
pip install --no-cache-dir "transformers" "onnx" "onnxscript"

# Direct torch.onnx.export instead of optimum-cli: optimum 2.x removed
# the `export onnx` subcommand and the replacement is in flux. Going
# straight through transformers + torch keeps this script independent
# of optimum versioning drift.
#
# Output contract (verified by inspecting the exported graph):
#   input  pixel_values: float32 [batch, 3, 224, 224]
#   output last_hidden_state: float32 [batch, 257, 384]
#          (257 = 1 CLS token + 16x16 patch tokens; backend takes index 0)
python - <<PY
import torch
from transformers import AutoModel

model = AutoModel.from_pretrained("facebook/dinov2-small").eval()
dummy = torch.zeros(1, 3, 224, 224, dtype=torch.float32)
torch.onnx.export(
    model,
    (dummy,),
    "/out/dinov2-vits14.onnx",
    input_names=["pixel_values"],
    output_names=["last_hidden_state"],
    dynamic_axes={
        "pixel_values": {0: "batch", 2: "height", 3: "width"},
        "last_hidden_state": {0: "batch"},
    },
    opset_version=17,
    do_constant_folding=True,
    # Force the classic tracer (not the dynamo exporter) so weights are
    # inlined and we end up with a single .onnx file instead of a
    # .onnx + .onnx.data pair.
    dynamo=False,
)
print("exported /out/dinov2-vits14.onnx")
PY
'

echo "done: $(du -h "$OUT_FILE" | cut -f1) → $OUT_FILE"
echo "restart the embedder to pick it up:  docker compose restart embedder"
