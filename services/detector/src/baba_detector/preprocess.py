from __future__ import annotations

import cv2
import numpy as np


def letterbox(
    image: np.ndarray, target: int, color: tuple[int, int, int] = (114, 114, 114)
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize while preserving aspect ratio, pad to target×target."""
    h, w = image.shape[:2]
    scale = min(target / h, target / w)
    new_w, new_h = round(w * scale), round(h * scale)
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_w = target - new_w
    pad_h = target - new_h
    top, bottom = pad_h // 2, pad_h - pad_h // 2
    left, right = pad_w // 2, pad_w - pad_w // 2
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return padded, scale, (left, top)


def letterbox_batch(
    images: list[np.ndarray], target: int
) -> tuple[np.ndarray, list[float], list[tuple[int, int]]]:
    """Stack a list of HWC RGB images into a BCHW float32 tensor."""
    tensors: list[np.ndarray] = []
    scales: list[float] = []
    pads: list[tuple[int, int]] = []
    for img in images:
        padded, scale, pad = letterbox(img, target)
        tensors.append(padded)
        scales.append(scale)
        pads.append(pad)
    batch = np.stack(tensors, axis=0).astype(np.float32) / 255.0
    batch = batch.transpose(0, 3, 1, 2)  # BHWC -> BCHW
    return np.ascontiguousarray(batch), scales, pads


def letterbox_batch_uint8(
    images: list[np.ndarray], target: int
) -> tuple[np.ndarray, list[float], list[tuple[int, int]]]:
    """Like `letterbox_batch` but stops at uint8 NHWC.

    For models that have Cast(uint8→f32) + Mul(1/255) + Transpose(NHWC→NCHW)
    baked into their graph (see `tools/bake_preproc.py`). Lets the GPU
    absorb the normalisation, dtype cast and layout permute that this
    function would otherwise do on the CPU — measured at ~20 ms per
    8-image batch on a 6-core machine.

    Returns: (batch, scales, pads) — batch is (B, target, target, 3) uint8.
    scales and pads are unchanged from the float32 variant so the
    postprocessor's letterbox-undo math keeps working.
    """
    tensors: list[np.ndarray] = []
    scales: list[float] = []
    pads: list[tuple[int, int]] = []
    for img in images:
        padded, scale, pad = letterbox(img, target)
        tensors.append(padded)
        scales.append(scale)
        pads.append(pad)
    batch = np.stack(tensors, axis=0)
    return np.ascontiguousarray(batch), scales, pads


# Neutral fill values when letterboxing NV12. Y=114 is the same dim-gray
# luminance the RGB letterbox uses (so the post-BT.709 RGB pad is also
# 114,114,114 to within rounding); U=V=128 are the unsigned chroma
# neutrals (zero chroma offset after the -128 bias).
_NV12_PAD_Y = 114
_NV12_PAD_U = 128
_NV12_PAD_V = 128


def letterbox_nv12(
    frame: np.ndarray,
    target: int,
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize an NV12 frame to fit in target×target preserving aspect, then
    pad to a square with neutral Y/U/V fills.

    Input shape: (H + H/2, W) uint8 — Y plane in the top H rows, interleaved
    UV plane (UVUV…) in the bottom H/2 rows. Width and height must be even
    (NV12 invariant).

    Output shape: (target + target/2, target) uint8 NV12 — same layout, but
    Y plane is exactly target×target and UV plane exactly (target/2)×target.
    Scale and pad are returned in Y-plane (picture) coordinates so the
    existing postprocessor's letterbox-undo math doesn't change.
    """
    buf_h, buf_w = frame.shape[:2]
    if frame.ndim != 2:
        raise ValueError(f"NV12 letterbox expects 2D ndarray, got shape {frame.shape}")
    if target % 2:
        raise ValueError(f"NV12 letterbox target must be even, got {target}")
    H = buf_h * 2 // 3
    W = buf_w
    if H % 2 or W % 2:
        raise ValueError(f"NV12 letterbox input dims must be even; got H={H} W={W}")

    # Aspect-preserving fit, rounded to even (chroma subsample alignment).
    scale = min(target / H, target / W)
    new_w = max(2, (round(W * scale) // 2) * 2)
    new_h = max(2, (round(H * scale) // 2) * 2)

    y_plane = frame[:H, :]  # (H, W)
    uv_flat = frame[H:, :]  # (H/2, W) interleaved
    # Split UV interleaved into separate U and V half-resolution planes so we
    # can run plain cv2.resize on each — cv2 is happiest with 1-channel
    # buffers, and a 2-channel ndarray would also work but is less portable
    # across cv2 builds. (H/2, W) → (H/2, W/2, 2)
    uv_pairs = uv_flat.reshape(H // 2, W // 2, 2)
    u_plane = uv_pairs[..., 0]  # (H/2, W/2)
    v_plane = uv_pairs[..., 1]  # (H/2, W/2)

    y_resized = cv2.resize(y_plane, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    u_resized = cv2.resize(u_plane, (new_w // 2, new_h // 2), interpolation=cv2.INTER_LINEAR)
    v_resized = cv2.resize(v_plane, (new_w // 2, new_h // 2), interpolation=cv2.INTER_LINEAR)

    # Padding in Y coords. UV pad is half of that (chroma is at H/2 × W/2).
    pad_h = target - new_h
    pad_w = target - new_w
    top = (pad_h // 2) & ~1  # ensure even for chroma alignment
    bottom = pad_h - top
    left = (pad_w // 2) & ~1
    right = pad_w - left
    top_uv = top // 2
    bottom_uv = bottom // 2
    left_uv = left // 2
    right_uv = right // 2

    y_padded = cv2.copyMakeBorder(
        y_resized,
        top,
        bottom,
        left,
        right,
        cv2.BORDER_CONSTANT,
        value=_NV12_PAD_Y,
    )
    u_padded = cv2.copyMakeBorder(
        u_resized,
        top_uv,
        bottom_uv,
        left_uv,
        right_uv,
        cv2.BORDER_CONSTANT,
        value=_NV12_PAD_U,
    )
    v_padded = cv2.copyMakeBorder(
        v_resized,
        top_uv,
        bottom_uv,
        left_uv,
        right_uv,
        cv2.BORDER_CONSTANT,
        value=_NV12_PAD_V,
    )

    # Re-interleave U and V back into a single (target/2, target) row.
    uv_interleaved = np.empty((target // 2, target), dtype=np.uint8)
    uv_interleaved[:, 0::2] = u_padded
    uv_interleaved[:, 1::2] = v_padded

    # Stack Y over UV → final NV12 buffer.
    out = np.empty((target + target // 2, target), dtype=np.uint8)
    out[:target, :] = y_padded
    out[target:, :] = uv_interleaved

    return out, scale, (left, top)


def stretch_nv12(
    frame: np.ndarray,
    target: int,
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize an NV12 frame to target×target by STRETCHING (anisotropic, no
    pad) — RF-DETR's native square resize.

    RF-DETR's DINOv2 backbone is trained on square-resized inputs; a grey
    letterbox pad is out-of-distribution (measured: 1/8 vs 0/8 phantom-person
    on shed). The box output is normalized to the stretched square, which maps
    LINEARLY back to the original frame, so the RF-DETR postprocessor undoes it
    with the original dims directly — the returned scale/pad are placeholders
    (1.0, (0,0)) and are not consulted by RFDetrPostprocessor.
    """
    buf_h, buf_w = frame.shape[:2]
    if frame.ndim != 2:
        raise ValueError(f"NV12 stretch expects 2D ndarray, got shape {frame.shape}")
    if target % 2:
        raise ValueError(f"NV12 stretch target must be even, got {target}")
    H = buf_h * 2 // 3
    W = buf_w
    if H % 2 or W % 2:
        raise ValueError(f"NV12 stretch input dims must be even; got H={H} W={W}")

    y_plane = frame[:H, :]
    uv_pairs = frame[H:, :].reshape(H // 2, W // 2, 2)
    u_plane = uv_pairs[..., 0]
    v_plane = uv_pairs[..., 1]

    y_resized = cv2.resize(y_plane, (target, target), interpolation=cv2.INTER_LINEAR)
    u_resized = cv2.resize(u_plane, (target // 2, target // 2), interpolation=cv2.INTER_LINEAR)
    v_resized = cv2.resize(v_plane, (target // 2, target // 2), interpolation=cv2.INTER_LINEAR)

    uv_interleaved = np.empty((target // 2, target), dtype=np.uint8)
    uv_interleaved[:, 0::2] = u_resized
    uv_interleaved[:, 1::2] = v_resized

    out = np.empty((target + target // 2, target), dtype=np.uint8)
    out[:target, :] = y_resized
    out[target:, :] = uv_interleaved
    return out, 1.0, (0, 0)


def stretch_batch_nv12(
    images: list[np.ndarray],
    target: int,
) -> tuple[np.ndarray, list[float], list[tuple[int, int]]]:
    """Batch stretch_nv12 (RF-DETR native square resize). Scales/pads are
    placeholders; RFDetrPostprocessor maps normalized boxes with the frame's
    own dims (a stretch is a linear full-frame map)."""
    tensors: list[np.ndarray] = []
    scales: list[float] = []
    pads: list[tuple[int, int]] = []
    for img in images:
        stretched, scale, pad = stretch_nv12(img, target)
        tensors.append(stretched)
        scales.append(scale)
        pads.append(pad)
    batch = np.stack(tensors, axis=0)
    return np.ascontiguousarray(batch), scales, pads


def letterbox_batch_nv12(
    images: list[np.ndarray],
    target: int,
) -> tuple[np.ndarray, list[float], list[tuple[int, int]]]:
    """Stack NV12 frames into a (B, target + target/2, target) uint8 batch.

    For models built with `tools/bake_preproc.py --input-format nv12`, which
    accept this flat NV12 layout and do the BT.709 limited YUV→RGB
    conversion on the GPU as part of the engine. Skips libswscale on the
    ingestor side AND the cv2.resize+RGB-cast step that letterbox_batch_uint8
    would do.
    """
    tensors: list[np.ndarray] = []
    scales: list[float] = []
    pads: list[tuple[int, int]] = []
    for img in images:
        padded, scale, pad = letterbox_nv12(img, target)
        tensors.append(padded)
        scales.append(scale)
        pads.append(pad)
    batch = np.stack(tensors, axis=0)
    return np.ascontiguousarray(batch), scales, pads
