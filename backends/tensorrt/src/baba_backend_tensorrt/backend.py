from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

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

log = logging.getLogger(__name__)


# TRT data types we need to map to numpy (nvinfer1::DataType).
_TRT_TO_NP: dict[Any, np.dtype] = {}


def _init_dtype_map() -> dict[Any, np.dtype]:
    import tensorrt as trt

    return {
        trt.DataType.FLOAT: np.dtype(np.float32),
        trt.DataType.HALF: np.dtype(np.float16),
        trt.DataType.INT32: np.dtype(np.int32),
        trt.DataType.INT64: np.dtype(np.int64),
        trt.DataType.INT8: np.dtype(np.int8),
        trt.DataType.UINT8: np.dtype(np.uint8),
        trt.DataType.BOOL: np.dtype(np.bool_),
    }


def _check(err: Any, label: str = "cuda") -> None:
    """cuda-python returns (error_code, ...) for every call. Crash loudly on err."""
    from cuda.bindings import runtime as cudart

    if err != cudart.cudaError_t.cudaSuccess:
        _, msg = cudart.cudaGetErrorString(err)
        raise RuntimeError(f"{label} failed: {msg.decode() if isinstance(msg, bytes) else msg}")


def _to_fp16(onnx_bytes: bytes) -> bytes:
    """TensorRT 11 networks are strongly typed: every layer runs in the type the
    ONNX graph gives it, so FP16 has to be an FP16 graph. The model's inputs and
    outputs keep their original types — the detector feeds and reads them as
    exported."""
    import warnings

    import onnx
    from onnxconverter_common import float16

    model = onnx.load_from_string(onnx_bytes)
    # The converter retypes a Cast's output to float16 but leaves its `to=float`,
    # a graph that contradicts itself; blocked, they stay float and get a Cast
    # to float16 after them.
    to_float = [
        n.name
        for n in model.graph.node
        if n.op_type == "Cast"
        and any(a.name == "to" and a.i == onnx.TensorProto.FLOAT for a in n.attribute)
    ]
    with warnings.catch_warnings():
        # One line per weight outside float16's range, clamped into it — which
        # is what FP16 means.
        warnings.filterwarnings("ignore", message="the float32 number", category=UserWarning)
        model = float16.convert_float_to_float16(
            model,
            keep_io_types=True,
            node_block_list=to_float,
            # A LayerNorm spelled out as ReduceMean/Pow squares activations that
            # overflow float16 after self-attention; TensorRT warns about it and
            # the scores drift by up to 0.15 on real frames.
            op_block_list=[*float16.DEFAULT_OP_BLOCK_LIST, "ReduceMean", "Pow"],
        )
    return model.SerializeToString()


