"""Embedding backends. Today: stub (random) + DINOv2-ONNX. Same input
contract (list of (224, 224, 3) uint8 RGB crops) and output contract
(L2-normalized (N, dim) float32 matrix) so the service can swap them by
config without touching the consumer side.

Lives in core (not in the embedder service) because the api also runs
this inference path now — when an operator uploads reference photos
through the Identities UI, the api decodes them, runs DINOv2 on each,
averages the result, and writes `identity_labels.reference_embedding`.
Keeping the model wrapper here means both services use the exact same
preprocessing + output handling — critical for the reference embedding
to be directly comparable with what the live pipeline produces.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

import numpy as np

# NOTE: cv2 is imported lazily inside _osnet_preprocess — services that import
# this module only for types/constants (e.g. event-manager) don't ship opencv.

log = logging.getLogger(__name__)

# Body re-ID embedding dim. As of 2026-06-18 the body model is OSNet
# (osnet_x0_25_msmt17, a *dedicated* person-ReID network trained with
# identity metric learning) — NOT DINOv2. DINOv2 is a general
# self-supervised feature extractor and scores ~0.3-4.7% mAP on ReID
# (it groups by colour/shape, not identity), which produced the
# "everyone resembles Marko" garbage. OSNet output is 512-d, so the DB
# body columns are vector(512). The DINOv2 backend below is kept as a
# selectable alternative but is no longer the default.
EMBEDDING_DIM = 512

# DINOv2 (legacy backend) input size. 224 = 14*16 patches, its pretraining
# size. The DINOv2 backend resizes to this itself — callers no longer
# pre-resize to it. The DEFAULT body backend is OSNet, which uses 256x128.
EMBED_INPUT_SIZE = 224

# Canonical stored/embedded crop cap. We DON'T pre-resize crops to a fixed
# model size upstream anymore — every backend resizes to its own input
# (OSNet 256x128 stretch, DINOv2 224, SCRFD 640 letterbox), so a fixed
# pre-resize only threw away detail before all of them AND padded crops with
# black bars OSNet was never trained on. Instead the crop is kept at its
# native aspect ratio, only downscaled when its long edge exceeds this cap
# (never upscaled — a 50px distant subject has no detail to invent). Bounds
# JPEG size + inference cost while giving face detection and display the full
# pixels the camera actually captured.
MAX_CROP_EDGE = 512

# Standard ImageNet normalization. DINOv2 was trained with these means/stds
# and runtime preprocessing must match or features degrade silently.
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 3, 1, 1)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 3, 1, 1)


def cap_long_edge(crop: np.ndarray, max_edge: int = MAX_CROP_EDGE) -> np.ndarray:
    """Downscale a crop so its longest edge is <= max_edge, preserving aspect
    ratio. No upscaling, no padding — distant tiny crops stay native size.
    This is the canonical "prepare a crop for storage + embedding" step shared
    by the embedder (live tracks) and the api (reference photos, search), so
    all three feed identical content into the backends and land in the same
    embedding space. INTER_AREA is the right kernel for downscaling."""
    import cv2  # lazy: pure-types importers (event-manager) don't ship opencv

    h, w = crop.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return crop
    scale = max_edge / long_edge
    nw = max(1, round(w * scale))
    nh = max(1, round(h * scale))
    return cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (x / norms).astype(np.float32, copy=False)


def _preprocess_batch(crops: list[np.ndarray]) -> np.ndarray:
    """uint8 RGB crops of ANY size → float32 NCHW ImageNet-normalized at
    EMBED_INPUT_SIZE. Each crop is letterboxed to a square then resized so a
    distant pedestrian isn't stretched into an aspect ratio DINOv2 never saw —
    the backend owns this so callers can pass native-aspect crops."""
    if not crops:
        return np.zeros((0, 3, EMBED_INPUT_SIZE, EMBED_INPUT_SIZE), dtype=np.float32)
    import cv2  # lazy

    squares = []
    for c in crops:
        if c.dtype != np.uint8:
            c = c.astype(np.uint8)
        h, w = c.shape[:2]
        side = max(h, w)
        top, left = (side - h) // 2, (side - w) // 2
        sq = cv2.copyMakeBorder(
            c,
            top,
            side - h - top,
            left,
            side - w - left,
            cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )
        squares.append(
            cv2.resize(sq, (EMBED_INPUT_SIZE, EMBED_INPUT_SIZE), interpolation=cv2.INTER_AREA)
        )
    arr = np.stack(squares, axis=0).astype(np.float32) / 255.0
    arr = arr.transpose(0, 3, 1, 2)
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD
    return np.ascontiguousarray(arr, dtype=np.float32)


class EmbeddingBackend(Protocol):
    dim: int

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """Returns an (N, dim) L2-normalized float32 matrix. Empty input
        gets an empty (0, dim) result, not an exception."""
        ...


class StubBackend:
    """Random L2-normalized vectors. Useful as a fallback when the model
    file isn't present (first deploy, dev box without weights) so the rest
    of the pipeline can still be tested end-to-end."""

    dim = EMBEDDING_DIM

    def __init__(self) -> None:
        self._rng = np.random.default_rng(seed=0xBABA)

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.dim), dtype=np.float32)
        v = self._rng.standard_normal((len(crops), self.dim), dtype=np.float32)
        return _l2_normalize(v)


class DINOv2OnnxBackend:
    """Real DINOv2 features via ONNX Runtime (CPU).

    Expects the ONNX exported with `optimum-cli export onnx --model
    facebook/dinov2-small --task image-feature-extraction`. That export
    has a single input `pixel_values` (B, 3, 224, 224) float32 and a
    single output `last_hidden_state` (B, 257, 384). The CLS token at
    index 0 of the sequence dimension is what we use for re-ID.

    Retained as a selectable alternative; OSNet (below) is the default
    body backend. DINOv2's 384-d output is incompatible with the current
    vector(512) DB columns, so selecting it requires a 384-d schema.
    """

    dim = 384

    def __init__(self, model_path: Path) -> None:
        # Cap intra-op threads even on GPU so CPU postprocessing doesn't
        # contend with the rest of the box; the GPU does the heavy lifting.
        from baba_core.onnx_session import make_session

        self._session, self._provider = make_session(
            model_path,
            intra_op_threads=4,
            name="dinov2",
        )
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if not inputs or not outputs:
            raise RuntimeError(f"dinov2 onnx has no inputs/outputs: {model_path}")
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name
        log.info(
            "dinov2 ready on %s | input=%s%s | output=%s%s",
            self._provider,
            self._input_name,
            inputs[0].shape,
            self._output_name,
            outputs[0].shape,
        )

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.dim), dtype=np.float32)
        x = _preprocess_batch(crops)
        out = self._session.run([self._output_name], {self._input_name: x})[0]
        # Two common shapes from feature-extraction exports:
        #  - (B, num_tokens, hidden) → CLS = index 0 on the token axis
        #  - (B, hidden)             → already pooled
        if out.ndim == 3:
            cls = out[:, 0, :]
        elif out.ndim == 2:
            cls = out
        else:
            raise RuntimeError(
                f"unexpected dinov2 output shape {out.shape}; expected (B, N, D) or (B, D)"
            )
        if cls.shape[1] != self.dim:
            raise RuntimeError(
                f"dinov2 output dim {cls.shape[1]} != expected {self.dim} "
                f"(wrong model size? this build expects ViT-S/14)"
            )
        return _l2_normalize(cls.astype(np.float32, copy=False))


# OSNet person-ReID input contract (matches the tracker's export):
#   input  (B, 3, 256, 128) float32, ImageNet-normalised, B often static=16
#   output (B, 512)         float32, raw (we L2-normalise here)
_OSNET_H, _OSNET_W, _OSNET_DIM = 256, 128, 512
_OSNET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_OSNET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _osnet_preprocess(crops: list[np.ndarray]) -> np.ndarray:
    """RGB uint8 HWC crops (any size) → (N, 3, 256, 128) float32 ImageNet-norm.
    Stretch-resize: ReID training used stretched person crops."""
    import cv2  # lazy: only services that actually embed need opencv

    out = np.empty((len(crops), 3, _OSNET_H, _OSNET_W), dtype=np.float32)
    for i, c in enumerate(crops):
        if c.dtype != np.uint8:
            c = c.astype(np.uint8)
        r = cv2.resize(c, (_OSNET_W, _OSNET_H), interpolation=cv2.INTER_LINEAR)
        x = (r.astype(np.float32) / 255.0 - _OSNET_MEAN) / _OSNET_STD
        out[i] = np.transpose(x, (2, 0, 1))
    return out


class OSNetOnnxBackend:
    """Person re-ID via OSNet ONNX (osnet_x0_25_msmt17 by default). Unlike
    DINOv2, OSNet is trained with identity metric learning, so different
    people are pushed apart in feature space — the right tool for re-ID."""

    dim = _OSNET_DIM

    def __init__(self, model_path: Path) -> None:
        from baba_core.onnx_session import make_session

        self._session, self._provider = make_session(
            model_path,
            intra_op_threads=2,
            name="osnet",
        )
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if not inputs or not outputs:
            raise RuntimeError(f"osnet onnx has no inputs/outputs: {model_path}")
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name
        # Static batch if the export fixes dim 0 to a positive int.
        b = inputs[0].shape[0] if inputs[0].shape else None
        self._batch = b if isinstance(b, int) and b > 0 else 0
        log.info(
            "osnet ready on %s | input=%s%s | output=%s%s | static_batch=%s",
            self._provider,
            self._input_name,
            inputs[0].shape,
            self._output_name,
            outputs[0].shape,
            self._batch or "dynamic",
        )

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.dim), dtype=np.float32)
        x = _osnet_preprocess(crops)
        n = x.shape[0]
        if self._batch:
            outs: list[np.ndarray] = []
            for s in range(0, n, self._batch):
                chunk = x[s : s + self._batch]
                real = chunk.shape[0]
                if real < self._batch:
                    pad = np.zeros((self._batch - real, 3, _OSNET_H, _OSNET_W), dtype=np.float32)
                    chunk = np.concatenate([chunk, pad], axis=0)
                out = self._session.run([self._output_name], {self._input_name: chunk})[0]
                outs.append(out[:real])
            merged = np.concatenate(outs, axis=0).astype(np.float32, copy=False)
        else:
            merged = self._session.run(
                [self._output_name],
                {self._input_name: x},
            )[0].astype(np.float32, copy=False)
        if merged.shape[1] != self.dim:
            raise RuntimeError(f"osnet output dim {merged.shape[1]} != expected {self.dim}")
        return _l2_normalize(merged)


def require_real_embedder_default() -> bool:
    """Whether a missing/broken embedder model is fatal (no StubBackend).

    Explicit `BABA_REQUIRE_REAL_EMBEDDER` wins; otherwise derive from the build
    variant — nvidia/intel are production builds where a missing model must
    surface, never degrade to random vectors that get persisted into pgvector
    and silently poison re-ID. Same shape as the tracker's
    `_require_reid_default`.
    """
    v = os.environ.get("BABA_REQUIRE_REAL_EMBEDDER", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return os.environ.get("BABA_VARIANT", "cpu").strip().lower() in ("nvidia", "intel")


def make_backend(
    model_path: Path | None,
    require_real: bool | None = None,
) -> EmbeddingBackend:
    """Pick the best available backend.

    On production variants (nvidia/intel, or `BABA_REQUIRE_REAL_EMBEDDER=1`) a
    missing or broken model file is FATAL — it raises instead of returning
    StubBackend, because StubBackend emits random vectors and the embedder
    persists them into `track_embedding_samples`, silently poisoning re-ID with
    data that outlives the incident. Callers that want to degrade gracefully
    (e.g. the api catches this and returns 503 for reference-photo enrollment
    rather than storing garbage) get the exception and decide.

    Only on dev/cpu builds (require_real=False) does a missing model fall back
    to the stub with a loud warning. Other exceptions (e.g. onnxruntime not
    installed) always propagate.

    `require_real` overrides the env/variant default."""
    if require_real is None:
        require_real = require_real_embedder_default()
    if model_path is None:
        if require_real:
            raise RuntimeError(
                "BABA_EMBEDDER_MODEL not set but a real embedder is required "
                "(nvidia/intel variant or BABA_REQUIRE_REAL_EMBEDDER=1). "
                "Refusing to start with random-vector StubBackend — set the env "
                "var to an OSNet ONNX file, or force dev mode with "
                "BABA_REQUIRE_REAL_EMBEDDER=0."
            )
        log.warning(
            "BABA_EMBEDDER_MODEL not set — using STUB backend (random vectors). "
            "Set the env var to an OSNet ONNX file to enable real embeddings."
        )
        return StubBackend()
    try:
        # Pick by filename: 'osnet*' → person-ReID (default), 'dinov2*' →
        # legacy general features (needs a 384-d schema).
        name = model_path.name.lower()
        if "dinov2" in name:
            return DINOv2OnnxBackend(model_path)
        return OSNetOnnxBackend(model_path)
    except FileNotFoundError:
        if require_real:
            raise RuntimeError(
                f"embedder model file {model_path} not found but a real "
                "embedder is required (nvidia/intel variant or "
                "BABA_REQUIRE_REAL_EMBEDDER=1). Refusing StubBackend "
                "(random vectors would poison pgvector re-ID)."
            ) from None
        log.warning(
            "embedder model file %s not found — falling back to STUB backend.",
            model_path,
        )
        return StubBackend()
