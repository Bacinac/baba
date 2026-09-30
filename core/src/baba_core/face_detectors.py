"""Face detector registry.

Parallel to `face_models.py` (which selects the embedder), this picks the
detector. Detection is the "where is the face / what are the 5 landmarks
for alignment" half of the pipeline. Together with the embedder choice,
the pair is exposed in the Settings UI as a swappable BYOM stack.

Shipping default is YuNet (Apache 2.0 code + weights, ~228 KB) — the only
detector with a fully commercial-clean weight license. Anything more
accurate (SCRFD, RetinaFace, YOLOv*-face) inherits a research-only or
GPL-3.0 weight constraint, so it can only be offered as BYOM.

BABA never redistributes BYOM detector weights. Users download them
themselves and BABA loads whatever it finds at the configured path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class FaceDetectorSpec:
    key: str  # short id used in env vars / DB / UI selector
    name: str  # human-readable
    description: str  # what this detector is, license posture
    license: str  # spdx-style or "unspecified"
    tier: str  # "default" | "byom"
    model_filename: str  # filename under /models/ — relative
    # Detector kind dispatches to the right wrapper class in face.py.
    # Each kind has its own ONNX I/O contract and postprocess:
    #   "yunet"  → cv2.FaceDetectorYN, postprocess inside OpenCV
    #   "scrfd"  → 3-stride FCOS-style with 5 keypoints, postprocess in Python
    kind: str
    bundled_with_baba: bool


FACE_DETECTORS: dict[str, FaceDetectorSpec] = {
    "yunet": FaceDetectorSpec(
        key="yunet",
        name="YuNet (2023mar)",
        description=(
            "Lightweight CNN face detector with 5-point landmarks, MIT "
            "license on both code AND weights. The default for BABA "
            "distributions. ~228 KB ONNX, WIDER FACE hard mAP ~0.80, "
            "~2 ms on CPU. "
            "⚠ MEASURED 2026-07-15 on an elevated patio camera: it fires "
            "on NON-FACES with high confidence — a shoulder scored 0.79, "
            "and of the six highest-scoring 'faces' on that camera not "
            "one was a face (shoulders, a t-shirt, backs of heads, an "
            "ear). 35% of person crops yielded a 'face'; SCRFD-10G on the "
            "same crops yielded 3.7%, of which ~8 in 11 were real. Its "
            "score is therefore useless for ranking a track's faces, "
            "which is what `landmarks_look_frontal` cannot fix: the guard "
            "checks that landmark geometry is PLAUSIBLE, and YuNet "
            "hallucinates plausible landmarks on a bald head. Frigate "
            "uses InsightFace buffalo_l (= SCRFD det_10g), which is why "
            "it recognises faces on cameras where this default does not. "
            "Adequate for close, frontal, cooperative capture; on an "
            "elevated or distant camera prefer SCRFD (BYOM — the weights "
            "are non-commercial, so BABA cannot ship them)."
        ),
        license="mit",
        tier="default",
        model_filename="face_yunet.onnx",
        kind="yunet",
        bundled_with_baba=True,
    ),
    "scrfd_10g": FaceDetectorSpec(
        key="scrfd_10g",
        name="SCRFD 10G (InsightFace)",
        description=(
            "Purpose-built face detector by the InsightFace team, "
            "10 GFLOPs mid-tier variant. WIDER FACE hard mAP ~0.83, "
            "+3 pp over YuNet on the same metric. ~16 MB ONNX. "
            "⚠ BYOM: code is MIT but weights are non-commercial — "
            "for personal/research use only without a separate commercial "
            "license from InsightFace. Download `det_10g.onnx` from the "
            "buffalo_l package at "
            "huggingface.co/immich-app/buffalo_l/blob/main/det_10g.onnx "
            "and place at /models/{model_filename}."
        ),
        license="non-commercial",
        tier="byom",
        model_filename="face_scrfd_10g.onnx",
        kind="scrfd",
        bundled_with_baba=False,
    ),
    "scrfd_34g": FaceDetectorSpec(
        key="scrfd_34g",
        name="SCRFD 34G (InsightFace)",
        description=(
            "InsightFace SCRFD large variant. WIDER FACE hard mAP ~0.85, "
            "+5 pp over YuNet. ~39 MB ONNX, slower (~15 ms CPU / ~4 ms "
            "CUDA). Same license posture as scrfd_10g (BYOM, "
            "non-commercial weights). Download `scrfd_34g.onnx` from "
            "huggingface.co/deepinsight/insightface and place at "
            "/models/{model_filename}."
        ),
        license="non-commercial",
        tier="byom",
        model_filename="face_scrfd_34g.onnx",
        kind="scrfd",
        bundled_with_baba=False,
    ),
}


DEFAULT_FACE_DETECTOR = "yunet"


def get_face_detector(key: str) -> FaceDetectorSpec:
    """Look up a face detector by its registry key. Raises KeyError with a
    helpful message listing available keys when the key isn't known."""
    try:
        return FACE_DETECTORS[key]
    except KeyError:
        raise KeyError(
            f"Unknown face detector {key!r}. "
            f"Registered: {sorted(FACE_DETECTORS.keys())}. "
            f"Set BABA_FACE_DETECTOR to one of these or extend "
            f"core/src/baba_core/face_detectors.py to add another."
        ) from None


def resolve_detector_path(spec: FaceDetectorSpec, models_dir: Path) -> Path:
    return models_dir / spec.model_filename
