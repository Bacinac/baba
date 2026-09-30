from __future__ import annotations

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


# Build the model for a fixed spatial size so OpenVINO can pick the best
# kernels and avoid recompilation on every inference. The detector
# pipeline letterboxes upstream to this same size, so no real loss of
# flexibility. Matches the TensorRT backend's _BUILD_SPATIAL constant.
_BUILD_SPATIAL: int = 640


def spatial_axes(rank: int) -> set[int]:
    """Which axes of a model input are H and W.

    Knowing this is the whole point. The reshape loop used to treat EVERY
    non-batch axis as spatial, so on the live detector's [1, 3, 512, 512] the
    channel count — static, and not 512 — was reported as a spatial dim that
    could not be reshaped. The warning fired on every detector start, naming
    "[3]", and told the operator their requested input size was being ignored
    while H and W were in fact exactly what they had asked for. A warning that
    fires on a correct configuration is how warnings stop being read.

    The other half was worse and only latent: an export that left the channel
    axis dynamic would have had it set to the input size, giving a 512-channel
    input.

    Rank 4 is NCHW for every model this runs, so H and W are the trailing two.
    Any other rank gets nothing: better to leave a shape alone than to guess at
    which of its axes are pixels.
    """
    return {rank - 2, rank - 1} if rank == 4 else set()


