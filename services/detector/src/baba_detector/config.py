from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from baba_core import build_variant, dsn_from_env

# The one detector backend each variant runs. The image for a variant ships
# exactly this plugin's runtime; there is no second choice to fall through to.
_BACKEND_FOR_VARIANT = {"nvidia": "tensorrt", "intel": "openvino", "cpu": "onnxruntime"}


@dataclass(slots=True, frozen=True)
class DetectorConfig:
    nats_url: str
    model_path: Path
    variant: str
    backend: str
    max_batch_size: int
    max_batch_wait_ms: int
    # Floor threshold the postprocessor uses before per-class / per-camera
    # rules are evaluated.  Set this LOW (e.g. 0.10) — the real gating is
    # done by DetectionRules in the main loop using DB-driven values that
    # the operator tunes via the UI.  The env var stays as the bottom of
    # the funnel so a freshly-deployed detector still publishes nothing
    # silly even before any rule row exists.
    conf_threshold: float
    # NOTE: the two-threshold MAINTAIN floor is per-camera now
    # (cameras.maintain_conf, read live via DetectionRules.maintain()) — no
    # global env. Retired BABA_DETECTOR_MAINTAIN_CONF.
    input_size: int
    device_id: int
    # OpenVINO detector precision opt-in ("f16" | "" = f32/ACCURACY). fp16 is
    # ~3x faster on the Arc but historically corrupted on SOME models (d-fine-m
    # post-first-call collapse), so it is per-deployment explicit and the OV
    # backend self-verifies stability at load — never a silent default.
    ov_precision: str
    # Detector head family: "dfine" (D-FINE / RT-DETR, 80-class, plain [0,1]
    # input) or "rfdetr" (RF-DETR / LWDETR, 91-class dets+labels output,
    # ImageNet-normalised input). Selects the postprocessor + on-device norm.
    family: str
    # Postgres DSN for live-reloadable rule rows.
    dsn: str

    @classmethod
    def from_env(cls) -> DetectorConfig:
        # NOTE: there is no BABA_CAMERAS knob. The detector subscribes to the
        # NATS wildcard `baba.frames.*`; the camera set is authoritative in
        # Postgres, not in an env var. (Older builds hard-failed without
        # BABA_CAMERAS even though the value was never used.)
        variant = build_variant()

        return cls(
            nats_url=os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
            model_path=Path(
                os.environ.get(
                    "BABA_DETECTOR_MODEL",
                    "/models/rtdetrv2-r18.onnx",
                )
            ),
            variant=variant,
            backend=_BACKEND_FOR_VARIANT[variant],
            max_batch_size=int(os.environ.get("BABA_DETECTOR_BATCH", "8")),
            max_batch_wait_ms=int(os.environ.get("BABA_DETECTOR_BATCH_WAIT_MS", "20")),
            conf_threshold=float(os.environ.get("BABA_DETECTOR_CONF", "0.10")),
            input_size=int(os.environ.get("BABA_DETECTOR_INPUT_SIZE", "640")),
            device_id=int(os.environ.get("BABA_DEVICE_ID", "0")),
            ov_precision=os.environ.get("BABA_DETECTOR_OV_PRECISION", "").strip().lower(),
            family=os.environ.get("BABA_DETECTOR_MODEL_FAMILY", "dfine").strip().lower(),
            dsn=dsn_from_env(),
        )
