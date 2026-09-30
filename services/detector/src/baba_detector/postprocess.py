"""Detection model postprocessing.

DETR-family detectors (RT-DETR, RT-DETRv2, D-FINE, RF-DETR) output a fixed
set of queries (typically 300) and require no NMS — each query produces a
unique object proposal.

For the HuggingFace-formatted ONNX exports we use (onnx-community/*), the
output signature is:
  - logits     : (B, num_queries, num_classes) — raw, pre-sigmoid
  - pred_boxes : (B, num_queries, 4)           — cx, cy, w, h normalized [0..1]

Postprocessing reduces to:
  1. sigmoid(logits) → per-class scores; argmax → class_id, max → confidence
  2. Filter by confidence threshold
  3. cxcywh → xyxy
  4. Map boxes from normalized [0..1] back to original image coords (undo
     letterbox padding and scale)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from baba_core import COCO_CLASSES, BoundingBox, Detection


@dataclass(slots=True, frozen=True)
class PostprocessContext:
    """Per-frame info needed to map model output back to image coords."""

    original_width: int
    original_height: int
    letterbox_scale: float
    letterbox_pad: tuple[int, int]  # (pad_x, pad_y) in input pixel space
    input_size: int


class Postprocessor(ABC):
    @abstractmethod
    def decode(
        self,
        raw_output: np.ndarray | list[np.ndarray],
        ctx: PostprocessContext,
        conf_threshold: float,
        class_names: list[str],
    ) -> list[Detection]: ...


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


class HFDetrPostprocessor(Postprocessor):
    """For HuggingFace ONNX exports of RT-DETR / RT-DETRv2 / D-FINE / RF-DETR.

    Expected outputs (in order):
      logits     : (num_queries, num_classes)
      pred_boxes : (num_queries, 4) — cxcywh in [0, 1] image-relative coords
    """

    def decode(
        self,
        raw_output: np.ndarray | list[np.ndarray],
        ctx: PostprocessContext,
        conf_threshold: float,
        class_names: list[str],
    ) -> list[Detection]:
        if not isinstance(raw_output, list) or len(raw_output) != 2:
            raise ValueError(
                f"HFDetrPostprocessor expects 2 outputs (logits, pred_boxes), got "
                f"{type(raw_output).__name__} len={len(raw_output) if hasattr(raw_output, '__len__') else 'N/A'}"
            )
        logits, boxes = raw_output

        # Squeeze batch dim if caller passed (1, Q, C)
        if logits.ndim == 3 and logits.shape[0] == 1:
            logits = logits[0]
            boxes = boxes[0]

        # logits → per-query best class + score
        scores_all = _sigmoid(logits)  # (Q, C)
        class_ids = np.argmax(scores_all, axis=1)  # (Q,)
        scores = scores_all[np.arange(len(class_ids)), class_ids]

        # Filter
        keep = scores >= conf_threshold
        if not np.any(keep):
            return []
        class_ids = class_ids[keep]
        scores = scores[keep]
        boxes = boxes[keep]

        # cxcywh (normalized) -> xyxy (input pixel space, then letterbox-undo)
        cx, cy, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - w / 2.0) * ctx.input_size
        y1 = (cy - h / 2.0) * ctx.input_size
        x2 = (cx + w / 2.0) * ctx.input_size
        y2 = (cy + h / 2.0) * ctx.input_size

        # Undo letterbox: subtract padding, then divide by scale
        pad_x, pad_y = ctx.letterbox_pad
        x1 = (x1 - pad_x) / ctx.letterbox_scale
        x2 = (x2 - pad_x) / ctx.letterbox_scale
        y1 = (y1 - pad_y) / ctx.letterbox_scale
        y2 = (y2 - pad_y) / ctx.letterbox_scale

        # Clip to image bounds
        x1 = np.clip(x1, 0, ctx.original_width)
        x2 = np.clip(x2, 0, ctx.original_width)
        y1 = np.clip(y1, 0, ctx.original_height)
        y2 = np.clip(y2, 0, ctx.original_height)

        # Reject oversized-bbox hallucinations. RT-DETRv2 on wide-aspect
        # inputs (Patio 2.2:1, West 3.6:1) emits phantom queries that
        # span large parts of the padded canvas; after letterbox-undo
        # they land as bboxes covering 40%+ of the frame area, often
        # tall vertical strips (e.g. w_frac=0.55, h_frac=0.98) that the
        # naive "full frame both axes" test misses. Measured rates pre-
        # filter: Patio 66%, West 51%, narrower cameras 0%. No plausible
        # surveillance target (person, car, pet) legitimately covers
        # more than ~40% of the frame area — even a close-range doorbell
        # subject typically stays under 30%.
        w_box = x2 - x1
        h_box = y2 - y1
        w_frac = w_box / max(1, ctx.original_width)
        h_frac = h_box / max(1, ctx.original_height)
        area_frac = (w_box * h_box) / max(1, ctx.original_width * ctx.original_height)
        keep_size = ~((area_frac > 0.40) | ((w_frac > 0.85) & (h_frac > 0.75)))
        if not np.all(keep_size):
            x1 = x1[keep_size]
            y1 = y1[keep_size]
            x2 = x2[keep_size]
            y2 = y2[keep_size]
            class_ids = class_ids[keep_size]
            scores = scores[keep_size]
        if x1.size == 0:
            return []

        detections: list[Detection] = []
        for xa, ya, xb, yb, cid, sc in zip(
            x1.tolist(),
            y1.tolist(),
            x2.tolist(),
            y2.tolist(),
            class_ids.tolist(),
            scores.tolist(),
            strict=True,
        ):
            cid_i = int(cid)
            name = class_names[cid_i] if 0 <= cid_i < len(class_names) else f"class_{cid_i}"
            detections.append(
                Detection(
                    bbox=BoundingBox(x1=float(xa), y1=float(ya), x2=float(xb), y2=float(yb)),
                    class_id=cid_i,
                    class_name=name,
                    confidence=float(sc),
                )
            )
        return detections


# The 80 COCO category ids that RF-DETR's 91-wide logit head actually uses,
# in the SAME order as baba_core's 80-class COCO_CLASSES. RF-DETR (LWDETR)
# emits the 91-slot COCO-paper scheme (person=1, car=3, …, with 11 unused
# "N/A" slots + a background slot 0); D-FINE/RT-DETR emit the 80-contiguous
# scheme directly. Gathering these columns turns a 91-logit tensor into an
# 80-logit tensor whose argmax is already a valid 80-class id — so the whole
# HF decode (sigmoid → argmax → box → letterbox-undo → size-gate) is reused
# verbatim, and the 91→80 remap can never drift (car@91=3 → car@80=2, etc.).
_RFDETR_91_TO_80_COLS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 27, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42,
    43, 44, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61,
    62, 63, 64, 65, 67, 70, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 84,
    85, 86, 87, 88, 89, 90,
)


class RFDetrPostprocessor(Postprocessor):
    """For RF-DETR (Roboflow, LWDETR) ONNX exports, fed STRETCHED input.

    Three things differ from the HF D-FINE/RT-DETR path:
      1. Output ORDER is (dets, labels) — boxes first, logits second — the
         reverse of HF's (logits, pred_boxes).
      2. Logits are the 91-slot COCO-paper scheme; we gather the 80 real
         columns (`_RFDETR_91_TO_80_COLS`) so the argmax is a valid 80-class id
         (car@91=3 → car@80=2), never a 91-scheme leak.
      3. The frame is fed via a square STRETCH (stretch_nv12), not a letterbox,
         because RF-DETR's DINOv2 backbone is trained on square resize and a
         grey pad is out-of-distribution. A stretch is a linear full-frame map,
         so a normalized box maps back with the original dims directly — there
         is no pad to subtract and no isotropic scale to divide (the HF
         letterbox-undo would be wrong here). Hence a self-contained decode.
    """

    def __init__(self) -> None:
        self._cols = np.asarray(_RFDETR_91_TO_80_COLS, dtype=np.intp)

    def decode(
        self,
        raw_output: np.ndarray | list[np.ndarray],
        ctx: PostprocessContext,
        conf_threshold: float,
        class_names: list[str],
    ) -> list[Detection]:
        if not isinstance(raw_output, list) or len(raw_output) != 2:
            raise ValueError(
                f"RFDetrPostprocessor expects 2 outputs (dets, labels), got "
                f"{type(raw_output).__name__} "
                f"len={len(raw_output) if hasattr(raw_output, '__len__') else 'N/A'}"
            )
        boxes, logits91 = raw_output  # RF-DETR ONNX order: dets, then labels
        if logits91.ndim == 3 and logits91.shape[0] == 1:
            logits91 = logits91[0]
            boxes = boxes[0]

        scores_all = _sigmoid(logits91[:, self._cols])  # (Q, 80)
        class_ids = np.argmax(scores_all, axis=1)
        scores = scores_all[np.arange(len(class_ids)), class_ids]
        keep = scores >= conf_threshold
        if not np.any(keep):
            return []
        class_ids = class_ids[keep]
        scores = scores[keep]
        boxes = boxes[keep]

        # STRETCH-undo: normalized cxcywh maps LINEARLY to the original frame
        # (or tile) — multiply by original dims, no pad, no isotropic scale.
        W = ctx.original_width
        H = ctx.original_height
        cx, cy, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = np.clip((cx - w / 2.0) * W, 0, W)
        x2 = np.clip((cx + w / 2.0) * W, 0, W)
        y1 = np.clip((cy - h / 2.0) * H, 0, H)
        y2 = np.clip((cy + h / 2.0) * H, 0, H)

        # Same oversized-hallucination gate as the HF path (wide-aspect phantom
        # queries spanning the canvas): drop >40% area or a full tall strip.
        w_box = x2 - x1
        h_box = y2 - y1
        w_frac = w_box / max(1, W)
        h_frac = h_box / max(1, H)
        area_frac = (w_box * h_box) / max(1, W * H)
        keep_size = ~((area_frac > 0.40) | ((w_frac > 0.85) & (h_frac > 0.75)))
        if not np.all(keep_size):
            x1, y1, x2, y2 = x1[keep_size], y1[keep_size], x2[keep_size], y2[keep_size]
            class_ids = class_ids[keep_size]
            scores = scores[keep_size]
        if x1.size == 0:
            return []

        detections: list[Detection] = []
        for xa, ya, xb, yb, cid, sc in zip(
            x1.tolist(), y1.tolist(), x2.tolist(), y2.tolist(),
            class_ids.tolist(), scores.tolist(), strict=True,
        ):
            cid_i = int(cid)
            name = class_names[cid_i] if 0 <= cid_i < len(class_names) else f"class_{cid_i}"
            detections.append(
                Detection(
                    bbox=BoundingBox(x1=float(xa), y1=float(ya), x2=float(xb), y2=float(yb)),
                    class_id=cid_i,
                    class_name=name,
                    confidence=float(sc),
                )
            )
        return detections


# COCO 80-class schema now lives in baba_core.classes (single source of truth,
# shared with the api's detection-rules catalogue). Re-exported here so
# existing `from baba_detector.postprocess import COCO_CLASSES` keeps working.
__all__ = [
    "COCO_CLASSES",
    "HFDetrPostprocessor",
    "PostprocessContext",
    "RFDetrPostprocessor",
]
