"""Reading plates off a native frame: models, licences, and where to look.

Two models in series — find the plate, then read it — the same shape as the
face stack, and registered the same way so the operator can see what they are
running and under what terms.

Both are BYOM. The code either side is MIT, but neither project states a licence
for its trained weights and the detector's are YOLOv9-derived, whose reference
implementation is GPL-3.0. That is the SCRFD situation again: code and weights
are separate grants and the second one is the one a customer's lawyer reads. So
BABA does not ship these; the operator brings them, exactly as with TopoFR.

The measurement that shaped this module, taken on this system's own footage
(west, 25.07 arrival):

  * On the PARKED car, oblique and 46 px wide, thirty readings produced
    twenty-four different strings. Unusable, and no confidence threshold
    rescues it — one position agreed unanimously and was wrong.
  * On the APPROACH, at 96-183 px, the plate read ZG9420GZ at confidence 1.0
    on seven consecutive frames.

Two consequences are baked in below. The window is about two seconds wide, so
sampling has to be dense rather than one-frame-per-track. And it has to come
from the recorded segment: `cameras.downscale_max_edge` caps west at 1920 from
a native 4096, which would turn those 96-183 px back into 45-86 and put the
reading back in the unusable band it just escaped.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

__all__ = [
    "PlateReading",
    "PlateStack",
    "make_plate_stack",
]

# All four models are BYOM tier: code MIT, weights without a licence statement
# (the detectors derive from YOLOv9, whose reference implementation is
# GPL-3.0). Licence detail lives in LICENSES.md.
#
# Defaults are the measured winners on this property's footage. The s-608
# detector found the plate on every frame of the measured arrival at
# 0.88-0.93 — localisation was never the weak link. The GLOBAL OCR model
# read ZG9420GZ at confidence 1.0 on seven consecutive approach frames;
# the European-specific model, the obvious first guess for Croatian plates,
# measured WORSE on the same frames (0.77-0.79, one digit wrong on the
# closest frame).
_DETECTOR_HUB = {
    "yolov9_s_608": "yolo-v9-s-608-license-plate-end2end",
    "yolov9_t_384": "yolo-v9-t-384-license-plate-end2end",
}
_OCR_HUB = {
    "cct_s_v1_global": "cct-s-v1-global-model",
    "european_vit_v2": "european-plates-mobile-vit-v2-model",
}


@dataclass(frozen=True)
class PlateReading:
    text: str
    confidence: float
    box: tuple[int, int, int, int]
    detection_score: float

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]


class PlateStack:
    """Detector + OCR over one image, returning every plate it found."""

    def __init__(self, detector: Any, ocr: Any) -> None:
        self._detector = detector
        self._ocr = ocr

    def read(self, rgb: np.ndarray) -> list[PlateReading]:
        """Every plate in `rgb`, largest first.

        `rgb` should be a NATIVE-resolution crop around the vehicle, not a
        whole frame: the detector resizes its input to 608, so a plate in a
        4096-wide frame arrives as a dozen pixels and is never found. Cropping
        to the vehicle first is what preserves it.
        """
        bgr = rgb[:, :, ::-1]
        out: list[PlateReading] = []
        for det in self._detector.predict(bgr):
            b = det.bounding_box
            x1, y1, x2, y2 = int(b.x1), int(b.y1), int(b.x2), int(b.y2)
            if x2 <= x1 or y2 <= y1:
                continue
            crop = bgr[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            result = self._ocr.run(crop, return_confidence=True)
            text, conf = _unpack_ocr(result)
            if not text:
                continue
            out.append(
                PlateReading(
                    text=text,
                    confidence=conf,
                    box=(x1, y1, x2, y2),
                    detection_score=float(det.confidence),
                )
            )
        out.sort(key=lambda r: r.width, reverse=True)
        return out


def _unpack_ocr(result: Any) -> tuple[str, float]:
    """Pull the text and a confidence out of whatever fast-plate-ocr returned.

    It hands back a batch of prediction objects carrying `.plate` and
    `.char_probs`, and older shapes returned a (texts, probs) pair. Both are
    handled explicitly rather than by falling through to `str(result)`, which
    is what happened first and silently turned every reading into the repr of a
    dataclass — the consensus came out as
    'PLATEPREDICTIONPLATEZG3720GZCHARPROBSNONE...' and matched nothing.

    The attribute is read WITHOUT a default. It was spelled `charprobs` here
    for a while and `getattr(..., None)` swallowed the typo in silence, so every
    reading scored 0.00 and the score column carried nothing at all. A missing
    attribute is a wrong assumption about the library and should say so.
    """
    first = result[0] if isinstance(result, (list, tuple)) and result else result
    text_raw = getattr(first, "plate", None)
    if text_raw is not None:
        probs = first.char_probs
    elif isinstance(result, tuple) and len(result) == 2:
        texts, probs_batch = result
        text_raw = texts[0] if isinstance(texts, (list, tuple)) else texts
        probs = probs_batch[0] if probs_batch is not None else None
    else:
        return "", 0.0
    text = str(text_raw).strip().replace("_", "")
    conf = 0.0
    if probs is not None:
        with contextlib.suppress(Exception):
            conf = float(np.mean(np.asarray(probs, dtype=np.float32)))
    return text, conf


def make_plate_stack(
    detector_key: str,
    ocr_key: str,
    *,
    models_dir: Path,
) -> PlateStack | None:
    """Load both models, or return None when they are not present.

    Returns None rather than raising because plate reading is optional: a
    deployment that has not brought the weights should read no plates, not fail
    to start. The caller logs the absence once.

    The weights are cached under `<models_dir>/cache`, not `<models_dir>`
    itself: services mount the models directory read-only and only the cache
    subdirectory is writable, which is also where the compiled native engines
    live. `HOME` is what steers the download — the two libraries ignore
    XDG_CACHE_HOME, so pointing that at a mounted path silently achieves
    nothing.

    A cache that cannot be created disables plate reading and says so, rather
    than taking the service down: whatever else this process is doing is not
    optional, and plate reading is.
    """
    cache = models_dir / "cache" / "alpr"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("plate reading disabled — cannot write model cache %s: %s", cache, exc)
        return None
    os.environ["HOME"] = str(cache)

    try:
        import onnxruntime as ort
        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import create_detector
    except ImportError:
        log.info("plate reading disabled — fast-alpr is not installed in this image")
        return None

    det_hub = _DETECTOR_HUB.get(detector_key)
    ocr_hub = _OCR_HUB.get(ocr_key)
    if det_hub is None or ocr_hub is None:
        log.warning(
            "plate reading disabled — unknown model key(s) detector=%r ocr=%r",
            detector_key,
            ocr_key,
        )
        return None

    # CPU on purpose, and it is not a fallback. The accelerator is already at
    # its inference ceiling carrying the live detector, and past that ceiling
    # this pipeline sheds frames silently rather than slowing down — so a
    # background sweep must not compete for it. The work is small enough to
    # afford: a few hundred decoded frames per vehicle, minutes after the fact,
    # against nothing that is waiting.
    #
    # Passing BABA's own provider names through would ALSO have landed on CPU,
    # but by way of an "Unknown Provider Type: intel" error and a silent
    # fallback inside onnxruntime — the same thing, unstated.
    providers = ["CPUExecutionProvider"]
    opts = ort.SessionOptions()
    # Left to its default, onnxruntime pins one thread per physical core of the
    # host, and inside a container whose cpuset lacks those cores every pin fails
    # with a logged pthread_setaffinity_np error. A set count is never pinned.
    opts.intra_op_num_threads = 4
    try:
        detector = create_detector(det_hub, providers=providers, sess_options=opts)
        ocr = LicensePlateRecognizer(hub_ocr_model=ocr_hub, providers=providers, sess_options=opts)
    except Exception:
        log.exception("plate reading disabled — model load failed")
        return None
    log.info("plate stack ready: detector=%s ocr=%s", det_hub, ocr_hub)
    return PlateStack(detector, ocr)