class OpenVINOBackend(InferenceBackend):
    """Intel OpenVINO backend (Arc, iGPU).

    Workflow:
      1. ONNX -> Core.read_model() (no separate IR conversion needed)
      2. Reshape input to dynamic batch + fixed spatial for stable kernel
         selection (same trade-off TRT makes for its optimization profile).
      3. Core.compile_model(device) — OV picks best EP for the device.
      4. Per-process disk cache via Core CACHE_DIR property so subsequent
         loads skip the compile step (seconds vs minutes on Arc / iGPU).

    Device selection:
      - the Intel GPU only, honouring config.device_id on multi-GPU hosts;
        no GPU visible is a load error, never a CPU fallback

    Precision:
      - FP32 default
      - FP16 hint when config.precision == Precision.FP16
      - INT8 requires NNCF post-training quantization on a separately
        quantized .xml; not done in this backend — we'd accept a
        pre-quantized model path.
    """

    name = "openvino"

    def __init__(self) -> None:
        self._core: Any = None
        self._compiled: Any = None
        self._input_names: list[str] = []
        self._output_names: list[str] = []
        self._input_dtypes: dict[str, np.dtype] = {}
        self._device_name: str = ""
        self._config: BackendConfig | None = None
        self._device: DeviceInfo | None = None
        # True when the model was wrapped with an NV12->RGB PrePostProcessor
        # (GPU-side colour conversion). Changes the input contract: the model
        # wants a 4D single-plane NV12 tensor (B,H+H/2,W,1) but we present a 3D
        # (B,H+H/2,W) contract to the detector and add the channel in infer().
        self._nv12_ppp: bool = False
        # Set to a concrete batch size when the model was compiled with a fixed
        # batch (fp16 path — the GPU plugin won't fp16-compile a dynamic batch).
        # infer() then pads short batches up to this and slices the outputs back.
        self._static_batch: int | None = None
        # What compile_model was actually told, which is NOT config.precision:
        # a detector that did not opt into f16 is forced to ACCURACY below.
        self._effective_precision: str = "?"
        # Lazily-compiled variants keyed by exact batch size, so a short batch
        # runs a model built for THAT batch instead of being zero-padded up to
        # the compile-time maximum. Padding wastes the pad rows' compute
        # outright: on a 2-camera install with BABA_DETECTOR_BATCH=8 every
        # frame paid 8 frames' worth of GPU (measured: Render/3D pinned ~99%
        # -> ~45% once the batch matched reality). Bounded cache: a fleet only
        # ever produces a handful of distinct batch sizes.
        self._compiled_by_batch: dict[int, Any] = {}
        self._model: Any = None            # reshaped/PPP-wrapped, kept for re-compiles
        self._compile_device: str = ""
        self._compile_config: dict[str, Any] = {}

    @classmethod
    def is_available(cls) -> bool:
        try:
            import openvino as ov
        except ImportError:
            return False
        try:
            core = ov.Core()
            return len(core.available_devices) > 0
        except RuntimeError:
            return False

    @classmethod
    def enumerate_devices(cls) -> list[DeviceInfo]:
        if not cls.is_available():
            return []
        import openvino as ov

        core = ov.Core()
        devices: list[DeviceInfo] = []
        for idx, name in enumerate(core.available_devices):
            try:
                full_name = core.get_property(name, "FULL_DEVICE_NAME")
            except RuntimeError:
                full_name = name
            devices.append(
                DeviceInfo(
                    backend=cls.name,
                    device_id=idx,
                    device_name=f"{name} ({full_name})",
                    supported_precisions=(Precision.FP32, Precision.FP16),
                )
            )
        return devices

    def _pick_device(self, config: BackendConfig, available: list[str]) -> str:
        """The GPU to load onto: GPU.<device_id> on a multi-GPU host, else the
        only one. This backend exists to run on the Intel GPU, so with none
        visible (render node not passed through, driver broken) it raises —
        OpenVINO would otherwise settle for the CPU and run the model there at
        a fraction of the speed while the stack looks healthy."""
        gpus = [d for d in available if d.startswith("GPU")]
        if not gpus:
            raise RuntimeError(
                f"OpenVINO sees no GPU (devices: {available}). Pass the Intel render "
                "node into the container and check the driver."
            )
        target = f"GPU.{config.device_id}"
        return target if target in gpus else gpus[0]

    def load(self, model_path: Path, config: BackendConfig) -> None:
        import openvino as ov

        if not model_path.exists():
            raise FileNotFoundError(f"OpenVINO model not found: {model_path}")

        self._core = ov.Core()
        self._enable_disk_cache(config.cache_dir)

        device_name = self._pick_device(config, list(self._core.available_devices))
        self._device_name = device_name
        log.info(
            "Loading %s into OpenVINO device=%s (requested precision=%s)",
            model_path.name,
            device_name,
            config.precision.value,
        )

        model = self._core.read_model(str(model_path))

        extra = config.extra or {}
        batch_only = bool(extra.get("ov_batch_only_reshape"))
        # The intel variant's ring carries NV12, so the detector always asks for
        # an NV12 input. A graph with the conversion already baked in
        # (tools/bake_preproc.py) takes it as it is; only a plain RGB graph gets
        # the on-device PrePostProcessor.
        nv12_ppp = bool(extra.get("ov_nv12_input")) and not self._takes_nv12(model)
        # RF-DETR (DINOv2 backbone) wants ImageNet-normalised RGB; D-FINE/RT-DETR
        # want plain [0,1]. Opt-in via extra["ov_imagenet_norm"], set by the
        # detector for the rfdetr family. Folded into the same on-device PPP.
        imagenet_norm = bool(extra.get("ov_imagenet_norm"))
        # Explicit per-deployment fp16 opt-in for the DETECTOR (see
        # _compile_hints for why this is opt-in rather than default).
        detector_f16 = not batch_only and str(extra.get("ov_detector_precision", "")).lower() == "f16"
        # Spatial size to pin dynamic H/W dims to at compile time. The detector
        # letterboxes to its input_size and passes it here; the compiled graph
        # must match. Lowering it (e.g. 640→512) is the ONE lever that actually
        # cuts GPU on the Arc A380 — this plugin is memory-bandwidth-bound on the
        # 640² feature maps, so cost scales ~with input AREA (512²≈0.63×), while
        # shrinking the model (backbone FLOPs) barely moves it. Crops for re-ID/
        # face come from the full-res SHM frame, not this tensor, so detection
        # res is decoupled from crop quality. Defaults to _BUILD_SPATIAL.
        build_spatial = int(extra.get("ov_input_size", _BUILD_SPATIAL))
        # The OpenVINO GPU plugin can't compile RT-DETR in fp16 with a *dynamic*
        # batch dim — it aborts at a MatMul dimension check (measured on OV
        # 2026.2 / Arc A380) and we'd fall back to f32. It compiles fine at a
        # *static* batch, so for fp16 detector loads we pin the batch to
        # max_batch_size and pad short batches in infer(). The fp16 win (~3x vs
        # f32 on the Arc) dwarfs the occasional padded row.
        use_static_batch = config.precision == Precision.FP16 or detector_f16
        if batch_only:
            # Small models get a static batch of ONE, not max_batch_size.
            #
            # A dynamic batch dim makes the GPU plugin fall back to
            # shape-agnostic kernels, and for these models that is the dominant
            # cost — measured on the Arc, f16 throughout:
            #   OSNet b=1   dynamic 25.9 ms  ->  static 6.1 ms   (4.2x)
            #   SCRFD 320   dynamic 19.7 ms  ->  static 5.7 ms   (3.5x)
            #   TopoFR b=1  dynamic 19.4 ms  ->  static 21.1 ms  (no gain)
            # so the shape fix carries OSNet/SCRFD while precision carries
            # TopoFR; neither alone is enough.
            #
            # One, because the live batch distribution is dominated by single
            # crops (~1.1 detections per frame across the fleet) and padding
            # every call up to max_batch_size would spend the win on zero rows.
            # `OSNetOnnxBackend` already chunks and zero-pads to a static batch
            # when one is set, so a larger bucket can be introduced later
            # without touching callers — measured, even the 8-crop case is not
            # worse looped at b=1 than it is today (8 x 6.1 = 49 ms against
            # 57.3 ms dynamic/f32).
            static_batch = 1 if use_static_batch else None
        else:
            static_batch = max(1, config.max_batch_size) if use_static_batch else None
        self._reshape_input(
            model, static_batch=static_batch, batch_only=batch_only, build_spatial=build_spatial
        )
        if nv12_ppp:
            model = self._wrap_nv12_input(model, imagenet_norm=imagenet_norm)

        compile_config, self._effective_precision = self._compile_hints(
            config.precision, batch_only=batch_only, detector_f16=detector_f16
        )
        self._compile(
            model, device_name, compile_config, precision=config.precision, detector_f16=detector_f16
        )

        # Keep what we need to re-compile this same graph at another batch
        # size (see _compiled_for). `model` is the post-reshape, post-PPP graph
        # actually handed to compile_model, so a re-compile differs only in the
        # batch dimension.
        self._model = model
        self._compile_device = device_name
        self._compile_config = dict(compile_config)
        self._compiled_by_batch = (
            {self._static_batch: self._compiled} if self._static_batch is not None else {}
        )

        self._input_names = [inp.any_name for inp in self._compiled.inputs]
        self._output_names = [out.any_name for out in self._compiled.outputs]
        # OV element_type → numpy dtype. Most detectors use f32 input;
        # cover the common cases so int64 indices don't trip us up.
        for inp in self._compiled.inputs:
            self._input_dtypes[inp.any_name] = np.dtype(inp.element_type.to_dtype())

        self._config = config
        try:
            full_name = self._core.get_property(device_name, "FULL_DEVICE_NAME")
        except RuntimeError:
            full_name = device_name
        self._device = DeviceInfo(
            backend=self.name,
            device_id=config.device_id,
            device_name=f"{device_name} ({full_name})",
            supported_precisions=(Precision.FP32, Precision.FP16),
        )
        if detector_f16:
            self._verify_repeat_stability()
        log.info(
            "openvino ready: device=%s precision=%s inputs=%s outputs=%s",
            self._device.device_name,
            self._effective_precision,
            self._input_names,
            self._output_names,
        )

    def _enable_disk_cache(self, cache_dir: Path | None) -> None:
        # Disk cache for compiled blobs. Same key the cache_dir parameter
        # serves for TRT — survives container restarts and avoids the
        # 30-60s compile step on Arc/iGPU after the first run. Only enable it
        # when the dir is actually writable: several services mount /models
        # read-only (models are inputs), and OV's compile serialisation to an
        # unwritable CACHE_DIR fails hard ("Failed to write N bytes to stream").
        # Those services just compile without a disk cache instead.
        if not cache_dir:
            return
        cache_root = cache_dir / "openvino"
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            probe = cache_root / ".wtest"
            probe.touch()
            probe.unlink()
        except OSError as e:
            log.info(
                "openvino: cache dir %s not writable (%s); compiling without "
                "a disk cache",
                cache_root,
                e,
            )
        else:
            self._core.set_property({"CACHE_DIR": str(cache_root)})

    def _reshape_input(
        self, model, *, static_batch: int | None, batch_only: bool, build_spatial: int
    ) -> None:
    # Reshape to dynamic batch. Two modes:
    #  - spatial (default, the detector): also pin any *other* dynamic dim
    #    to _BUILD_SPATIAL. Mirrors TRT's optimisation profile — the
    #    detector letterboxes to a fixed square, and a concrete spatial
    #    lets the GPU pick good kernels AND lets warmup() build a valid
    #    dummy. Right for square-input DETR detectors.
    #  - batch_only (opt-in via extra["ov_batch_only_reshape"], used by the
    #    onnx_session shim for the small models): make ONLY the batch dim
    #    dynamic and leave every other dim exactly as the ONNX declares.
    #    The 640-pinning is WRONG for models whose non-batch dims are
    #    dynamic but not square 640 — e.g. DINOv2 exports input as
    #    [?,?,?,?] and pinning would force channels to 640 (Conv channel
    #    mismatch). OpenVINO compiles the dynamic shape fine; the caller
    #    feeds the model's real size at inference.
        import openvino as ov

        try:
            input_node = model.input(0)
            existing = input_node.get_partial_shape()
            new_dims = list(existing)
            spatial = spatial_axes(len(new_dims))
            stuck_static: list[int] = []  # spatial dims we couldn't reshape
            for i in range(len(new_dims)):
                if i == 0:
                    new_dims[i] = (
                        ov.Dimension(static_batch) if static_batch else ov.Dimension.dynamic()
                    )
                elif batch_only or i not in spatial:
                    continue
                elif new_dims[i].is_dynamic:
                    new_dims[i] = ov.Dimension(build_spatial)
                elif int(new_dims[i].get_length()) != build_spatial:
                    # A statically-sized spatial dim that doesn't already match
                    # the requested build_spatial: reshape() leaves it untouched,
                    # so BABA_DETECTOR_INPUT_SIZE silently doesn't take effect
                    # (the model still runs at its baked H/W). Fail loud — a
                    # 512-request quietly running at 640 gives zero GPU saving
                    # with no signal, exactly the Arc lever we were chasing.
                    stuck_static.append(int(new_dims[i].get_length()))
            model.reshape({input_node.any_name: ov.PartialShape(new_dims)})
            self._static_batch = static_batch
            if stuck_static and build_spatial != _BUILD_SPATIAL:
                log.warning(
                    "openvino: requested input size %d cannot take effect — the "
                    "ONNX graph has statically-baked spatial dim(s) %s. Re-export "
                    "the model with a dynamic H/W (or matching size) for "
                    "BABA_DETECTOR_INPUT_SIZE to have any effect.",
                    build_spatial,
                    stuck_static,
                )
        except RuntimeError as e:
            # Some models may have static dims that can't reshape. Don't fail
            # load — fall back to whatever the ONNX declares.
            log.warning(
                "openvino: reshape to dynamic batch failed (%s); using ONNX shape",
                e,
            )

    @staticmethod
    def _takes_nv12(model) -> bool:
        """Whether the graph's input is already single-plane NV12: uint8
        (B, H+H/2, W), the shape tools/bake_preproc.py produces."""
        from openvino import Type

        first = model.inputs[0]
        return (
            first.get_element_type() == Type.u8
            and first.get_partial_shape().rank.get_length() == 3
        )

    def _wrap_nv12_input(self, model, *, imagenet_norm: bool):
    # GPU-side NV12->RGB colour conversion (extra["ov_nv12_input"], which the
    # detector sets because the intel ingestor always decodes to NV12). Wrap
    # the plain RGB model with an OpenVINO PrePostProcessor that takes a
    # single-plane NV12 tensor (B, H+H/2, W, 1) uint8 and runs
    # convert_color -> resize -> f32 -> /255 on-device. This is the
    # hardware-adapter equivalent of the TensorRT nv12-baked ONNX graph: it
    # moves the YUV->RGB matrix off the CPU (libswscale) onto the Arc media/
    # compute engine. The plain /255 matches the detector's letterbox_batch
    # float32 normalisation (no ImageNet mean/std). The detector runs f32 on
    # OV regardless (fp16 is broken for these transformers — see the compile
    # block below), so the PPP's f32 output feeds the graph directly.
        from openvino import Layout, Type
        from openvino.preprocess import ColorFormat, PrePostProcessor, ResizeAlgorithm

        first = model.inputs[0]
        if first.get_element_type() != Type.f32 or first.get_partial_shape().rank.get_length() != 4:
            raise RuntimeError(
                f"openvino: the NV12 PrePostProcessor wraps a plain float32 NCHW model, "
                f"but this one takes {first.get_element_type()} {first.get_partial_shape()}; "
                "use the plain .onnx or the .nv12.onnx file"
            )
        ppp = PrePostProcessor(model)
        ov_in = ppp.input()
        ov_in.tensor().set_element_type(Type.u8).set_color_format(
            ColorFormat.NV12_SINGLE_PLANE
        ).set_layout(Layout("NHWC"))
        steps = ov_in.preprocess().convert_color(ColorFormat.RGB).resize(
            ResizeAlgorithm.RESIZE_LINEAR
        ).convert_element_type(Type.f32).scale(255.0)
        if imagenet_norm:
            # (x/255 - mean) / std per channel. mean() subtracts, scale()
            # divides; applied after the /255 above → net ImageNet norm.
            steps.mean([0.485, 0.456, 0.406]).scale([0.229, 0.224, 0.225])
        ov_in.model().set_layout(Layout("NCHW"))
        wrapped = ppp.build()
        self._nv12_ppp = True
        log.info(
            "openvino: NV12->RGB colour conversion wrapped on-device; model "
            "now takes a single-plane NV12 uint8 input"
        )
        return wrapped

    @staticmethod
    def _compile_hints(
        precision: Precision, *, batch_only: bool, detector_f16: bool
    ) -> tuple[dict[str, Any], str]:
        # Latency-oriented hint — single big batch from the detector, not a
        # server fielding many small parallel requests.
        compile_config: dict[str, Any] = {"PERFORMANCE_HINT": "LATENCY"}
        # The OpenVINO GPU plugin's fp16 path is UNUSABLE for the RT-DETR /
        # D-FINE transformer detectors on this stack (measured OV 2026.2 / Arc
        # A380). Two independent fp16 failures:
        #   1. RT-DETR (r18) fp16 with a dynamic batch fails to COMPILE — aborts
        #      at a MatMul dimension check in shape inference.
        #   2. D-FINE fp16 (static batch) COMPILES but the GPU program corrupts
        #      after the FIRST inference: call 0 is correct (sigmoid max ~0.94),
        #      every subsequent call returns collapsed logits (~0.016 → zero
        #      detections). It is NOT a warmup or infer-request-reuse artefact —
        #      a fresh request per call collapses identically, and f32 is
        #      bit-stable across every call. See tools/ov_precision_probe.py.
        # r18 only ever worked because failure (1) bounced it into the f32 retry
        # below; d-fine-m's fp16 compile SUCCEEDS, so nothing caught it and it
        # silently served garbage. So the detector (spatial reshape, i.e. NOT the
        # batch_only shim) runs f32/ACCURACY on the GPU BY DEFAULT: slower than
        # fp16 but CORRECT, and still fully on the GPU — a precision change, not
        # a silent CPU fallback. The small models (batch_only, via the
        # onnx_session shim) keep the fp16/f32 hint they ask for.
        #
        # 2026-07-22 re-measure (OV 2026.2.1, Arc A380): d-fine-s fp16 with
        # fully static shapes is STABLE across 270+ real inferences and its
        # detection quality is intact (seated-person p50 0.529 vs 0.498 f32) at
        # ~3x the speed — the collapse does NOT reproduce on that model. Since
        # the failure was per-model (d-fine-m corrupted, d-fine-s does not),
        # fp16 stays an EXPLICIT opt-in per deployment
        # (extra["ov_detector_precision"]="f16", wired from
        # BABA_DETECTOR_OV_PRECISION), and load() VERIFIES stability below:
        # the same input run repeatedly must give identical outputs — the exact
        # historical failure mode. On divergence load() raises; there is no
        # silent f32 fallback for an explicitly requested f16 (fail loud).
        force_f32_detector = not batch_only and not detector_f16
        if force_f32_detector:
            compile_config["EXECUTION_MODE_HINT"] = "ACCURACY"
        elif detector_f16:
            compile_config["INFERENCE_PRECISION_HINT"] = "f16"
        elif precision == Precision.FP16:
            # Hint, not a hard requirement. OV may still keep some ops at
            # FP32 if the device doesn't support FP16 for them.
            compile_config["INFERENCE_PRECISION_HINT"] = "f16"
        else:
            compile_config["INFERENCE_PRECISION_HINT"] = "f32"
        effective = (
            "f32 (ACCURACY)"
            if force_f32_detector
            else compile_config.get("INFERENCE_PRECISION_HINT", "f32")
        )
        return compile_config, effective

    def _compile(
        self,
        model,
        device_name: str,
        compile_config: dict[str, Any],
        *,
        precision: Precision,
        detector_f16: bool,
    ) -> None:
        try:
            self._compiled = self._core.compile_model(
                model,
                device_name=device_name,
                config=compile_config,
            )
        except RuntimeError as compile_err:
            if detector_f16:
                # The operator EXPLICITLY asked for f16; quietly serving f32
                # instead would hide that the request is impossible on this
                # model. Fail loud, let them drop the env.
                raise RuntimeError(
                    "OpenVINO fp16 was explicitly requested "
                    "(BABA_DETECTOR_OV_PRECISION=f16) but the compile failed for "
                    f"this model: {compile_err}"
                ) from compile_err
            # Safety net for any remaining graph the plugin can't lay out in the
            # requested precision. Retry once in ACCURACY (full f32): slower but
            # correct, and STILL on the same GPU — a precision change, NOT a
            # silent fallback to CPU. Logged loud.
            last = next(
                (ln for ln in reversed(str(compile_err).splitlines()) if ln.strip()),
                str(compile_err),
            )
            log.warning(
                "openvino: %s compile failed at precision=%s (%s); retrying in "
                "ACCURACY/f32 mode",
                device_name,
                precision.value,
                last[:160],
            )
            self._compiled = self._core.compile_model(
                model,
                device_name=device_name,
                config={"PERFORMANCE_HINT": "LATENCY", "EXECUTION_MODE_HINT": "ACCURACY"},
            )
            self._effective_precision = "f32 (ACCURACY, after failed compile)"

    def _verify_repeat_stability(self) -> None:
        """fp16 opt-in gate. The historical GPU-plugin failure COMPILED cleanly
        and corrupted from the second inference on (d-fine-m: call 0 sane,
        every later call collapsed logits ~60x). So before serving anything,
        run one fixed input three times: any divergence between calls IS that
        bug, and an explicitly requested f16 must die loudly rather than
        quietly serve garbage detections."""
        assert self._compiled is not None
        rng = np.random.default_rng(0)
        feeds: dict[str, np.ndarray] = {}
        for inp in self._compiled.inputs:
            shape = [int(d.get_length()) for d in inp.get_partial_shape()]
            dt = np.dtype(inp.element_type.to_dtype())
            feeds[inp.any_name] = (
                rng.integers(0, 255, size=shape, dtype=np.uint8)
                if dt == np.uint8
                else rng.random(size=shape, dtype=np.float32).astype(dt)
            )
        outs = []
        for _ in range(3):
            r = self._compiled(feeds)
            outs.append(np.asarray(r[self._compiled.outputs[0]]).copy())
        worst = max(float(np.max(np.abs(o - outs[0]))) for o in outs[1:])
        if worst > 1e-2:
            raise RuntimeError(
                "OpenVINO fp16 stability check FAILED: identical input diverges "
                f"by {worst:.4f} across repeated inference — the known GPU-plugin "
                "fp16 corruption. Remove BABA_DETECTOR_OV_PRECISION=f16 for this "
                "model."
            )
        log.info(
            "openvino: fp16 repeat-stability verified (max drift %.2e over 3 calls)",
            worst,
        )

    def warmup(self, batch_size: int = 1) -> None:
        if self._compiled is None:
            raise RuntimeError("Backend not loaded. Call load() first.")
        max_b = self._config.max_batch_size if self._config else batch_size
        # Shape comes from the compiled model so a uint8 NHWC baked model
        # warms with the correct layout instead of a hardcoded NCHW one.
        per_input_shape: dict[str, tuple[int, ...]] = {}
        for ov_inp in self._compiled.inputs:
            name = ov_inp.any_name
            dims = list(ov_inp.get_partial_shape())
            # Only dim 0 (batch) may be dynamic here. If any OTHER dim is
            # dynamic (batch_only reshape mode — e.g. DINOv2's [?,?,?,?]), we
            # can't pick a valid warmup shape, so skip warmup: kernels compile
            # on the first real inference instead.
            if any(d.is_dynamic for d in dims[1:]):
                log.info(
                    "openvino: skipping warmup for input %s with dynamic non-batch "
                    "dims %s; kernels compile on first inference",
                    name,
                    [(-1 if d.is_dynamic else d.get_length()) for d in dims],
                )
                return
            shape: list[int] = []
            for _i, d in enumerate(dims):
                if d.is_dynamic:
                    shape.append(0)  # placeholder, will set per iteration
                else:
                    shape.append(d.get_length())
            per_input_shape[name] = tuple(shape)

        for b in {max(1, batch_size), max(1, max_b)}:
            dummy = {}
            for name, shape in per_input_shape.items():
                # Dim 0 is batch and is the only legitimate dynamic dim.
                concrete = tuple(b if s == 0 else s for s in shape)
                dummy[name] = np.zeros(
                    concrete,
                    dtype=self._input_dtypes.get(name, np.dtype(np.float32)),
                )
            self._compiled(dummy)

    _MAX_BATCH_VARIANTS = 4

    def _compiled_for(self, batch: int) -> Any | None:
        """Compiled model whose static batch is exactly `batch`, or None if we
        can't build one (caller then falls back to zero-padding).

        Compiles on first sight of a batch size and caches it. OV's on-disk
        CACHE_DIR makes the repeat cost near-zero across restarts. The cache is
        capped so a pathological caller can't compile without bound; once full
        we serve the padded path rather than thrash."""
        # `in` not `.get()`: a failed compile caches None so we don't retry it
        # on every single frame.
        if batch in self._compiled_by_batch:
            return self._compiled_by_batch[batch]
        if self._model is None or len(self._compiled_by_batch) >= self._MAX_BATCH_VARIANTS:
            return None
        import openvino as ov

        try:
            # Reshape the kept graph in place — already-compiled models are
            # independent snapshots, so this can't disturb one mid-inference.
            node = self._model.input(0)
            dims = list(node.get_partial_shape())
            dims[0] = ov.Dimension(batch)
            self._model.reshape({node.any_name: ov.PartialShape(dims)})
            compiled = self._core.compile_model(
                self._model, device_name=self._compile_device, config=self._compile_config
            )
        except RuntimeError as e:
            # Not fatal: the padded path still produces correct results.
            log.warning(
                "openvino: could not compile a batch=%d variant (%s); "
                "falling back to zero-padded batch=%s",
                batch,
                str(e).splitlines()[0][:120] if str(e) else type(e).__name__,
                self._static_batch,
            )
            self._compiled_by_batch[batch] = None  # negative cache: don't retry every frame
            return None
        log.info("openvino: compiled batch=%d variant (was padding to %s)", batch, self._static_batch)
        self._compiled_by_batch[batch] = compiled
        return compiled

    def infer(self, inputs: ModelInput) -> ModelOutput:
        if self._compiled is None:
            raise RuntimeError("Backend not loaded. Call load() first.")
        if isinstance(inputs, np.ndarray):
            if len(self._input_names) != 1:
                raise ValueError(f"Model has {len(self._input_names)} inputs; pass a dict")
            feed: dict[str, np.ndarray] = {self._input_names[0]: inputs}
        else:
            feed = dict(inputs)

        # Real batch (dim0) before any padding, so we can slice the compiled
        # model's fixed-batch outputs back down to what the caller sent.
        real_batch: int | None = None
        compiled = self._compiled
        pad_to = self._static_batch
        if self._static_batch is not None and feed:
            real_batch = int(next(iter(feed.values())).shape[0])
            if 0 < real_batch < self._static_batch:
                exact = self._compiled_for(real_batch)
                if exact is not None:
                    # Right-sized graph: no pad rows, no wasted GPU.
                    compiled = exact
                    pad_to = real_batch

        # Coerce to the model's declared dtype so a float64 caller doesn't
        # blow up the EP. ascontiguousarray avoids a copy when already
        # contiguous; the dtype cast is no-op if already correct.
        normalised: dict[str, np.ndarray] = {}
        for name, arr in feed.items():
            want = self._input_dtypes.get(name, np.dtype(np.float32))
            arr = arr.astype(want, copy=False)
            # NV12-PPP model wants a 4D single-plane tensor (B,H+H/2,W,1); the
            # detector feeds the 3D (B,H+H/2,W) contract we advertise in
            # input_tensors(). Add the trailing channel here.
            if self._nv12_ppp and arr.ndim == 3:
                arr = arr[..., np.newaxis]
            # Static-batch (fp16) model: pad a short batch up to the compiled
            # batch with zero rows. RT-DETR has no cross-batch mixing so the
            # real rows are unaffected; the padded outputs are sliced off below.
            if pad_to is not None and arr.shape[0] < pad_to:
                pad = pad_to - arr.shape[0]
                arr = np.concatenate(
                    [arr, np.zeros((pad, *arr.shape[1:]), dtype=arr.dtype)], axis=0
                )
            normalised[name] = np.ascontiguousarray(arr)

        results = compiled(normalised)
        # OV returns an OVDict-like; index by output port. Materialize as
        # plain numpy arrays so callers don't depend on OV objects.
        outputs: list[np.ndarray] = [np.asarray(results[port]) for port in compiled.outputs]
        # Undo static-batch padding: slice each batched output back to the real
        # batch. Guard on dim0 == static_batch so a non-batched output (if any)
        # is left intact.
        if real_batch is not None:
            outputs = [
                o[:real_batch] if o.ndim >= 1 and o.shape[0] == pad_to else o
                for o in outputs
            ]
        if len(outputs) == 1:
            return outputs[0]
        return outputs

    def input_tensors(self) -> list[InputTensorInfo]:
        if self._compiled is None:
            raise RuntimeError("Backend not loaded.")
        out: list[InputTensorInfo] = []
        for ov_inp in self._compiled.inputs:
            name = ov_inp.any_name
            dims = list(ov_inp.get_partial_shape())
            shape = tuple(-1 if d.is_dynamic else d.get_length() for d in dims)
            # NV12-PPP: the compiled model's input is 4D (B,H+H/2,W,1) but the
            # detector's format detection + letterbox_nv12 expect the 3D
            # (B,H+H/2,W) NV12 contract. Drop the trailing channel; infer()
            # adds it back before feeding the model.
            if self._nv12_ppp and len(shape) == 4 and shape[-1] == 1:
                shape = shape[:3]
            dtype = self._input_dtypes.get(name, np.dtype(np.float32))
            out.append(InputTensorInfo(name=name, dtype=dtype, shape=shape))
        return out

    def output_tensors(self) -> list[InputTensorInfo]:
        """Describe the model's output tensors, in the same order infer()
        returns them (i.e. self._compiled.outputs order). Symmetric with
        input_tensors(); the OpenVINO session shim in core.onnx_session
        uses this to emulate onnxruntime's get_outputs()/run(names, feed)."""
        if self._compiled is None:
            raise RuntimeError("Backend not loaded.")
        out: list[InputTensorInfo] = []
        for ov_out in self._compiled.outputs:
            name = ov_out.any_name
            dims = list(ov_out.get_partial_shape())
            shape = tuple(-1 if d.is_dynamic else d.get_length() for d in dims)
            dtype = np.dtype(ov_out.element_type.to_dtype())
            out.append(InputTensorInfo(name=name, dtype=dtype, shape=shape))
        return out

    def unload(self) -> None:
        # OpenVINO has no explicit free; dropping references is enough.
        # We clear in dependency order: compiled → core last.
        self._compiled = None
        self._input_names.clear()
        self._output_names.clear()
        self._input_dtypes.clear()
        self._core = None
        self._device = None
        self._config = None
        self._device_name = ""
        self._nv12_ppp = False
        self._static_batch = None

    @property
    def device_info(self) -> DeviceInfo:
        if self._device is None:
            raise RuntimeError("Backend not loaded")
        return self._device
