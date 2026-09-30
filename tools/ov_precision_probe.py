"""Isolate OpenVINO GPU precision bugs on transformer detectors (RT-DETR / D-FINE).

Why this exists
---------------
On OV 2026.2 / Arc A380 the GPU plugin's fp16 path is unusable for these
detectors, in two independent ways:

  1. RT-DETR (r18) fp16 with a *dynamic* batch fails to COMPILE (MatMul dim check).
  2. D-FINE fp16 (static batch) COMPILES but the GPU program corrupts after the
     FIRST inference: call 0 is correct, every later call returns collapsed
     logits (sigmoid max ~0.016 -> zero detections). NOT a warmup or infer-request
     artefact — a fresh request per call collapses identically. f32 is bit-stable.

That second failure is silent (the model loads, "works" once, then serves
garbage), so this probe exists to make it reproducible. The fix lives in
backends/openvino: the detector always compiles f32/ACCURACY on the GPU.

Usage (inside the detector container, which has openvino + baba packages):
    python tools/ov_precision_probe.py /models/d-fine-m.onnx
    python tools/ov_precision_probe.py /models/d-fine-m.onnx --camera west

With --camera it pulls a real frame off that camera's SHM ring; otherwise it
uses a deterministic synthetic gradient (enough to exercise the collapse).
"""
from __future__ import annotations

import argparse

import numpy as np
import openvino as ov

TARGET = 640


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def real_frame(camera: str) -> np.ndarray:
    import cv2
    from baba_core.frame_ring import FrameRingReader

    rf = FrameRingReader(camera).get_latest()
    if rf is None:
        raise SystemExit(f"no frame in ring for camera {camera!r}")
    rgb = cv2.cvtColor(rf.pixels, cv2.COLOR_YUV2RGB_NV12) if rf.pixel_format == "nv12" else rf.pixels
    h, w = rgb.shape[:2]
    s = min(TARGET / h, TARGET / w)
    nw, nh = round(w * s), round(h * s)
    canvas = np.full((TARGET, TARGET, 3), 114, np.uint8)
    canvas[:nh, :nw] = cv2.resize(rgb, (nw, nh))
    return np.ascontiguousarray((canvas.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def synthetic() -> np.ndarray:
    g = np.linspace(0.0, 1.0, TARGET, dtype=np.float32)
    plane = np.outer(g, g)
    return np.ascontiguousarray(np.stack([plane, plane[::-1], plane.T])[None])


def compile_model(model_path: str, precision: str):
    core = ov.Core()
    m = core.read_model(model_path)
    m.reshape({m.inputs[0].get_any_name(): [1, 3, TARGET, TARGET]})
    hint = {"PERFORMANCE_HINT": "LATENCY"}
    hint["EXECUTION_MODE_HINT" if precision == "f32-accuracy" else "INFERENCE_PRECISION_HINT"] = (
        "ACCURACY" if precision == "f32-accuracy" else precision
    )
    return core.compile_model(m, "GPU", hint)


def run(model_path: str, camera: str | None) -> None:
    tensor = real_frame(camera) if camera else synthetic()
    print(f"input: shape={tensor.shape} mean={tensor.mean():.3f} source={'camera ' + camera if camera else 'synthetic'}")
    for precision in ("f16", "f32-accuracy"):
        cm = compile_model(model_path, precision)
        maxes = []
        for _ in range(4):
            r = cm(tensor)
            maxes.append(float(sigmoid(np.asarray(r[cm.outputs[0]])[0]).max()))
        verdict = "STABLE" if max(maxes) - min(maxes) < 0.05 else "COLLAPSE after call 0"
        print(f"  {precision:14s} sigmoid-max per call = {[f'{v:.3f}' for v in maxes]}  -> {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="path to detector .onnx (e.g. /models/d-fine-m.onnx)")
    ap.add_argument("--camera", default=None, help="camera slug to pull a real SHM frame from")
    args = ap.parse_args()
    run(args.model, args.camera)
