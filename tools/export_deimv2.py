"""Export DEIMv2 checkpoints (Intellindust HF safetensors) to ONNX with the
BABA detector contract: input `pixel_values` [B,3,640,640] f32 in [0,1],
outputs `logits` [B,Q,80] + `pred_boxes` [B,Q,4] (cxcywh, normalized) — the
same convention as the HF D-FINE exports, so DETRPostprocessor decodes them
as-is and the OpenVINO NV12-PPP wrapper applies unchanged (DEIMv2 eval
transforms are plain /255, no ImageNet mean/std).

Run inside a throwaway container (containers-only rule):

    docker run --rm -v $PWD:/work -w /work python:3.12-slim bash -c '
      apt-get update -qq && apt-get install -y -qq git &&
      git clone --depth 1 https://github.com/Intellindust-AI-Lab/DEIMv2 &&
      pip install -q torch --index-url https://download.pytorch.org/whl/cpu &&
      pip install -q onnx onnxruntime huggingface_hub safetensors pillow \
                     -r DEIMv2/requirements.txt &&
      python export_deimv2.py --image sample.jpg'

Parity gotcha (measured 2026-07-12): raw-logit torch-vs-ORT comparison on
RANDOM NOISE input diverges wildly (~1.0) — near-uniform encoder scores make
the top-k query selection order-unstable, so different queries get picked.
That is NOT an export bug: on a real image the detection sets match exactly
(logits maxdiff ~3e-4 for S). Always parity-check on a real image, comparing
thresholded detections.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "DEIMv2")

import numpy as np
import torch
import torch.nn as nn
from engine.core import YAMLConfig
from huggingface_hub import hf_hub_download
from PIL import Image
from safetensors.torch import load_file

VARIANTS = {
    "deimv2-s": (
        "DEIMv2/configs/deimv2/deimv2_dinov3_s_coco.yml",
        "Intellindust/DEIMv2_DINOv3_S_COCO",
    ),
    "deimv2-n": (
        "DEIMv2/configs/deimv2/deimv2_hgnetv2_n_coco.yml",
        "Intellindust/DEIMv2_HGNetv2_N_COCO",
    ),
    "deimv2-pico": (
        "DEIMv2/configs/deimv2/deimv2_hgnetv2_pico_coco.yml",
        "Intellindust/DEIMv2_HGNetv2_PICO_COCO",
    ),
}


class ExportWrapper(nn.Module):
    """Deploy model minus the bundled postprocessor — BABA does its own
    letterbox-aware decode in DETRPostprocessor."""

    def __init__(self, m: nn.Module) -> None:
        super().__init__()
        self.m = m

    def forward(self, pixel_values):
        out = self.m(pixel_values)
        if isinstance(out, dict):
            return out["pred_logits"], out["pred_boxes"]
        return out[0], out[1]


def letterbox_tensor(image_path: str, size: int = 640) -> torch.Tensor:
    img = Image.open(image_path).convert("RGB")
    iw, ih = img.size
    s = min(size / iw, size / ih)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(img.resize((round(iw * s), round(ih * s))), (0, 0))
    arr = np.asarray(canvas)
    return torch.from_numpy(arr).permute(2, 0, 1).float().div(255)[None]


def detections(logits, thr: float = 0.25):
    scores = torch.sigmoid(torch.as_tensor(logits))[0]
    conf, cls = scores.max(-1)
    keep = conf > thr
    return sorted(
        [(int(a), round(float(b), 3)) for a, b in zip(cls[keep], conf[keep], strict=True)],
        key=lambda t: -t[1],
    )[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="real image for the parity check")
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    x = letterbox_tensor(args.image)

    import onnxruntime as ort

    for name in args.variants:
        cfg_path, repo = VARIANTS[name]
        print(f"=== {name}")
        cfg = YAMLConfig(cfg_path)
        if "HGNetv2" in cfg.yaml_cfg:
            cfg.yaml_cfg["HGNetv2"]["pretrained"] = False
        model = cfg.model
        sd = load_file(hf_hub_download(repo, "model.safetensors"))
        # `decoder.up` / `decoder.reg_scale` are init-time buffers the HF
        # safetensors deliberately omit — strict=False is expected to report
        # exactly those two as missing.
        missing, unexpected = model.load_state_dict(sd, strict=False)
        print(f"state_dict: missing={len(missing)} unexpected={len(unexpected)}")
        wrapper = ExportWrapper(model.deploy().eval()).eval()

        out_file = f"{name}.onnx"
        torch.onnx.export(
            wrapper,
            (x,),
            out_file,
            input_names=["pixel_values"],
            output_names=["logits", "pred_boxes"],
            dynamic_axes={
                "pixel_values": {0: "batch"},
                "logits": {0: "batch"},
                "pred_boxes": {0: "batch"},
            },
            opset_version=args.opset,
            do_constant_folding=True,
            dynamo=False,
        )

        sess = ort.InferenceSession(out_file, providers=["CPUExecutionProvider"])
        ol, _ = sess.run(None, {"pixel_values": x.numpy()})
        with torch.no_grad():
            tl, _ = wrapper(x)
        d_torch, d_onnx = detections(tl), detections(ol)
        print(f"torch: {d_torch}")
        print(f"onnx : {d_onnx}")
        if d_torch != d_onnx:
            raise SystemExit(f"{name}: thresholded detections diverge — export is broken")
        print(f"SAVED {out_file}")


if __name__ == "__main__":
    main()
