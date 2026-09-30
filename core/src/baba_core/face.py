"""Face detection + recognition stack.

Two ONNX models, both commercially clean and served on the same execution
target as DINOv2 (`baba_core.onnx_session.make_session`), which the build
variant picks for the entire ONNX pipeline.

Pipeline (always runs on `class=person` body crops, never on vehicles):

    body crop (224x224 RGB, from baba_core.embed pre-resize)
        → YuNet detect → list of (bbox, 5 landmarks, score)
        → pick the largest above MIN_FACE_SCORE
        → affine warp to 112x112 using the 5 landmarks (ArcFace standard)
        → AuraFace embed → 512-d L2-normalized vector

Picks (audited May 2026, see ../../services/embedder/scripts/fetch_face_models.sh):

  Detector: YuNet (face_detection_yunet_2023mar.onnx)
    - Author: opencv_zoo + libfacedetection (W.Y. Yu et al.)
    - License: MIT *on the weights*, not just the code — very rare for
      face stacks. ~228 KB ONNX. WIDER FACE hard AP ~0.75. Good enough
      for "is there a face inside this 224x224 crop", and crucially it
      gives us 5 landmarks (eyes + nose + mouth corners) for alignment.
    - Avoided alternatives: SCRFD is more accurate but its weights are
      research-only (InsightFace training data); RetinaFace ports are
      similarly muddy on weight licenses; YOLOv5/8-face weights are
      GPL on the weights.

  Embedder: AuraFace v1 (face_auraface.onnx, ResNet100 ArcFace head)
    - Author: fal (HuggingFace fal/AuraFace-v1)
    - License: Apache 2.0 *on the weights* — fal explicitly retrained the
      ArcFace architecture on commercial-clean data to give us an
      alternative to the InsightFace buffalo packs.
    - ~249 MB ONNX, 512-d output, LFW 99.65 / CFP-FP 95.18 / AgeDB 96.10.
    - Avoided alternatives: InsightFace buffalo_l/s, AdaFace shipped
      weights, ElasticFace, MagFace, EdgeFace — every one of them
      either trained on MS1M*/Glint/WebFace (non-commercial) or
      explicitly CC-BY-NC. Code license is irrelevant; weight provenance
      is what matters. AuraFace is currently the cleanest production
      pick that exists.

Output embeddings are directly comparable: cosine distance on
L2-normalized 512-d vectors. Typical thresholds (calibrate per deployment):
  < 0.30  almost certainly the same person
  0.30-0.45  likely match, gates user-merge offer
  > 0.45  different identity
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

# cv2 is deferred to call-site: services that only consume baba_core
# types (event-manager, tracker) shouldn't have to ship opencv-python
# just to import this module. The detector/embedder/api containers do
# need it and will import lazily on first use.

log = logging.getLogger(__name__)

FACE_EMBEDDING_DIM = 512
FACE_INPUT_SIZE = 112  # ArcFace standard

# YuNet score threshold. The model outputs sigmoid-ish scores. Surveillance
# body crops produce faces that score in the 0.5-0.7 range (smaller than
# typical selfie / WIDER-test faces); 0.6 was too strict and rejected real
# matches. 0.5 catches genuine faces with the occasional weak detection
# downstream embedder filters tend to cluster as low-confidence anyway.
# Licences BABA may ship weights under. Anything else is BYOM: legitimate on the
# operator's own host, never redistributable in a product build.
_COMMERCIAL_CLEAN_LICENSES = frozenset({"apache-2.0", "mit", "bsd-3-clause"})

MIN_FACE_SCORE = 0.5
# A face bbox below this size in pixels won't have enough detail for a
# stable AuraFace embedding. Tuned against the 224x224 body crop scale.
MIN_FACE_SIDE_PX = 28

# ArcFace canonical 5-point template — eyes, nose, mouth corners — to
# which YuNet's landmarks get affine-warped. Values are for 112x112 input.
# Standard across all ArcFace-family embedders.
_ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],  # left eye
        [73.5318, 51.5014],  # right eye
        [56.0252, 71.7366],  # nose tip
        [41.5493, 92.3655],  # left mouth corner
        [70.7299, 92.2041],  # right mouth corner
    ],
    dtype=np.float32,
)


# Above this, the five landmarks cannot be made to sit on the ArcFace template
# by any similarity transform — the thing in the box is not a face presented to
# the camera. Measured 2026-09-10 from both sides: of 162 curated reference
# portraits NONE exceeds it (median 0.085, p99 0.247, max 0.269), and of the
# pipeline's own accepted "faces" every one above it was paving, a beam or a
# wall — including the 117 px patio detection that outranked every real face on
# that camera and, being the track's largest, kept it from ever being re-read.
# Used to RANK, never to reject: a track whose every sample is implausible
# still yields one, so nothing can be lost to it.
FRONTALITY_MAX = 0.29


async def set_canonical_face(db, track_id, model_key: str) -> None:
    """Point the track's face at its best sample in `model_key`'s space.

    Finalize and the native re-read both ask this, so there is one answer.

    Not by person-box confidence: that says nothing about the face, and of the
    ~10 face samples a person track yields typically ONE is a real frontal face
    (patio 2026-07-15: same-person distances 0.870 against 0.886 for random
    pairs). First whether the face survived its own alignment (099: 0 of 70
    ears, profiles and crowns do, 161 of 162 portraits), then plausibility
    (the residual lets a crown of hair through), then the face detector's own
    score — against face_px it picked the sample nearer the enrolled portraits
    on 97 of 182 named tracks where the two disagree (2026-09-25). NULLs rank
    as plausible, so rows older than 060/098 are not punished for a number
    nobody measured.

    Only vectors from `model_key`: a face embedding is comparable only within
    the model that produced it, and `face_embedding_model` on the track is how
    a matcher knows which space the canonical face is in.
    """
    await db.execute(
        """
        UPDATE tracks SET
            face_embedding       = face_best.face_embedding,
            face_embedding_model = $3,
            face_crop_path       = face_best.face_crop_path,
            -- Not the chosen sample's own size but the BEST the track ever
            -- had: whether this track may name anyone is a question about the
            -- track, not about that one frame. Measured off the samples not
            -- known to be something other than a face, or a beam of the patio
            -- awning lifts a track over the identity floor with 117 px of
            -- masonry — and so does an ear, which the residual alone lets past.
            face_px              = face_best.best_px
        FROM (
            SELECT face_embedding, face_crop_path,
                   COALESCE(
                       (SELECT max(face_px)
                          FROM track_embedding_samples
                         WHERE track_id = $1
                           AND face_embedding IS NOT NULL
                           AND face_embedding_model = $3
                           AND face_realigned IS NOT FALSE
                           AND COALESCE(face_frontality, 0) <= $2),
                       -- The residual is soft and mostly NULL, so a track whose
                       -- every sample sits above it still gets a size. The
                       -- alignment check is not soft: falling back to ALL
                       -- samples would hand the floor an ear's 68 px. 370
                       -- tracks had nothing but non-faces; 53 of them were
                       -- naming people through that hole.
                       (SELECT max(face_px)
                          FROM track_embedding_samples
                         WHERE track_id = $1
                           AND face_embedding IS NOT NULL
                           AND face_embedding_model = $3
                           AND face_realigned IS NOT FALSE),
                       -- Nothing face-shaped at all: names nobody, and 0 keeps
                       -- it inside the native re-read's `face_px < 60`.
                       0
                   ) AS best_px
            FROM track_embedding_samples
            WHERE track_id = $1
              AND face_embedding IS NOT NULL
              AND face_embedding_model = $3
            ORDER BY CASE WHEN face_realigned THEN 0
                          WHEN face_realigned IS NULL THEN 1
                          ELSE 2 END ASC,
                     COALESCE(face_frontality, 0) > $2 ASC,
                     face_score DESC NULLS LAST
            LIMIT 1
        ) AS face_best
        WHERE tracks.id = $1
        """,
        track_id,
        FRONTALITY_MAX,
        model_key,
    )


def frontality_residual(kps: np.ndarray) -> float | None:
    """How far the 5 landmarks sit from the canonical template after the best
    similarity fit, in template inter-ocular units. A similarity may rotate,
    scale and translate but NOT squash, so a foreshortened crown of a head or
    an incoherent set of points cannot be fitted down to a small residual the
    way a face presented to the camera can. None when the fit is degenerate."""
    import cv2

    src = kps.astype(np.float32)
    if src.shape != (5, 2):
        return None
    M, _inliers = cv2.estimateAffinePartial2D(src, _ARCFACE_TEMPLATE, method=cv2.LMEDS)
    if M is None:
        return None
    fitted = (M[:, :2] @ src.T).T + M[:, 2]
    iod = float(np.linalg.norm(_ARCFACE_TEMPLATE[1] - _ARCFACE_TEMPLATE[0]))
    return float(np.sqrt(((fitted - _ARCFACE_TEMPLATE) ** 2).sum(1).mean()) / iod)


def landmarks_look_frontal(kps: np.ndarray, bbox: tuple[float, float, float, float]) -> bool:
    """True only when the 5 landmarks form a geometrically plausible
    (semi-)frontal face. Top-down / rear cameras (e.g. a patio cam looking
    down at people's backs) make SCRFD/YuNet fire on the BACK OF A HEAD or a
    head of hair — those false positives carry incoherent landmarks (eyes
    below the nose, collapsed inter-ocular distance, nose outside the eye
    span). Embedding such a crop yields a vector that chains unrelated people
    into one identity (a bald man's head-back and a woman's hair both score
    as the same garbage), so we drop the detection rather than trust it.

    Landmark order is ArcFace: left_eye, right_eye, nose, left_mouth,
    right_mouth (x, y; image y grows downward). Generous bounds — the point
    is to reject obvious non-faces, not to enforce strict frontality, so real
    in-domain faces on the doorbell / eye-level cameras still pass."""
    if kps.shape != (5, 2):
        return False
    le, re, nose, lm, rm = kps[0], kps[1], kps[2], kps[3], kps[4]
    bw = float(bbox[2] - bbox[0])
    bh = float(bbox[3] - bbox[1])
    if bw <= 0 or bh <= 0:
        return False
    # Eyes and mouth corners ordered left→right.
    if not (le[0] < re[0] and lm[0] < rm[0]):
        return False
    eye_y = (le[1] + re[1]) / 2.0
    mouth_y = (lm[1] + rm[1]) / 2.0
    # Vertical face structure: eyes above nose above mouth.
    if not (eye_y < nose[1] < mouth_y):
        return False
    # Inter-ocular distance a sane fraction of the face width.
    eye_dx = abs(float(re[0] - le[0]))
    if not (0.15 * bw <= eye_dx <= 0.95 * bw):
        return False
    # Nose horizontally within a slack around the eye span.
    slack = 0.5 * eye_dx
    if not (min(le[0], re[0]) - slack <= nose[0] <= max(le[0], re[0]) + slack):
        return False
    # Eye→mouth vertical span a sane fraction of the face height.
    return 0.15 * bh <= mouth_y - eye_y <= 0.95 * bh


class FaceDetector(Protocol):
    def detect(self, image_rgb: np.ndarray) -> FaceDetection | None:
        """Return the most prominent detected face or None."""
        ...


class FaceEmbedder(Protocol):
    dim: int

    def embed(self, aligned_112: np.ndarray) -> np.ndarray:
        """Return (512,) L2-normalized float32 embedding."""
        ...


# --- detection -----------------------------------------------------------


class FaceDetection:
    """One detected face, in *input-image* pixel coordinates."""

    __slots__ = ("bbox", "landmarks", "score")

    def __init__(
        self, bbox: tuple[float, float, float, float], landmarks: np.ndarray, score: float
    ) -> None:
        self.bbox = bbox  # (x1, y1, x2, y2)
        self.landmarks = landmarks  # shape (5, 2) float32
        self.score = score


class YuNetDetector:
    """YuNet face detector via OpenCV's `cv2.FaceDetectorYN_create` wrapper.

    We deliberately don't drive YuNet through `baba_core.onnx_session` like
    DINOv2 / AuraFace do: the YuNet 2023mar ONNX has the priors + NMS
    postprocess factored *out* of the graph (it ships as 3 raw tensors —
    loc/conf/iou) and reimplementing that prior-decoding loop in Python
    would duplicate ~50 lines of cv2's C++ code for a model that already
    runs at ~2 ms on CPU. The OpenCV wrapper handles input letterboxing,
    the priors decode and NMS internally, and accepts an arbitrary
    setInputSize() at call time — so we feed it the *original* body crop
    dimensions and it returns face boxes in those same coordinates.

    GPU acceleration for YuNet specifically goes through cv2.dnn's CUDA
    backend (set when opencv is built with CUDA — not the case for the
    default opencv-python-headless wheel). The cost of leaving YuNet on
    CPU is ~2-5 ms per face crop; AuraFace's ~80 ms is the bigger lever
    and that one rides the variant's execution target.
    """

    def __init__(self, model_path: Path) -> None:
        import cv2  # deferred so non-inference services can import this module

        # The constructor always sets a target, and OpenCV 5's graph engine —
        # CPU-only, which is what runs here anyway — warns that it ignores it.
        prev_level = cv2.utils.logging.getLogLevel()
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        try:
            self._detector = cv2.FaceDetectorYN_create(
                str(model_path),
                "",  # config (unused for ONNX)
                (224, 224),  # placeholder size; setInputSize() per call
                score_threshold=MIN_FACE_SCORE,
                nms_threshold=0.3,
                top_k=10,
            )
        finally:
            cv2.utils.logging.setLogLevel(prev_level)
        self._provider = "cv2.dnn (CPU)"
        log.info("yunet ready on %s | path=%s", self._provider, model_path.name)

    def detect(self, image_rgb: np.ndarray) -> FaceDetection | None:
        import cv2

        h, w = image_rgb.shape[:2]
        # cv2 wants BGR. Our crops are RGB.
        bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
        # Wrapper requires explicit size before each detect() because
        # the underlying priors depend on it. Cheap call; no allocations.
        self._detector.setInputSize((w, h))
        _retval, faces = self._detector.detect(bgr)
        if faces is None or len(faces) == 0:
            return None
        # `faces` shape: (N, 15) — [x, y, w, h, 5*landmark_xy, score].
        # Pick the largest face above MIN_FACE_SIDE_PX.
        bboxes = faces[:, :4]
        areas = bboxes[:, 2] * bboxes[:, 3]
        order = np.argsort(-areas)
        for idx in order:
            x, y, bw, bh = bboxes[idx]
            if min(bw, bh) < MIN_FACE_SIDE_PX:
                continue
            x1 = max(0.0, float(x))
            y1 = max(0.0, float(y))
            x2 = min(float(w), float(x + bw))
            y2 = min(float(h), float(y + bh))
            lm = faces[idx, 4:14].reshape(5, 2).astype(np.float32).copy()
            # Reject back-of-head / hair false positives (top-down cameras).
            if not landmarks_look_frontal(lm, (x1, y1, x2, y2)):
                continue
            return FaceDetection(
                bbox=(x1, y1, x2, y2),
                landmarks=lm,
                score=float(faces[idx, -1]),
            )
        return None


class SCRFDDetector:
    """SCRFD face detector via onnxruntime.

    Drop-in alternative to YuNet with the same `detect()` contract. SCRFD
    is the InsightFace team's purpose-built face detector — it restructures
    the FCOS framework with sample-and-computation-redistribution to chase
    every FLOP into the 32-px-and-down face range. On WIDER FACE hard the
    10G variant beats YuNet by ~3 pp and the 34G variant by ~5 pp, which
    matters most on surveillance scenes where faces are small / partly
    occluded.

    The ONNX graph emits 9 tensors (3 strides × {score, bbox, kps}):
        score_8, score_16, score_32       — sigmoid prob per anchor
        bbox_8, bbox_16, bbox_32          — (l, t, r, b) distances from
                                            anchor center, scaled by stride
        kps_8,  kps_16,  kps_32           — 5 keypoint deltas, same scaling
    Postprocess is anchor decode + per-class top-k + NMS, all in numpy.
    Inputs are NCHW, BGR, mean-subtract 127.5, divide 128.

    Landmark order matches ArcFace (left eye, right eye, nose tip,
    left mouth corner, right mouth corner) — the standard insightface
    keypoint convention. No reindexing needed for downstream alignment.
    """

    # Three strides emitted by every SCRFD variant we register.
    _STRIDES = (8, 16, 32)
    # 2 anchors per location for all SCRFD variants with KPS heads.
    _NUM_ANCHORS = 2

    def __init__(
        self,
        model_path: Path,
        input_size: int | None = None,
        name: str = "scrfd",
    ) -> None:
        from baba_core.onnx_session import make_session

        self._session, self._provider = make_session(
            model_path,
            intra_op_threads=4,
            name=name,
        )
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        self._output_names = [o.name for o in self._session.get_outputs()]
        # SCRFD ONNX exports come in two flavours:
        #   - dynamic: input shape is ('batch', 3, ?, ?) — caller picks size
        #   - fixed: input shape is (1, 3, 640, 640) — must feed exactly that
        # Detect at load time and use whatever the graph requires. Fixed-shape
        # 34G in particular hard-bakes 640; feeding 320 throws
        # INVALID_ARGUMENT at first run.
        shape = list(inp.shape) if inp.shape else []
        fixed_h = shape[2] if len(shape) >= 4 and isinstance(shape[2], int) else None
        fixed_w = shape[3] if len(shape) >= 4 and isinstance(shape[3], int) else None
        if fixed_h is not None and fixed_w is not None:
            self._input_size = max(fixed_h, fixed_w)
            input_kind = "fixed"
        else:
            self._input_size = input_size if input_size is not None else 320
            input_kind = "dynamic"
        self._center_cache: dict[tuple[int, int, int], np.ndarray] = {}
        log.info(
            "%s ready on %s | path=%s input=%d (%s)",
            name,
            self._provider,
            model_path.name,
            self._input_size,
            input_kind,
        )

    def _anchor_centers(self, h: int, w: int, stride: int) -> np.ndarray:
        key = (h, w, stride)
        cached = self._center_cache.get(key)
        if cached is not None:
            return cached
        # Grid of (cy, cx) per stride, expanded for _NUM_ANCHORS.
        ys, xs = np.mgrid[:h, :w]
        centers = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
        centers = centers.reshape(-1, 2)
        if self._NUM_ANCHORS > 1:
            centers = np.repeat(centers, self._NUM_ANCHORS, axis=0)
        self._center_cache[key] = centers
        return centers

    def detect(self, image_rgb: np.ndarray) -> FaceDetection | None:
        import cv2

        h0, w0 = image_rgb.shape[:2]
        # Letterbox into input_size×input_size keeping aspect ratio so
        # face proportions don't distort.
        scale = self._input_size / max(h0, w0)
        nh, nw = round(h0 * scale), round(w0 * scale)
        resized = cv2.resize(image_rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((self._input_size, self._input_size, 3), dtype=np.uint8)
        canvas[:nh, :nw] = resized
        # BGR, mean-subtract 127.5, /128, NCHW.
        bgr = cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR)
        x = (bgr.astype(np.float32) - 127.5) / 128.0
        x = x.transpose(2, 0, 1)[None, ...]

        outs = self._session.run(self._output_names, {self._input_name: x})
        # Outputs come back in name order. We sort by stride so the
        # postprocess loop doesn't depend on session output ordering.
        out_by_name = dict(zip(self._output_names, outs, strict=False))

        scores_all: list[np.ndarray] = []
        bboxes_all: list[np.ndarray] = []
        kpss_all: list[np.ndarray] = []
        for stride in self._STRIDES:
            score_t = next(
                (
                    out_by_name[n]
                    for n in self._output_names
                    if n.endswith(f"_{stride}") and "score" in n.lower()
                ),
                None,
            )
            bbox_t = next(
                (
                    out_by_name[n]
                    for n in self._output_names
                    if n.endswith(f"_{stride}") and "bbox" in n.lower()
                ),
                None,
            )
            kps_t = next(
                (
                    out_by_name[n]
                    for n in self._output_names
                    if n.endswith(f"_{stride}") and ("kps" in n.lower() or "landmark" in n.lower())
                ),
                None,
            )
            if score_t is None or bbox_t is None:
                # Fallback for SCRFD ONNX exports that use generic output
                # names (e.g. 447/450/453). Order by FLOPs convention:
                # the first 3 outputs are scores, next 3 are bboxes,
                # last 3 are kps.
                idx = self._STRIDES.index(stride)
                score_t = outs[idx]
                bbox_t = outs[idx + 3]
                kps_t = outs[idx + 6] if len(outs) >= 9 else None

            scores = score_t.reshape(-1)
            bbox_preds = bbox_t.reshape(-1, 4) * stride
            grid_n = scores.shape[0] // self._NUM_ANCHORS
            # Square feature map assumption: SCRFD always produces hxw
            # where h=w=input_size/stride.
            side = int(np.sqrt(grid_n))
            centers = self._anchor_centers(side, side, stride)
            # Decode bbox: (cx - l, cy - t, cx + r, cy + b)
            x1 = centers[:, 0] - bbox_preds[:, 0]
            y1 = centers[:, 1] - bbox_preds[:, 1]
            x2 = centers[:, 0] + bbox_preds[:, 2]
            y2 = centers[:, 1] + bbox_preds[:, 3]
            bboxes = np.stack([x1, y1, x2, y2], axis=1)
            scores_all.append(scores)
            bboxes_all.append(bboxes)
            if kps_t is not None:
                kps_preds = kps_t.reshape(-1, 10) * stride
                kpss = centers[:, None, :].repeat(5, axis=1)
                kpss = kpss + kps_preds.reshape(-1, 5, 2)
                kpss_all.append(kpss)
            else:
                kpss_all.append(np.zeros((scores.shape[0], 5, 2), dtype=np.float32))

        scores_v = np.concatenate(scores_all, axis=0)
        bboxes_v = np.concatenate(bboxes_all, axis=0)
        kpss_v = np.concatenate(kpss_all, axis=0)

        keep = scores_v >= MIN_FACE_SCORE
        if not np.any(keep):
            return None
        scores_v = scores_v[keep]
        bboxes_v = bboxes_v[keep]
        kpss_v = kpss_v[keep]

        # Greedy NMS (numpy). SCRFD is anchor-dense so this matters.
        order = scores_v.argsort()[::-1]
        keep_idx: list[int] = []
        x1, y1, x2, y2 = bboxes_v[:, 0], bboxes_v[:, 1], bboxes_v[:, 2], bboxes_v[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        while order.size > 0:
            i = order[0]
            keep_idx.append(int(i))
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0, xx2 - xx1 + 1) * np.maximum(0, yy2 - yy1 + 1)
            iou = inter / (areas[i] + areas[order[1:]] - inter)
            order = order[1:][iou < 0.4]
        if not keep_idx:
            return None
        bboxes_v = bboxes_v[keep_idx]
        kpss_v = kpss_v[keep_idx]
        scores_v = scores_v[keep_idx]

        # Map back from letterboxed input_size coords to original image.
        bboxes_v = bboxes_v / scale
        kpss_v = kpss_v / scale

        # Pick the largest face above MIN_FACE_SIDE_PX.
        widths = bboxes_v[:, 2] - bboxes_v[:, 0]
        heights = bboxes_v[:, 3] - bboxes_v[:, 1]
        areas = widths * heights
        order = np.argsort(-areas)
        for idx in order:
            bw, bh = widths[idx], heights[idx]
            if min(bw, bh) < MIN_FACE_SIDE_PX:
                continue
            x1f = max(0.0, float(bboxes_v[idx, 0]))
            y1f = max(0.0, float(bboxes_v[idx, 1]))
            x2f = min(float(w0), float(bboxes_v[idx, 2]))
            y2f = min(float(h0), float(bboxes_v[idx, 3]))
            lmk = kpss_v[idx].astype(np.float32).copy()
            # Reject back-of-head / hair false positives (top-down cameras).
            if not landmarks_look_frontal(lmk, (x1f, y1f, x2f, y2f)):
                continue
            return FaceDetection(
                bbox=(x1f, y1f, x2f, y2f),
                landmarks=lmk,
                score=float(scores_v[idx]),
            )
        return None


# --- alignment + embedding ----------------------------------------------


def align_face(image_rgb: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    """Affine-warp the image so the 5 landmarks land on the canonical
    ArcFace template. Output is 112x112 RGB ready for AuraFace input."""
    import cv2

    src = landmarks.astype(np.float32)
    if src.shape != (5, 2):
        raise ValueError(f"expected 5x2 landmarks, got {src.shape}")
    # cv2.estimateAffinePartial2D returns (matrix, inliers). Partial2D
    # = similarity (rotation + uniform scale + translation), which is
    # the ArcFace convention.
    M, _inliers = cv2.estimateAffinePartial2D(
        src,
        _ARCFACE_TEMPLATE,
        method=cv2.LMEDS,
    )
    if M is None:
        # Estimation failed (degenerate landmarks); fall back to a
        # plain centre-crop resize so the embedder still gets *something*.
        return cv2.resize(
            image_rgb, (FACE_INPUT_SIZE, FACE_INPUT_SIZE), interpolation=cv2.INTER_AREA
        )
    warped = cv2.warpAffine(
        image_rgb,
        M,
        (FACE_INPUT_SIZE, FACE_INPUT_SIZE),
        borderValue=(0, 0, 0),
    )
    return warped


class ArcFaceStyleEmbedder:
    """Generic ONNX embedder for ArcFace-family face recognition models.

    AuraFace, TopoFR, ArcFace original and most ResNet-backbone ArcFace
    descendants share the same I/O contract: (B, 3, 112, 112) float32
    normalised to roughly [-1, 1], output (B, 512) float32. They differ
    only in the normalisation divisor — AuraFace uses /128.0 while the
    InsightFace-lineage models (TopoFR included) use /127.5. That's a
    ~1% pixel scale difference and the preprocess strategy is selected
    per-model in `baba_core.face_models`.

    Output is L2-normalised here so the rest of the pipeline can treat
    cosine similarity as a plain dot product.
    """

    dim = FACE_EMBEDDING_DIM

    def __init__(
        self,
        model_path: Path,
        preprocess: str,
        name: str = "face_embed",
    ) -> None:
        from baba_core.face_models import PREPROC_ARCFACE_CLASSIC, PREPROC_AURAFACE
        from baba_core.onnx_session import make_session

        if preprocess == PREPROC_AURAFACE:
            self._sub = 127.5
            self._div = 128.0
        elif preprocess == PREPROC_ARCFACE_CLASSIC:
            self._sub = 127.5
            self._div = 127.5
        else:
            raise ValueError(f"unsupported face preprocess: {preprocess!r}")

        self._session, self._provider = make_session(
            model_path,
            intra_op_threads=4,
            name=name,
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name
        log.info(
            "%s ready on %s | path=%s preprocess=%s",
            name,
            self._provider,
            model_path.name,
            preprocess,
        )

    def embed(self, aligned_112: np.ndarray) -> np.ndarray:
        # Batch of one (face hits are sparse — no batching gain). float32
        # NCHW with the model-specific normalisation set at __init__.
        if aligned_112.shape[:2] != (FACE_INPUT_SIZE, FACE_INPUT_SIZE):
            raise ValueError(f"expected 112x112 input, got {aligned_112.shape}")
        x = aligned_112.astype(np.float32)
        x = (x - self._sub) / self._div
        x = x.transpose(2, 0, 1)[None, ...]
        out = self._session.run([self._output_name], {self._input_name: x})[0]
        vec = out[0].astype(np.float32)
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        return vec


# --- public factory + one-shot helper -----------------------------------


class FaceStack:
    """Bundles detector + embedder so callers have a single object to
    pass around. `embed_from_crop` returns (embedding, bbox, score) or
    None when no face was detected / face was too small to be useful."""

    def __init__(self, detector: FaceDetector, embedder: FaceEmbedder) -> None:
        self.detector = detector
        self.embedder = embedder

    def embed_from_crop(
        self,
        image_rgb: np.ndarray,
    ) -> tuple[np.ndarray, tuple[float, float, float, float], float] | None:
        det = self.detector.detect(image_rgb)
        if det is None:
            return None
        aligned = align_face(image_rgb, det.landmarks)
        vec = self.embedder.embed(aligned)
        return vec, det.bbox, det.score


def _make_detector(
    *,
    detector_key: str,
    models_dir: Path,
    yunet_path: Path,
) -> tuple[FaceDetector | None, str]:
    """Resolve and load a detector by registry key, with graceful fallback
    to YuNet when a BYOM file is missing. Returns (detector, resolved_key)."""
    from baba_core.face_detectors import (
        DEFAULT_FACE_DETECTOR,
        FACE_DETECTORS,
        get_face_detector,
        resolve_detector_path,
    )

    try:
        spec = get_face_detector(detector_key)
    except KeyError as e:
        log.error("%s; falling back to %s", e, DEFAULT_FACE_DETECTOR)
        spec = FACE_DETECTORS[DEFAULT_FACE_DETECTOR]

    # YuNet path may differ from spec.model_filename if the caller passed
    # an explicit yunet_path — use it for backwards compatibility with
    # BABA_FACE_DETECTOR_MODEL env var.
    if spec.kind == "yunet":
        path = yunet_path if yunet_path else resolve_detector_path(spec, models_dir)
    else:
        path = resolve_detector_path(spec, models_dir)

    if not path.exists():
        if spec.key == DEFAULT_FACE_DETECTOR:
            log.warning(
                "default face detector %s missing at %s — face stack disabled. "
                "Run services/embedder/scripts/fetch_face_models.sh.",
                spec.key,
                path,
            )
            return None, spec.key
        log.warning(
            "BYOM face detector %s requested but file missing at %s — "
            "falling back to %s. See baba_core.face_detectors for "
            "download instructions.",
            spec.key,
            path,
            DEFAULT_FACE_DETECTOR,
        )
        spec = FACE_DETECTORS[DEFAULT_FACE_DETECTOR]
        path = yunet_path if yunet_path else resolve_detector_path(spec, models_dir)
        if not path.exists():
            return None, spec.key

    # The model file exists (checked above), so an init failure is a broken
    # model or accelerator, never a legitimate "no face models" disable: it
    # propagates, or enrollment would silently drop to body-only and store
    # face-less references.
    if spec.kind == "yunet":
        detector: FaceDetector = YuNetDetector(path)
    elif spec.kind == "scrfd":
        detector = SCRFDDetector(path, name=spec.key)
    else:
        raise ValueError(f"unsupported detector kind: {spec.kind!r}")
    return detector, spec.key


def make_face_stack_for_model(
    *,
    yunet_path: Path,
    models_dir: Path,
    model_key: str,
    detector_key: str = "yunet",
) -> tuple[FaceStack | None, str, str]:
    """Build a face stack using the detector named `detector_key` and the
    embedder named `model_key`.

    Returns (stack, resolved_detector_key, resolved_model_key). The detector
    key may differ from the requested one when a BYOM detector file is missing
    and YuNet stands in; the embedder never does, because another model's
    vectors are not comparable to the enrolled references.

    Returns (None, …, …) when the face pipeline is fully disabled
    (default models missing).
    """
    from baba_core.face_models import (
        DEFAULT_FACE_MODEL,
        get_face_model,
        resolve_model_path,
    )

    detector, resolved_detector_key = _make_detector(
        detector_key=detector_key,
        models_dir=models_dir,
        yunet_path=yunet_path,
    )
    if detector is None:
        return None, resolved_detector_key, model_key

    try:
        spec = get_face_model(model_key)
    except KeyError as e:
        raise RuntimeError(
            f"{e}; refusing to fall back to {DEFAULT_FACE_MODEL!r}: its embeddings "
            "are not comparable to the enrolled references. Change the active "
            "model in face recognition settings."
        ) from e

    embedder_path = resolve_model_path(spec, models_dir)
    if not embedder_path.exists():
        if spec.key == DEFAULT_FACE_MODEL:
            log.warning(
                "default face embedder %s missing at %s — face stack disabled. "
                "Run services/embedder/scripts/fetch_face_models.sh.",
                spec.key,
                embedder_path,
            )
            return None, resolved_detector_key, spec.key
        # No silent fallback to a different embedder: the embedding space is
        # the model. Enrolled references and stored track vectors were computed
        # under `model_key`; quietly swapping in the default here would compare
        # vectors from two spaces and put wrong names on people with no signal
        # that anything changed. A missing configured model is an operator
        # error — fail loud so it is fixed, not absorbed.
        raise RuntimeError(
            f"face embedder {spec.key!r} is configured but its model file is "
            f"missing at {embedder_path}. Refusing to fall back to "
            f"{DEFAULT_FACE_MODEL!r}: embeddings from a different model are not "
            "comparable to the enrolled references. Install the model (see "
            "services/embedder/scripts/fetch_face_models.sh) or change the "
            "active model in face recognition settings."
        )

    # Model file exists; an init failure propagates (see _make_detector).
    embedder = ArcFaceStyleEmbedder(
        embedder_path,
        preprocess=spec.preprocess,
        name=spec.key,
    )

    # Report BOTH licences. This line used to print only the embedder's, so a
    # stack of SCRFD (non-commercial weights) + AuraFace logged
    # "license=apache-2.0" — a build asserting it was commercial-clean while
    # running weights that aren't. The licence of a face stack is the licence of
    # its most restrictive part, and an operator reading one field cannot know
    # that. Loud rather than silent: shipping the wrong weights is not a runtime
    # failure that surfaces on its own.
    from baba_core.face_detectors import FACE_DETECTORS

    det_spec = FACE_DETECTORS.get(resolved_detector_key)
    det_license = det_spec.license if det_spec else "unknown"
    log.info(
        "face stack ready | detector=%s (%s) embedder=%s (%s, %s)",
        resolved_detector_key,
        det_license,
        spec.key,
        spec.name,
        spec.license,
    )
    unclean = [
        f"{k} ({lic})"
        for k, lic in ((resolved_detector_key, det_license), (spec.key, spec.license))
        if lic not in _COMMERCIAL_CLEAN_LICENSES
    ]
    if unclean:
        log.warning(
            "face stack is NOT commercial-clean: %s. Fine for a BYOM/private "
            "deployment — the operator brought these weights themselves — but "
            "this build must not be redistributed as-is. Commercial-clean "
            "default is detector=yunet + embedder=auraface.",
            ", ".join(unclean),
        )
    return FaceStack(detector, embedder), resolved_detector_key, spec.key
