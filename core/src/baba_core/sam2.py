"""SAM2 (Segment Anything 2) — promptable image segmentation.

Used by the api service to turn rough VLM-suggested zone polygons into
pixel-perfect masks. VLMs (Claude, GPT) have approximate spatial
reasoning — they identify *what* and *roughly where*, but their polygon
coords are typically 5–15% off the actual object edge. SAM2 takes the
rough region as a prompt and snaps it to real boundaries.

Standard Meta-style ONNX export (encoder + decoder split):
  encoder: image (1, 3, 1024, 1024) → image_embed + 2 high-res feature
           maps. Runs once per image (~150–200 ms on GPU).
  decoder: image_embed + high_res_feats + point_coords + point_labels
           → masks + iou_predictions. Runs once per prompt (~5–20 ms).

Coordinate convention: SAM2 ONNX inputs expect coords in the resized
1024×1024 space. We do letterbox-style resize (long edge = 1024, pad to
square with zeros) so we can map back unambiguously. Point/box prompts
get the same transform; output masks get the inverse transform when we
convert them back to the original snapshot dims.

Apache 2.0 licensed weights — safe for our commercial roadmap.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)

# SAM2's pretrained input resolution. Hard-coded in the ONNX export
# (the encoder's first dim is [1, 3, 1024, 1024] static), do not change.
SAM2_INPUT_SIZE = 1024

# SAM2 prompt point_labels convention (Meta reference):
#   1 = positive (foreground click)
#   0 = negative (background click)
#   2 = top-left of a bounding box
#   3 = bottom-right of a bounding box
#  -1 = padding (ignore)
_LABEL_POS = 1
_LABEL_BBOX_TL = 2
_LABEL_BBOX_BR = 3


class _Letterbox(NamedTuple):
    """Records the resize+pad parameters so we can map coords (input dir)
    and masks (output dir) between original-image space and 1024×1024."""

    orig_w: int
    orig_h: int
    scale: float  # multiplier from orig px to 1024-space px
    new_w: int  # width  after scale, before padding
    new_h: int  # height after scale, before padding


def _letterbox_image(rgb: np.ndarray) -> tuple[np.ndarray, _Letterbox]:
    """Resize RGB uint8 (H, W, 3) so the long edge = 1024, then zero-pad
    bottom/right to a square 1024×1024. Returns (padded_image, params)."""
    import cv2

    h, w = rgb.shape[:2]
    scale = SAM2_INPUT_SIZE / max(h, w)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    padded = np.zeros((SAM2_INPUT_SIZE, SAM2_INPUT_SIZE, 3), dtype=np.uint8)
    padded[:new_h, :new_w] = resized
    return padded, _Letterbox(w, h, scale, new_w, new_h)


def _normalize_for_encoder(padded_rgb: np.ndarray) -> np.ndarray:
    """SAM2 encoder expects float32 NCHW, ImageNet-normalized."""
    arr = padded_rgb.astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return np.ascontiguousarray(arr.transpose(2, 0, 1)[None, ...], dtype=np.float32)


class Sam2Predictor:
    """Stateful SAM2 wrapper: encode an image once, then run any number
    of prompt decodes against the cached embedding.

    Typical use (single image, several prompts):

        pred = Sam2Predictor(enc_path, dec_path)
        pred.set_image(rgb_uint8_HWC)
        for vlm_poly in vlm_polygons:
            mask = pred.predict_from_polygon(vlm_poly)  # bool (H, W)
            refined_poly = mask_to_polygon(mask)        # external helper
    """

    def __init__(
        self,
        encoder_path: Path,
        decoder_path: Path,
    ) -> None:
        from baba_core.onnx_session import make_session

        self._enc, self._enc_provider = make_session(
            encoder_path,
            intra_op_threads=4,
            name="sam2-enc",
        )
        self._dec, self._dec_provider = make_session(
            decoder_path,
            intra_op_threads=2,
            name="sam2-dec",
        )
        # Cached encoder outputs + letterbox params for the current image.
        self._image_embed: np.ndarray | None = None
        self._high_res_0: np.ndarray | None = None
        self._high_res_1: np.ndarray | None = None
        self._lb: _Letterbox | None = None
        log.info(
            "sam2 ready (encoder=%s, decoder=%s)",
            self._enc_provider,
            self._dec_provider,
        )

    def set_image(self, rgb: np.ndarray) -> None:
        """Encode the image. `rgb` is uint8 HWC, any size; we letterbox
        to 1024×1024 internally. Cache the embedding for subsequent
        prompt calls — the encoder is the expensive part (~150 ms)."""
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"sam2.set_image: expected uint8 HWC RGB, got {rgb.shape} {rgb.dtype}")
        padded, lb = _letterbox_image(rgb)
        x = _normalize_for_encoder(padded)
        out = self._enc.run(None, {"image": x})
        # Output order from the export: high_res_feats_0, high_res_feats_1, image_embed
        names = [o.name for o in self._enc.get_outputs()]
        by_name = dict(zip(names, out, strict=True))
        self._high_res_0 = by_name["high_res_feats_0"]
        self._high_res_1 = by_name["high_res_feats_1"]
        self._image_embed = by_name["image_embed"]
        self._lb = lb

    def _map_xy_to_input(self, xy: np.ndarray) -> np.ndarray:
        """Map original-image (px) coords to the 1024×1024 input space."""
        if self._lb is None:
            raise RuntimeError("sam2: call set_image() before predict_*()")
        return xy * self._lb.scale

    def predict_from_polygon(
        self,
        polygon_norm: list[list[float]] | np.ndarray,
        *,
        use_polygon_as_mask: bool = True,
        area_ratio_bounds: tuple[float, float] = (0.4, 2.5),
    ) -> np.ndarray | None:
        """Refine one VLM polygon into a pixel-perfect mask.

        Two distinct strategies depending on `use_polygon_as_mask`:

        - True (default, recommended for AREA-style zones like yards,
          driveways, restricted regions): rasterize the VLM polygon as
          a "previous mask" hypothesis and feed it via `mask_input`.
          SAM2 then refines the *edges* of THAT mask to actual image
          boundaries instead of re-segmenting from scratch. Critical
          for non-object regions — without it SAM2 falls back to
          object-segmentation mode and snaps to whatever small texture
          patch sits under the prompt point.

        - False: bbox + center-point prompt only. SAM2 segments the
          dominant object inside the bbox. Right for OBJECT-style
          zones (gates, doors, parking spots) where the VLM polygon is
          a rough container around a real object.

        `area_ratio_bounds` (lo, hi) rejects the result if the refined
        mask's area is < lo× or > hi× the input polygon's area. SAM2
        sometimes hallucinates tiny specks or balloons out to the
        whole frame; rejecting those keeps the user's mental model
        ("refinement, not redefinition") intact.

        Returns a boolean (orig_h, orig_w) mask, or None on rejection
        / SAM2 failure. The caller should fall back to the VLM polygon.
        """
        if self._image_embed is None or self._lb is None:
            raise RuntimeError("sam2: call set_image() before predict_*()")
        poly = np.asarray(polygon_norm, dtype=np.float32)
        if poly.ndim != 2 or poly.shape[1] != 2 or len(poly) < 3:
            return None

        # Denormalize to original-image px, then to 1024-space px.
        poly_px = poly * np.array([self._lb.orig_w, self._lb.orig_h], dtype=np.float32)
        x1, y1 = poly_px.min(axis=0)
        x2, y2 = poly_px.max(axis=0)
        cx, cy = poly_px.mean(axis=0)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None

        prompt_px = np.array(
            [[x1, y1], [x2, y2], [cx, cy]],
            dtype=np.float32,
        )
        prompt_in = self._map_xy_to_input(prompt_px)[None, ...]  # (1, 3, 2)
        labels = np.array(
            [[_LABEL_BBOX_TL, _LABEL_BBOX_BR, _LABEL_POS]],
            dtype=np.float32,
        )

        if use_polygon_as_mask:
            mask_input = self._rasterize_polygon_as_mask_input(poly_px)
            has_mask = np.ones((1,), dtype=np.float32)
            # When seeding with a mask, asking for multi-candidate output
            # is counterproductive — SAM2 returns small variations of the
            # same hypothesis, none of them "alternative interpretations".
            want_multimask = False
        else:
            mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
            has_mask = np.zeros((1,), dtype=np.float32)
            want_multimask = True

        out = self._dec.run(
            None,
            {
                "image_embed": self._image_embed,
                "high_res_feats_0": self._high_res_0,
                "high_res_feats_1": self._high_res_1,
                "point_coords": prompt_in,
                "point_labels": labels,
                "mask_input": mask_input,
                "has_mask_input": has_mask,
            },
        )
        masks, iou = out[0], out[1]
        if masks.ndim == 4:
            masks = masks[0]
            iou = iou[0]
        if want_multimask and masks.shape[0] >= 3:
            best = int(np.argmax(iou))
            chosen = masks[best]
        else:
            chosen = masks[0]

        refined = self._postprocess_mask(chosen)
        if refined is None:
            return None

        # Sanity check vs the input polygon's area. SAM2 occasionally
        # returns a mask that's a tiny speck or covers most of the frame
        # — neither is a refinement of what the user/VLM asked for.
        input_mask = self._rasterize_polygon_full_res(poly_px)
        input_area = int(input_mask.sum())
        refined_area = int(refined.sum())
        if input_area == 0:
            return None
        ratio = refined_area / input_area
        lo, hi = area_ratio_bounds
        if ratio < lo or ratio > hi:
            log.info(
                "sam2: rejecting refined mask, area ratio %.2f out of [%.2f, %.2f]",
                ratio,
                lo,
                hi,
            )
            return None
        return refined

    def _rasterize_polygon_as_mask_input(self, poly_px: np.ndarray) -> np.ndarray:
        """VLM polygon → 256×256 logits in SAM2 input space, suitable for
        the decoder's `mask_input` slot. Positive values inside (~+20),
        negative outside (~-20) — SAM2 treats this as 'previous mask
        prediction logits' and refines from there."""
        import cv2

        if self._lb is None:
            raise RuntimeError("sam2: state cleared")
        # mask_input lives in a 256×256 space that corresponds to the
        # SAM2 internal feature map (1024 → 256 via 4× downsample).
        # We rasterize at 1024 then downsample to 256 with INTER_AREA so
        # the resulting binary stays clean.
        canvas = np.zeros((SAM2_INPUT_SIZE, SAM2_INPUT_SIZE), dtype=np.uint8)
        poly_in = (poly_px * self._lb.scale).astype(np.int32)
        cv2.fillPoly(canvas, [poly_in], color=255)
        small = cv2.resize(canvas, (256, 256), interpolation=cv2.INTER_AREA)
        binary = (small > 127).astype(np.float32)
        # Convert binary mask → logits. SAM2 expects values like the
        # decoder's own previous output (post-sigmoid threshold at 0).
        # Magnitude ~20 is large enough that subsequent sigmoid saturates,
        # so the seed is treated as a strong hypothesis without being
        # rigid (SAM2 can still pull edges away if image gradients say so).
        logits = (binary * 40.0) - 20.0
        return logits[None, None, :, :].astype(np.float32)

    def predict_from_clicks(
        self,
        positive_norm: list[list[float]],
        negative_norm: list[list[float]] | None = None,
        *,
        prev_mask: np.ndarray | None = None,
    ) -> np.ndarray | None:
        """User-driven segmentation: take 1+ positive clicks (and optional
        negative clicks to exclude regions) and return the best mask.

        This is SAM2's strongest mode — it was trained on exactly this
        prompt format. With a single positive click on an actual object
        boundary, results are typically pixel-perfect.

        `positive_norm` / `negative_norm` are lists of [x, y] in [0, 1]
        normalized coords. At least one positive click is required.

        `prev_mask` is an optional boolean (orig_h, orig_w) mask returned
        by a previous `predict_from_clicks()` call — pass it back in on
        the next call to iteratively refine. SAM2 uses it as a starting
        hypothesis so the new clicks adjust the existing mask instead
        of starting from scratch.

        Returns a boolean (orig_h, orig_w) mask, or None on failure.
        """
        if self._image_embed is None or self._lb is None:
            raise RuntimeError("sam2: call set_image() before predict_*()")
        if not positive_norm:
            return None
        pos = np.asarray(positive_norm, dtype=np.float32)
        neg = (
            np.asarray(negative_norm, dtype=np.float32)
            if negative_norm
            else np.zeros((0, 2), dtype=np.float32)
        )
        scale_xy = np.array([self._lb.orig_w, self._lb.orig_h], dtype=np.float32)
        pos_px = pos * scale_xy
        neg_px = neg * scale_xy

        all_pts_px = np.concatenate([pos_px, neg_px], axis=0)
        all_labels = np.concatenate(
            [
                np.ones(len(pos_px), dtype=np.float32) * _LABEL_POS,
                np.zeros(len(neg_px), dtype=np.float32),
            ]
        )
        prompt_in = self._map_xy_to_input(all_pts_px)[None, ...]  # (1, N, 2)
        labels_in = all_labels[None, ...]  # (1, N)

        # Seed with previous mask if the caller is iterating — matches
        # Meta's interactive demo behaviour where clicks adjust an
        # evolving mask rather than re-segmenting from scratch.
        if prev_mask is not None and prev_mask.any():
            import cv2

            small = cv2.resize(
                prev_mask.astype(np.uint8) * 255,
                (256, 256),
                interpolation=cv2.INTER_AREA,
            )
            binary = (small > 127).astype(np.float32)
            mask_input = ((binary * 40.0) - 20.0)[None, None, :, :].astype(np.float32)
            has_mask = np.ones((1,), dtype=np.float32)
            want_multimask = False
        else:
            mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
            has_mask = np.zeros((1,), dtype=np.float32)
            # First click — let SAM2 return its 3 candidate scales so we
            # can pick the best-IoU one. For most clicks one candidate
            # dominates; for ambiguous clicks (gate vs gate+fence) this
            # gives us a fighting chance.
            want_multimask = True

        out = self._dec.run(
            None,
            {
                "image_embed": self._image_embed,
                "high_res_feats_0": self._high_res_0,
                "high_res_feats_1": self._high_res_1,
                "point_coords": prompt_in,
                "point_labels": labels_in,
                "mask_input": mask_input,
                "has_mask_input": has_mask,
            },
        )
        masks, iou = out[0], out[1]
        if masks.ndim == 4:
            masks = masks[0]
            iou = iou[0]
        chosen = masks[int(np.argmax(iou))] if want_multimask and masks.shape[0] >= 3 else masks[0]
        return self._postprocess_mask(chosen)

    def _rasterize_polygon_full_res(self, poly_px: np.ndarray) -> np.ndarray:
        """VLM polygon → (orig_h, orig_w) boolean mask. Used for the
        area-ratio sanity check against SAM2's refined output."""
        import cv2

        if self._lb is None:
            raise RuntimeError("sam2: state cleared")
        canvas = np.zeros((self._lb.orig_h, self._lb.orig_w), dtype=np.uint8)
        cv2.fillPoly(canvas, [poly_px.astype(np.int32)], color=1)
        return canvas.astype(bool)

    def _postprocess_mask(self, low_res_logits: np.ndarray) -> np.ndarray | None:
        """SAM2 returns low-res mask logits in 1024-space. Upscale to the
        full 1024 input then crop+resize back to the original image."""
        import cv2

        if self._lb is None:
            raise RuntimeError("sam2: state cleared")
        # Logits → resize to 1024×1024 with bilinear, then threshold.
        full = cv2.resize(
            low_res_logits.astype(np.float32),
            (SAM2_INPUT_SIZE, SAM2_INPUT_SIZE),
            interpolation=cv2.INTER_LINEAR,
        )
        # Crop off the bottom/right padding we added in letterbox_image.
        cropped = full[: self._lb.new_h, : self._lb.new_w]
        # Resize back to the original snapshot resolution.
        unscaled = cv2.resize(
            cropped,
            (self._lb.orig_w, self._lb.orig_h),
            interpolation=cv2.INTER_LINEAR,
        )
        mask = unscaled > 0.0
        if not mask.any():
            return None
        return mask


