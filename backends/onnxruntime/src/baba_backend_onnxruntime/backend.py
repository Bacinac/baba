from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
from baba_core import (
    BackendConfig,
    DeviceInfo,
    InferenceBackend,
    InputTensorInfo,
    ModelInput,
    ModelOutput,
    Precision,
)
from baba_core.onnx_session import check_bound_provider, ort_provider

log = logging.getLogger(__name__)


class ONNXRuntimeBackend(InferenceBackend):
    """The cpu variant's detector backend: onnxruntime on the one execution
    provider the build variant ships (see baba_core.onnx_session)."""

    name = "onnxruntime"

    def __init__(self) -> None:
        self._session: object | None = None
        self._config: BackendConfig | None = None
        self._device: DeviceInfo | None = None
        self._input_names: list[str] = []
        self._output_names: list[str] = []

    @classmethod
    def is_available(cls) -> bool:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def enumerate_devices(cls) -> list[DeviceInfo]:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        devices: list[DeviceInfo] = []
        for idx, provider in enumerate(providers):
            devices.append(
                DeviceInfo(
                    backend=cls.name,
                    device_id=idx,
                    device_name=provider,
                    supported_precisions=(Precision.FP32, Precision.FP16),
                )
            )
        return devices

    def load(self, model_path: Path, config: BackendConfig) -> None:
        import onnxruntime as ort

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # ORT tries to set pthread affinity by default which fails in containers
        # (cgroup CPU set != system CPU set). Bound to (os.cpu_count() // 2)
        # threads — leaves CPU room for I/O/decode work in the same container.
        cpu_count = os.cpu_count() or 1
        sess_opts.intra_op_num_threads = max(1, cpu_count // 2)
        sess_opts.inter_op_num_threads = 1
        if config.cache_dir:
            config.cache_dir.mkdir(parents=True, exist_ok=True)
            sess_opts.add_session_config_entry(
                "session.optimized_model_filepath",
                str(config.cache_dir / f"{model_path.stem}.optimized.onnx"),
            )

        provider = ort_provider()
        # CUDA takes the configured device; the CPU EP has no device option.
        entry = (
            (provider, {"device_id": config.device_id})
            if provider == "CUDAExecutionProvider"
            else provider
        )
        log.info(
            "Loading %s with provider=%s precision=%s",
            model_path.name,
            provider,
            config.precision,
        )
        self._session = ort.InferenceSession(
            str(model_path), sess_options=sess_opts, providers=[entry]
        )
        check_bound_provider(self._session, provider, f"onnxruntime backend ({model_path.name})")
        self._input_names = [i.name for i in self._session.get_inputs()]
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._config = config
        self._device = DeviceInfo(
            backend=self.name,
            device_id=config.device_id,
            device_name=provider,
            supported_precisions=(Precision.FP32, Precision.FP16),
        )

    def warmup(self, batch_size: int = 1) -> None:
        if self._session is None:
            raise RuntimeError("Backend not loaded. Call load() first.")
        # Common DETR-family default. Use 640 for dynamic spatial dims so the
        # encoder stride (typically 32) divides cleanly; using 1 here breaks
        # internal concats that depend on multi-scale feature pyramids.
        default_spatial = 640
        inputs = {}
        for inp in self._session.get_inputs():
            shape: list[int] = []
            for d in inp.shape:
                if isinstance(d, int):
                    shape.append(d)
                elif d in ("batch_size", "batch") or d is None:
                    shape.append(batch_size)
                elif (
                    d in ("height", "width", "image_size")
                    or "height" in str(d)
                    or "width" in str(d)
                ):
                    shape.append(default_spatial)
                else:
                    shape.append(batch_size)
            # Map the real ORT input type — the old float/int64 heuristic fed
            # int64 into a uint8 (nv12/rgb-baked) input, crashing warmup with
            # "Unexpected input data type ... expected tensor(uint8)".
            dtype = {
                "tensor(float)": np.float32,
                "tensor(float16)": np.float16,
                "tensor(uint8)": np.uint8,
                "tensor(int8)": np.int8,
                "tensor(int32)": np.int32,
                "tensor(int64)": np.int64,
                "tensor(bool)": np.bool_,
            }.get(inp.type, np.float32)
            inputs[inp.name] = np.zeros(shape, dtype=dtype)
        self._session.run(self._output_names, inputs)

    def infer(self, inputs: ModelInput) -> ModelOutput:
        if self._session is None:
            raise RuntimeError("Backend not loaded. Call load() first.")
        feed = {self._input_names[0]: inputs} if isinstance(inputs, np.ndarray) else inputs
        outputs = self._session.run(self._output_names, feed)
        if len(outputs) == 1:
            return outputs[0]
        return outputs

    def input_tensors(self) -> list[InputTensorInfo]:
        if self._session is None:
            raise RuntimeError("Backend not loaded.")
        # ORT reports types as strings like "tensor(float)" / "tensor(uint8)";
        # map a few common ones. Shape entries that are symbolic (str) or
        # None are treated as dynamic (-1).
        type_map = {
            "tensor(float)": np.dtype(np.float32),
            "tensor(float16)": np.dtype(np.float16),
            "tensor(uint8)": np.dtype(np.uint8),
            "tensor(int8)": np.dtype(np.int8),
            "tensor(int32)": np.dtype(np.int32),
            "tensor(int64)": np.dtype(np.int64),
            "tensor(bool)": np.dtype(np.bool_),
        }
        out: list[InputTensorInfo] = []
        for inp in self._session.get_inputs():
            dtype = type_map.get(inp.type, np.dtype(np.float32))
            shape = tuple(-1 if not isinstance(d, int) else d for d in inp.shape)
            out.append(InputTensorInfo(name=inp.name, dtype=dtype, shape=shape))
        return out

    def unload(self) -> None:
        self._session = None
        self._device = None
        self._config = None

    @property
    def device_info(self) -> DeviceInfo:
        if self._device is None:
            raise RuntimeError("Backend not loaded")
        return self._device
