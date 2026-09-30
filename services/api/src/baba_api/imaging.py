"""Safe decoding of user-uploaded images.

PIL decodes the entire pixel buffer into memory before the caller can downscale
it, so a small but well-compressed PNG that declares hundreds of megapixels is a
~GB allocation — a decompression-bomb / OOM vector against the single-process,
memory-capped api. PIL's own MAX_IMAGE_PIXELS only warns at ~178 MP and raises
at ~357 MP, leaving a dangerous 50–357 MP window. We close it by checking the
header-declared dimensions BEFORE any pixel decode.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

# Comfortably covers 8K stills (33 MP) and any real camera image; rejects the
# crafted-oversize decompression bombs.
MAX_DECODE_PIXELS = 50_000_000


def decode_rgb_capped(buf: bytes) -> np.ndarray | None:
    """Decode JPEG/PNG bytes to an HWC uint8 RGB array, rejecting images whose
    declared dimensions exceed MAX_DECODE_PIXELS before the pixel buffer is
    allocated. Returns None for unrecognised / corrupt / oversize payloads so
    callers can skip them without failing the whole batch."""
    try:
        img = Image.open(io.BytesIO(buf))
        # img.size comes from the header; no pixels decoded yet.
        w, h = img.size
        if w * h > MAX_DECODE_PIXELS:
            return None
        return np.array(img.convert("RGB"))  # HWC uint8 RGB
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