def mask_to_polygon(
    mask: np.ndarray,
    *,
    epsilon_frac: float = 0.005,
    max_vertices: int = 64,
) -> list[list[float]] | None:
    """Boolean (H, W) mask → list of [x, y] vertices normalized to [0, 1].

    Uses cv2.findContours to get the outer boundary of the largest
    connected component, then Douglas-Peucker (cv2.approxPolyDP) to
    simplify down to a manageable vertex count. `epsilon_frac` is the
    DP tolerance as a fraction of the contour perimeter — 0.005 yields
    ~15–30 vertices for typical zone shapes; raise it to simplify
    further (lose detail), lower it to keep more vertices.

    Returns None if the mask is empty or no contour can be extracted.
    """
    import cv2

    h, w = mask.shape[:2]
    binary = mask.astype(np.uint8) * 255
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    # Pick the largest connected component — SAM2 occasionally emits
    # specks on background texture that we don't want as separate zones.
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 100:  # < 100 px² is noise, discard
        return None

    epsilon = epsilon_frac * cv2.arcLength(contour, closed=True)
    approx = cv2.approxPolyDP(contour, epsilon, closed=True)
    pts = approx.reshape(-1, 2)
    if len(pts) < 3:
        return None
    # If still too many vertices, increase epsilon iteratively. Rare
    # for typical zone shapes but the API caps polygon size at 64.
    while len(pts) > max_vertices:
        epsilon *= 1.5
        approx = cv2.approxPolyDP(contour, epsilon, closed=True)
        pts = approx.reshape(-1, 2)
        if epsilon > cv2.arcLength(contour, closed=True):
            break  # safety: don't loop forever on weird contours
    if len(pts) < 3:
        return None
    out = [[float(x) / w, float(y) / h] for x, y in pts]
    return out