class TensorRTBackend(InferenceBackend):
    """NVIDIA TensorRT backend.

    Workflow:
      1. ONNX model in -> build TensorRT engine with an optimization profile
         covering dynamic batch (1..max_batch) at a fixed spatial size.
      2. Cache the serialized engine to disk. Engine binary is tied to:
         GPU compute capability, TRT version, precision, max batch, spatial size.
         Cache key encodes all of these so swapping GPU rebuilds automatically.
      3. On subsequent loads, deserialize the cached engine (seconds, not minutes).

    Inference uses execute_async_v3 with a single CUDA stream and pre-allocated
    device buffers sized for max_batch. Smaller batches reuse the same buffers
    via set_input_shape; we just memcpy the right number of bytes.
    """

    name = "tensorrt"

    # Spatial size we build the engine for. Detector pipeline letterboxes to
    # this size, so the engine doesn't need to support arbitrary H/W — fixing
    # H,W cuts engine build time dramatically and lets TRT pick better kernels.
    _BUILD_SPATIAL: int = 640

    def __init__(self) -> None:
        self._engine: Any = None
        self._context: Any = None
        self._stream: Any = None
        self._config: BackendConfig | None = None
        self._device: DeviceInfo | None = None
        self._trt_logger: Any = None

        # Tensor metadata captured at load time.
        self._input_names: list[str] = []
        self._output_names: list[str] = []
        self._tensor_dtype: dict[str, np.dtype] = {}
        # Max byte size we allocated for each tensor (sized for max_batch).
        self._tensor_capacity_bytes: dict[str, int] = {}
        # Device pointers (int) for each tensor.
        self._d_ptr: dict[str, int] = {}
        # Pinned host buffers for fast H2D / D2H. ndarray-shaped.
        self._h_buf: dict[str, np.ndarray] = {}
        # Output shape (excluding batch dim) — known after first set_input_shape.
        self._output_shapes: dict[str, tuple[int, ...]] = {}

    # ---------- discovery ----------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import tensorrt  # noqa: F401
            from cuda.bindings import runtime as cudart
        except ImportError:
            return False
        err, count = cudart.cudaGetDeviceCount()
        return err == cudart.cudaError_t.cudaSuccess and count > 0

    @classmethod
    def enumerate_devices(cls) -> list[DeviceInfo]:
        if not cls.is_available():
            return []
        from cuda.bindings import runtime as cudart

        err, count = cudart.cudaGetDeviceCount()
        _check(err, "cudaGetDeviceCount")
        devices: list[DeviceInfo] = []
        for i in range(count):
            err, props = cudart.cudaGetDeviceProperties(i)
            _check(err, "cudaGetDeviceProperties")
            name = props.name.decode() if isinstance(props.name, bytes) else props.name
            devices.append(
                DeviceInfo(
                    backend=cls.name,
                    device_id=i,
                    device_name=name,
                    supported_precisions=(Precision.FP32, Precision.FP16),
                )
            )
        return devices

    # ---------- load / build ----------

    def load(self, model_path: Path, config: BackendConfig) -> None:
        import tensorrt as trt
        from cuda.bindings import runtime as cudart

        # Idempotent: a second load() (e.g. a detector model hot-swap) must
        # first release the previous engine's CUDA + pinned buffers and reset
        # the I/O metadata. Otherwise load() APPENDS to _input_names/
        # _output_names and re-cudaMalloc's over the existing dict entries —
        # orphaning a full engine's worth of VRAM + pinned host memory and
        # doubling the tensor-name lists (which breaks infer()'s single-input
        # shortcut). unload() clears every container + frees the buffers.
        if self._engine is not None:
            self.unload()

        global _TRT_TO_NP
        if not _TRT_TO_NP:
            _TRT_TO_NP = _init_dtype_map()

        # Bind to the requested CUDA device first — engine deserialize and
        # context creation are device-scoped.
        (err,) = cudart.cudaSetDevice(config.device_id)
        _check(err, "cudaSetDevice")

        self._trt_logger = trt.Logger(trt.Logger.WARNING)
        engine_path = self._engine_cache_path(model_path, config)

        if engine_path.exists():
            log.info("Deserializing cached TRT engine: %s", engine_path.name)
            engine = self._deserialize(engine_path)
        else:
            log.info("Building TRT engine from %s (this can take minutes)", model_path.name)
            engine = self._build_engine(model_path, config, engine_path)

        self._engine = engine
        self._context = engine.create_execution_context()

        # Capture I/O metadata.
        for i in range(engine.num_io_tensors):
            tname = engine.get_tensor_name(i)
            mode = engine.get_tensor_mode(tname)
            dtype = _TRT_TO_NP[engine.get_tensor_dtype(tname)]
            self._tensor_dtype[tname] = dtype
            if mode == trt.TensorIOMode.INPUT:
                self._input_names.append(tname)
            else:
                self._output_names.append(tname)

        # Allocate buffers sized for max_batch at build spatial size.
        # Set input shape first so TRT can resolve output shapes.
        max_batch = config.max_batch_size
        for inp in self._input_names:
            shape = self._shape_for(inp, max_batch)
            self._context.set_input_shape(inp, shape)

        err, stream = cudart.cudaStreamCreate()
        _check(err, "cudaStreamCreate")
        self._stream = stream

        for tname in self._input_names + self._output_names:
            shape = tuple(self._context.get_tensor_shape(tname))
            dtype = self._tensor_dtype[tname]
            nbytes = int(np.prod(shape)) * dtype.itemsize
            err, d_ptr = cudart.cudaMalloc(nbytes)
            _check(err, "cudaMalloc")
            err, h_ptr = cudart.cudaMallocHost(nbytes)
            _check(err, "cudaMallocHost")
            # Wrap pinned memory as a numpy array of the correct shape/dtype.
            # ctypeslib.as_array gives us a view onto the pinned buffer.
            import ctypes

            buf_type = (ctypes.c_byte * nbytes).from_address(int(h_ptr))
            h_arr = np.frombuffer(buf_type, dtype=dtype).reshape(shape)
            self._d_ptr[tname] = int(d_ptr)
            self._h_buf[tname] = h_arr
            self._tensor_capacity_bytes[tname] = nbytes
            self._context.set_tensor_address(tname, int(d_ptr))
            if tname in self._output_names:
                self._output_shapes[tname] = shape[1:]
            log.info(
                "  %s %s shape=%s dtype=%s bytes=%d",
                "in " if tname in self._input_names else "out",
                tname,
                shape,
                dtype,
                nbytes,
            )

        self._config = config

        # Build the DeviceInfo for the active device.
        err, props = cudart.cudaGetDeviceProperties(config.device_id)
        _check(err, "cudaGetDeviceProperties")
        name = props.name.decode() if isinstance(props.name, bytes) else props.name
        self._device = DeviceInfo(
            backend=self.name,
            device_id=config.device_id,
            device_name=name,
            supported_precisions=(Precision.FP32, Precision.FP16),
        )

    def _shape_for(self, tname: str, batch: int) -> tuple[int, ...]:
        """Replace the dynamic batch dim in the engine's declared input
        shape with a concrete value. Any other dynamic dims (beyond batch)
        are rejected — the optimisation profile builder assumes a fixed
        spatial size, so encountering dynamic H/W here means a model
        outside that contract."""
        engine_shape = list(self._engine.get_tensor_shape(tname))
        if engine_shape and engine_shape[0] == -1:
            engine_shape[0] = batch
        for i, d in enumerate(engine_shape[1:], start=1):
            if d == -1:
                raise ValueError(
                    f"Engine input {tname!r} has dynamic dim at index {i} "
                    f"(shape={engine_shape}); only batch is allowed dynamic"
                )
        return tuple(engine_shape)

    def _deserialize(self, engine_path: Path) -> Any:
        import tensorrt as trt

        runtime = trt.Runtime(self._trt_logger)
        engine = runtime.deserialize_cuda_engine(engine_path.read_bytes())
        if engine is None:
            raise RuntimeError(f"Failed to deserialize TRT engine: {engine_path}")
        return engine

    def _build_engine(self, model_path: Path, config: BackendConfig, engine_path: Path) -> Any:
        import tensorrt as trt

        if config.precision == Precision.INT8:
            # Refuse rather than silently demote — see capabilities note.
            raise ValueError(
                "TensorRT backend does not support INT8 yet (no calibrator wired up). "
                "Use Precision.FP16 or supply a pre-quantized engine via cache."
            )
        onnx_bytes = model_path.read_bytes()
        if config.precision == Precision.FP16:
            onnx_bytes = _to_fp16(onnx_bytes)
        log.info("TRT build precision: %s", config.precision.value)

        builder = trt.Builder(self._trt_logger)
        network = builder.create_network(0)
        parser = trt.OnnxParser(network, self._trt_logger)
        if not parser.parse(onnx_bytes):
            errs = "\n".join(str(parser.get_error(i)) for i in range(parser.num_errors))
            raise RuntimeError(f"ONNX parse failed:\n{errs}")

        build_config = builder.create_builder_config()

        # Optimization profile: dynamic batch [1..max], fixed everything
        # else. We read the actual input shape from the parsed ONNX so
        # this works for both the legacy float32 NCHW models and the
        # preprocessing-baked uint8 NHWC variants (see tools/bake_preproc.py).
        profile = builder.create_optimization_profile()
        for i in range(network.num_inputs):
            inp = network.get_input(i)
            inp_shape = list(inp.shape)  # batch dim is -1 (dynamic)
            if not inp_shape or inp_shape[0] != -1:
                raise ValueError(
                    f"Input {inp.name!r} has shape {inp_shape}; expected "
                    "dynamic batch at dim 0 (use ONNX with batch_size symbol)"
                )
            for j, d in enumerate(inp_shape[1:], start=1):
                if d == -1:
                    raise ValueError(
                        f"Input {inp.name!r} has dynamic dim at index {j} "
                        f"(shape={inp_shape}); only batch may be dynamic. "
                        "Re-export the ONNX with fixed spatial size."
                    )

            def _with_batch(b: int, shape: tuple[int, ...] = inp_shape) -> tuple[int, ...]:
                s = list(shape)
                s[0] = b
                return tuple(s)

            min_shape = _with_batch(1)
            opt_shape = _with_batch(max(1, config.max_batch_size // 2))
            max_shape = _with_batch(config.max_batch_size)
            profile.set_shape(inp.name, min_shape, opt_shape, max_shape)
            log.info(
                "  profile %s: min=%s opt=%s max=%s dtype=%s",
                inp.name,
                min_shape,
                opt_shape,
                max_shape,
                inp.dtype,
            )
        build_config.add_optimization_profile(profile)

        serialized = builder.build_serialized_network(network, build_config)
        if serialized is None:
            raise RuntimeError(
                "TensorRT engine build failed (build_serialized_network returned None)"
            )

        engine_path.parent.mkdir(parents=True, exist_ok=True)
        serialized_bytes = bytes(serialized)
        engine_path.write_bytes(serialized_bytes)
        log.info("Saved engine to %s (%.1f MB)", engine_path, len(serialized_bytes) / 1e6)

        runtime = trt.Runtime(self._trt_logger)
        return runtime.deserialize_cuda_engine(serialized)

    def input_tensors(self) -> list[InputTensorInfo]:
        if self._engine is None:
            raise RuntimeError("Backend not loaded.")
        out: list[InputTensorInfo] = []
        for name in self._input_names:
            dtype = self._tensor_dtype[name]
            shape = tuple(int(d) for d in self._engine.get_tensor_shape(name))
            out.append(InputTensorInfo(name=name, dtype=dtype, shape=shape))
        return out

    def _engine_cache_path(self, model_path: Path, config: BackendConfig) -> Path:
        """Cache key encodes everything that invalidates the engine binary."""
        import tensorrt as trt
        from cuda.bindings import runtime as cudart

        err, props = cudart.cudaGetDeviceProperties(config.device_id)
        _check(err, "cudaGetDeviceProperties")
        sm = f"sm{props.major}{props.minor}"
        trt_ver = trt.__version__
        precision = config.precision.value
        max_b = config.max_batch_size
        spatial = self._BUILD_SPATIAL

        # Include the ONNX file's size+mtime in the key. Without it, re-baking a
        # model under the SAME filename (e.g. swapping in a new nv12/512 bake of
        # rtdetrv2-r18.onnx) would deserialize the STALE cached engine and serve
        # the old graph silently — the exact trap the recorded bake workflow
        # hits. size+mtime is cheap and changes on every re-export.
        try:
            st = model_path.stat()
            src_stamp = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            src_stamp = "nostat"

        key = f"{model_path.name}|{src_stamp}|trt{trt_ver}|{sm}|{precision}|b{max_b}|s{spatial}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        cache_dir = config.cache_dir or model_path.parent / "cache"
        return (
            cache_dir / f"{model_path.stem}.{sm}.trt{trt_ver}.{precision}.b{max_b}.{digest}.engine"
        )

    # ---------- inference ----------

    def warmup(self, batch_size: int = 1) -> None:
        if self._context is None:
            raise RuntimeError("Backend not loaded.")
        max_b = self._config.max_batch_size if self._config else batch_size
        batch_size = min(batch_size, max_b)
        # One iteration at the requested batch + one at full batch, to compile
        # any tactics TRT defers to first use at that shape. Shape and dtype
        # are read from the engine so we hit it correctly whether the model
        # expects float32 NCHW or uint8 NHWC (preprocessing baked in).
        for b in {batch_size, max_b}:
            dummy: dict[str, np.ndarray] = {}
            for tname in self._input_names:
                shape = self._shape_for(tname, b)
                dtype = self._tensor_dtype[tname]
                dummy[tname] = np.zeros(shape, dtype=dtype)
            self.infer(dummy)

    def infer(self, inputs: ModelInput) -> ModelOutput:
        if self._context is None:
            raise RuntimeError("Backend not loaded.")
        from cuda.bindings import runtime as cudart

        # Normalize input shape: accept ndarray or dict.
        if isinstance(inputs, np.ndarray):
            if len(self._input_names) != 1:
                raise ValueError(f"Model has {len(self._input_names)} inputs; pass a dict")
            feed = {self._input_names[0]: inputs}
        else:
            feed = inputs

        # Set input shape(s) for this call's batch and copy data H2D.
        batch: int | None = None
        for name, arr in feed.items():
            if name not in self._input_names:
                raise KeyError(f"Unknown input {name!r}; engine has {self._input_names}")
            arr = np.ascontiguousarray(arr.astype(self._tensor_dtype[name], copy=False))
            shape = arr.shape
            batch = shape[0] if batch is None else batch
            self._context.set_input_shape(name, shape)
            nbytes = arr.nbytes
            if nbytes > self._tensor_capacity_bytes[name]:
                raise ValueError(
                    f"Input {name} bytes {nbytes} exceeds allocated capacity "
                    f"{self._tensor_capacity_bytes[name]} (raise BABA_DETECTOR_BATCH?)"
                )
            # Stage into pinned host buffer flat slice, then async H2D.
            pinned = self._h_buf[name]
            flat = pinned.reshape(-1)[: arr.size]
            flat[...] = arr.reshape(-1)
            (err,) = cudart.cudaMemcpyAsync(
                self._d_ptr[name],
                pinned.ctypes.data,
                nbytes,
                cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
                self._stream,
            )
            _check(err, "cudaMemcpyAsync H2D")

        # Execute on the stream. TRT's Python binding wants a raw int handle;
        # cuda-python's stream is a typed handle, so convert.
        ok = self._context.execute_async_v3(int(self._stream))
        if not ok:
            raise RuntimeError("execute_async_v3 returned False")

        # D2H copy + reshape per output. Output shape may depend on input batch
        # — re-query the context after execution.
        out_arrays: list[np.ndarray] = []
        for name in self._output_names:
            out_shape = tuple(self._context.get_tensor_shape(name))
            dtype = self._tensor_dtype[name]
            elems = int(np.prod(out_shape))
            nbytes = elems * dtype.itemsize
            pinned = self._h_buf[name]
            (err,) = cudart.cudaMemcpyAsync(
                pinned.ctypes.data,
                self._d_ptr[name],
                nbytes,
                cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
                self._stream,
            )
            _check(err, "cudaMemcpyAsync D2H")
            out_arrays.append((pinned, out_shape, elems, dtype))  # type: ignore[arg-type]

        (err,) = cudart.cudaStreamSynchronize(self._stream)
        _check(err, "cudaStreamSynchronize")

        # Now that the stream is synced, materialize each output as a copy so
        # the next infer() call doesn't overwrite the pinned buffer.
        result: list[np.ndarray] = []
        for pinned, out_shape, elems, _dtype in out_arrays:  # type: ignore[misc]
            view = pinned.reshape(-1)[:elems].reshape(out_shape)
            result.append(view.copy())

        if len(result) == 1:
            return result[0]
        return result

    def unload(self) -> None:
        from cuda.bindings import runtime as cudart

        for d_ptr in self._d_ptr.values():
            cudart.cudaFree(d_ptr)
        for h_arr in self._h_buf.values():
            cudart.cudaFreeHost(h_arr.ctypes.data)
        if self._stream is not None:
            cudart.cudaStreamDestroy(self._stream)
        self._d_ptr.clear()
        self._h_buf.clear()
        self._tensor_capacity_bytes.clear()
        self._tensor_dtype.clear()
        self._input_names.clear()
        self._output_names.clear()
        self._output_shapes.clear()
        self._context = None
        self._engine = None
        self._stream = None
        self._device = None
        self._config = None

    @property
    def device_info(self) -> DeviceInfo:
        if self._device is None:
            raise RuntimeError("Backend not loaded")
        return self._device
