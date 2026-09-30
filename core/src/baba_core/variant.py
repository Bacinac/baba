"""The build variant a service runs as — the one fact every backend choice
derives from.

Each service runs exactly one backend per job (decoder, detector, ONNX
execution provider), picked from its variant, and refuses to start when that
backend is unavailable. There are no preference chains: the next entry in a
chain is always slower and usually the CPU, and a GPU host that quietly moved
work onto its CPU chokes while every container still reports healthy.
"""

from __future__ import annotations

import os

VARIANTS = ("nvidia", "intel", "cpu")


def build_variant() -> str:
    """`BABA_VARIANT` as this process sees it. Compose sets it per service from
    that service's image variant (`BABA_<SERVICE>_VARIANT`, else the host's),
    so it names what the image actually ships. An unknown value is a
    configuration error, never a reason to guess."""
    v = os.environ.get("BABA_VARIANT", "cpu").strip().lower()
    if v not in VARIANTS:
        raise RuntimeError(f"BABA_VARIANT={v!r} is not one of: {', '.join(VARIANTS)}")
    return v


# Pixel format the ingestor's decoder writes into the frame ring. GPU variants
# do no colour work on the CPU: NV12 goes into the ring and the detector graph
# (nv12-baked, or OpenVINO's on-device PrePostProcessor) converts it on the GPU.
# The cpu variant decodes straight to RGB.
RING_PIXEL_FORMAT = {"nvidia": "nv12", "intel": "nv12", "cpu": "rgb"}
