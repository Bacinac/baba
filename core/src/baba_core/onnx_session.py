"""Inference sessions for the small ONNX models (embedder, face, SAM2, tracker
ReID) on the one execution target this build's variant ships:

  nvidia → onnxruntime, CUDA execution provider
  intel  → the standalone OpenVINO runtime, on the GPU
  cpu    → onnxruntime, CPU execution provider

There is no fallback: a target that is missing or fails to initialise raises.
The next entry in any fallback chain is the CPU, and a GPU host quietly
running inference on its CPU pegs the box while every container still
reports healthy.

The detector does not come through here — its backend plugins (tensorrt /
openvino / onnxruntime) keep exactly one engine per service with a controlled
workspace, and pick among themselves by the same variant.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from baba_core.variant import build_variant

log = logging.getLogger(__name__)

# nvidia binds the CUDA EP, not TensorRT: the embedder and the api each load
# several small models through here, and a TRT engine per model per process
# holds gigabytes of workspace per CUDA context — enough to starve the GPU and
# push NVDEC in the ingestor off the card. Intel has no entry: it never reaches
# onnxruntime (see _openvino_session).
_ORT_PROVIDER = {
    "nvidia": "CUDAExecutionProvider",
    "cpu": "CPUExecutionProvider",
}


def ort_provider() -> str:
    """The onnxruntime execution provider for this build's variant."""
    variant = build_variant()
    if variant not in _ORT_PROVIDER:
        raise RuntimeError(f"BABA_VARIANT={variant} runs on OpenVINO, not onnxruntime")
    return _ORT_PROVIDER[variant]


def check_bound_provider(session: Any, provider: str, label: str) -> None:
    """Raise unless `session` actually runs on `provider`. onnxruntime does not
    raise when a GPU provider fails to initialise (a broken CUDA wheel, the
    nvidia_uvm trap): it logs a warning and runs the model on the CPU, and only
    get_providers() tells."""
    bound = session.get_providers()[0]
    if bound != provider:
        raise RuntimeError(
            f"{label}: {provider} failed to initialise and onnxruntime bound {bound} instead"
        )
    session.disable_fallback()


# ---------------------------------------------------------------------------
# Native OpenVINO session shim.
#
# On Intel builds we run the small ORT-driven models (dinov2 / osnet / scrfd /
# yunet / auraface / sam2) on the SAME standalone OpenVINO runtime the detector
# uses via backends/openvino — NOT onnxruntime-openvino. onnxruntime-openvino
# bundles a second OpenVINO runtime that must stay version-locked to the
# standalone `openvino` package; shipping both invites the exact silent
# version-clash we refuse to live with. One runtime = one source of truth.
#
# The shim exposes only the slice of onnxruntime.InferenceSession that BABA's
# consumers touch: get_inputs(), get_outputs(), get_providers(), run().


_NP_TO_ORT_TYPE: dict[Any, str] = {}


def _np_to_ort_type(dtype: Any) -> str:
    """Map a numpy dtype to onnxruntime's `tensor(...)` type string, so the
    shim's NodeArg.type reads like a real ORT NodeArg for any consumer that
    inspects it (none do today, but keep the contract honest)."""
    import numpy as np

    global _NP_TO_ORT_TYPE
    if not _NP_TO_ORT_TYPE:
        _NP_TO_ORT_TYPE = {
            np.dtype(np.float32): "tensor(float)",
            np.dtype(np.float16): "tensor(float16)",
            np.dtype(np.uint8): "tensor(uint8)",
            np.dtype(np.int8): "tensor(int8)",
            np.dtype(np.int32): "tensor(int32)",
            np.dtype(np.int64): "tensor(int64)",
            np.dtype(np.bool_): "tensor(bool)",
        }
    return _NP_TO_ORT_TYPE.get(dtype, "tensor(float)")


class _NodeArg:
    """Mimics onnxruntime.NodeArg. Consumers read `.name` everywhere and
    `.shape[0]` to detect a static batch dim; a dynamic dim is None (ORT
    reports it as a symbolic string / None — same `isinstance(x, int)` test
    the callers already use resolves it to 'dynamic')."""

    __slots__ = ("name", "shape", "type")

    def __init__(self, name: str, shape: list[int | None], type_: str) -> None:
        self.name = name
        self.shape = shape
        self.type = type_


