#!/usr/bin/env python3
"""BABA demo blur — v2. Reuses BABA's rtdetrv2 detector, then blurs the UNION of
detected person/vehicle regions (+ optional per-camera fixed regions) as one
feathered mask over a heavily-blurred copy. Clean, uniform, no per-box patchwork.
"""
import glob
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, "/baba/core/src")
sys.path.insert(0, "/baba/services/detector/src")
from baba_core import COCO_CLASSES
from baba_detector.postprocess import HFDetrPostprocessor, PostprocessContext
from baba_detector.preprocess import letterbox

MODEL, INPUT, CONF = "/models/rtdetrv2-r18.onnx", 640, 0.10
BLUR = {"person", "bicycle", "car", "motorcycle", "bus", "truck", "cat", "dog"}
# Prag po klasi: ljude/bicikle bluramo osjetljivo (recall), a vozila samo na
# VISOK confidence — inace detektor citAa zidove/sjene kao "auto" (0.1-0.3) i
# zamuti vlastitu zgradu. Pravi auto s registracijom je uvijek >0.5.
THRESH = {"person": 0.25, "bicycle": 0.25, "motorcycle": 0.30,
          "cat": 0.30, "dog": 0.30, "car": 0.55, "truck": 0.55, "bus": 0.55}
FIXED = json.loads(Path("/work/regions.json").read_text()) if os.path.exists("/work/regions.json") else {}

sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
inname = sess.get_inputs()[0].name
post = HFDetrPostprocessor()


def detect(bgr):
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    padded, scale, pad = letterbox(rgb, INPUT)
    ten = np.ascontiguousarray((padded.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    out = sess.run(None, {inname: ten})
    bi = next(i for i, a in enumerate(out) if a.shape[-1] == 4)
    li = next(i for i in range(len(out)) if i != bi)
    ctx = PostprocessContext(original_width=bgr.shape[1], original_height=bgr.shape[0],
                             letterbox_scale=scale, letterbox_pad=pad, input_size=INPUT)
    return post.decode([out[li], out[bi]], ctx, conf_threshold=CONF, class_names=COCO_CLASSES)


def paint(mask, regions, value):
    h, w = mask.shape
    for region in regions:
        if isinstance(region[0], (int, float)):    # pravokutnik [x1,y1,x2,y2]
            x1, y1, x2, y2 = region
            cv2.rectangle(mask, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), value, -1)
        else:                                       # poligon [[x,y],...] — prati rubnjak
            pts = np.array([[int(px * w), int(py * h)] for px, py in region], np.int32)
            cv2.fillPoly(mask, [pts], value)


for path in sorted(glob.glob("/work/*.jpg")):
    name = os.path.basename(path)
    if name.startswith("blurred_"):
        continue
    img = cv2.imread(path)
    if img is None:
        print(f"  {name}: unreadable")
        continue
    h, w = img.shape[:2]
    key = name.replace(".jpg", "")
    mask = np.zeros((h, w), np.uint8)
    dets = [d for d in detect(img) if d.class_name in BLUR and d.confidence >= THRESH.get(d.class_name, 0.5)]
    for d in dets:
        b = d.bbox
        bw, bh = b.x2 - b.x1, b.y2 - b.y1
        x1, y1 = max(0, int(b.x1 - 0.15 * bw)), max(0, int(b.y1 - 0.15 * bh))
        x2, y2 = min(w, int(b.x2 + 0.15 * bw)), min(h, int(b.y2 + 0.15 * bh))
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
    # Per-camera fixed regions. Either a bare list of blur regions, or a dict
    # {"blur": [...], "clear": [...]} — `clear` polygons are SUBTRACTED from the
    # mask after everything else, so a foreground structure we own (e.g. our own
    # gate) can stay sharp inside an otherwise-blurred band. `clear` wins over
    # both fixed blur AND detections, so only ever draw a clear over pixels you
    # are sure carry no third party (this frame is a hand-reviewed still).
    spec = FIXED.get(key, [])
    blur_regions = spec.get("blur", []) if isinstance(spec, dict) else spec
    clear_regions = spec.get("clear", []) if isinstance(spec, dict) else []

    paint(mask, blur_regions, 255)
    paint(mask, clear_regions, 0)

    if mask.any():
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(4, w // 120))
        m3 = (mask.astype(np.float32) / 255.0)[..., None]
        k = max(21, (w // 24) | 1)
        blurred = cv2.GaussianBlur(img, (k, k), 0)
        blurred = cv2.GaussianBlur(blurred, (k, k), 0)
        img = (blurred * m3 + img * (1 - m3)).astype(np.uint8)
    cv2.imwrite(f"/work/blurred_{name}", img)
    nf, nc = len(blur_regions), len(clear_regions)
    print(f"  {name}: zamuceno {len(dets)} objekata"
          + (f" + {nf} fiksnih" if nf else "") + (f" - {nc} clear" if nc else ""))
