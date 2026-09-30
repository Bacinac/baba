#!/usr/bin/env python3
"""Blur a camera clip frame-by-frame with the SAME pipeline as the stills:
per-frame rtdetrv2 person/vehicle detection + the camera's fixed regions
(rect or curb polygon), mask-composited over a heavy blur. Writes blurred JPG
frames; ffmpeg reassembles them into a fragmented MP4.

    python video_blur.py <slug> <in.mp4> <out-frames-dir>
"""
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
THRESH = {"person": 0.25, "bicycle": 0.25, "motorcycle": 0.30, "cat": 0.30, "dog": 0.30,
          "car": 0.55, "truck": 0.55, "bus": 0.55}
FIXED = json.loads(Path("/work/regions.json").read_text()) if os.path.exists("/work/regions.json") else {}

slug, inp, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(outdir, exist_ok=True)
regions = FIXED.get(slug, [])
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


def blur_frame(img):
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for d in detect(img):
        if d.class_name not in BLUR or d.confidence < THRESH.get(d.class_name, 0.5):
            continue
        b = d.bbox
        bw, bh = b.x2 - b.x1, b.y2 - b.y1
        cv2.rectangle(mask, (max(0, int(b.x1 - .15 * bw)), max(0, int(b.y1 - .15 * bh))),
                      (min(w, int(b.x2 + .15 * bw)), min(h, int(b.y2 + .15 * bh))), 255, -1)
    for region in regions:
        if isinstance(region[0], (int, float)):
            x1, y1, x2, y2 = region
            cv2.rectangle(mask, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), 255, -1)
        else:
            cv2.fillPoly(mask, [np.array([[int(px * w), int(py * h)] for px, py in region], np.int32)], 255)
    if not mask.any():
        return img
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(4, w // 120))
    m3 = (mask.astype(np.float32) / 255.0)[..., None]
    k = max(21, (w // 24) | 1)
    bl = cv2.GaussianBlur(cv2.GaussianBlur(img, (k, k), 0), (k, k), 0)
    return (bl * m3 + img * (1 - m3)).astype(np.uint8)


cap = cv2.VideoCapture(inp)
i = 0
while True:
    ok, frame = cap.read()
    if not ok:
        break
    cv2.imwrite(f"{outdir}/{i:05d}.jpg", blur_frame(frame), [cv2.IMWRITE_JPEG_QUALITY, 88])
    i += 1
cap.release()
print(f"{slug}: {i} frames blurred")