class _OpenVINOSession:
    """Adapts a loaded OpenVINOBackend to the onnxruntime.InferenceSession
    surface BABA's embedder / face / reid / sam2 code calls. Owns the backend
    for the process lifetime (freed on interpreter exit)."""

    def __init__(self, backend: Any) -> None:
        import numpy as np

        self._backend = backend
        self._provider = "OpenVINOExecutionProvider"
        self._inputs = [
            _NodeArg(
                t.name,
                [None if int(d) == -1 else int(d) for d in t.shape],
                _np_to_ort_type(np.dtype(t.dtype)),
            )
            for t in backend.input_tensors()
        ]
        outs = backend.output_tensors()
        self._outputs = [
            _NodeArg(
                t.name,
                [None if int(d) == -1 else int(d) for d in t.shape],
                _np_to_ort_type(np.dtype(t.dtype)),
            )
            for t in outs
        ]
        # infer() returns outputs in this order — used to key run() results.
        self._output_order = [t.name for t in outs]

    def get_inputs(self) -> list[_NodeArg]:
        return self._inputs

    def get_outputs(self) -> list[_NodeArg]:
        return self._outputs

    def get_providers(self) -> list[str]:
        return [self._provider]

    def run(self, output_names: list[str] | None, input_feed: dict):
        result = self._backend.infer(input_feed)
        outs = result if isinstance(result, list) else [result]
        if output_names is None:
            return outs
        # strict: a backend returning a different number of outputs than the
        # graph declares is a real defect. Without it zip truncates silently and
        # the caller gets a KeyError on some unrelated name three lines later.
        by_name = dict(zip(self._output_order, outs, strict=True))
        return [by_name[n] for n in output_names]


def _openvino_session(model_path: Path, label: str) -> _OpenVINOSession:
    """Load `model_path` on the standalone OpenVINO backend. The backend binds
    the Intel GPU or raises; it never settles for the CPU."""
    from baba_core.backend import BackendConfig
    from baba_core.paths import StoragePaths
    from baba_core.registry import BACKENDS
    from baba_core.types import Precision

    # FP16 by default. The FP32 default this replaces was defensive — re-ID and
    # face distances are empirical constants and the worry was that FP16
    # rounding would move them under fixed thresholds. Measured 2026-07-25 on
    # 64 live production crops, both models, all pairwise distances:
    #
    #   OSNet  (threshold 0.30): shift median 0.0010, max 0.0076 — 1/2016 pairs
    #                            change side of the threshold
    #   TopoFR (threshold 0.75): shift median 0.0007, max 0.0097 — 2/2016
    #
    # The impostor distribution starts at 0.820 against a 0.75 gate, so the
    # decision margin is 0.07 — seven times the worst observed drift, and the
    # handful of pairs that flip sit exactly on the threshold where the call is
    # arbitrary anyway. Both backbones L2-normalise their output, which is what
    # keeps the drift this small.
    #
    # What FP32 cost, measured on the same box: TopoFR 116.6 ms vs 18.2, OSNet
    # 20.7 vs 6.0, SCRFD-320 27.6 vs 6.7. Note this default was also *worse
    # than not setting the hint at all* — the OV GPU plugin's own default is
    # f16, so the explicit f32 was actively opting into the slow path.
    # BABA_OPENVINO_PRECISION=fp32 restores the old behaviour.
    precision = (
        Precision.FP32
        if os.environ.get("BABA_OPENVINO_PRECISION", "fp16").strip().lower() == "fp32"
        else Precision.FP16
    )
    backend = BACKENDS.select(["openvino"])()
    backend.load(
        Path(model_path),
        BackendConfig(
            precision=precision,
            max_batch_size=32,
            cache_dir=StoragePaths.from_env().model_cache,
            # Small models (dinov2/osnet/scrfd/…) keep their own input sizes;
            # only flex the batch dim. Pinning other dynamic dims to the
            # detector's 640 build size would corrupt e.g. DINOv2's fully
            # dynamic [?,?,?,?] input.
            extra={"ov_batch_only_reshape": True},
        ),
    )
    backend.warmup(batch_size=1)
    log.info(
        "%s: native OpenVINO backend on %s (precision=%s)",
        label,
        backend.device_info.device_name,
        precision.value,
    )
    return _OpenVINOSession(backend)


def make_session(
    model_path: str | Path,
    *,
    intra_op_threads: int = 4,
    name: str | None = None,
):
    """Create an inference session for `model_path` on this build's execution
    target. Returns (session, provider_name), e.g. "CUDAExecutionProvider" —
    useful for logging and capability decisions ("can I batch large?")."""
    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(model_path)
    label = f"onnx[{name or model_path.stem}]"

    if build_variant() == "intel":
        return _openvino_session(model_path, label), "OpenVINOExecutionProvider"

    import onnxruntime as ort

    provider = ort_provider()
    available = ort.get_available_providers()
    if provider not in available:
        raise RuntimeError(
            f"{label}: this build needs {provider}, but onnxruntime only has {sorted(available)}"
        )
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = intra_op_threads
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(str(model_path), sess_options=opts, providers=[provider])
    check_bound_provider(session, provider, label)
    log.info("%s: using %s (path=%s)", label, provider, model_path.name)
    return session, provider
