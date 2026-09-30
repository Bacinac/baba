from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import msgspec
import numpy as np
from baba_core import (
    BACKENDS,
    RING_PIXEL_FORMAT,
    BackendConfig,
    Detection,
    FrameRingReader,
    Precision,
    StatsCollector,
    StoragePaths,
    drain_quietly,
    setup_logging,
)
from baba_core.detection_gate import GateRules
from baba_core.detection_gate import classify as gate_classify
from baba_core.detection_gate import size_pct as gate_size_pct
from baba_core.nats_conn import connect as nats_connect
from baba_core.runtime import run_service
from baba_core.wire import (
    SUBJECT_DETECTOR_TRACE,
    SUBJECT_TELEMETRY_TEMPLATE,
    FrameMessage,
)
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_detector.batcher import FrameBatcher, PendingFrame
from baba_detector.config import DetectorConfig
from baba_detector.postprocess import (
    COCO_CLASSES,
    HFDetrPostprocessor,
    PostprocessContext,
    RFDetrPostprocessor,
)
from baba_detector.preprocess import (
    letterbox_batch,
    letterbox_batch_nv12,
    letterbox_batch_uint8,
    stretch_batch_nv12,
)
from baba_detector.publisher import DetectionPublisher
from baba_detector.raw_trace import RawDetectionTrace
from baba_detector.rules import DetectionRules
from baba_detector.telemetry import TelemetryAccumulator
from baba_detector.tiling import (
    TileSpec,
    dedup_detections,
    interior_seams,
    is_seam_clipped,
    offset_detections,
    plan_tiles,
    slice_tile,
)

log = logging.getLogger("baba.detector")

# COCO class name → id, for the per-camera class remap (a remap rewrites
# both a detection's label AND its id so downstream stays consistent).
_CLASS_ID = {name: i for i, name in enumerate(COCO_CLASSES)}


def _remap_detections(
    rules: DetectionRules, camera_slug: str, detections: list[Detection]
) -> list[Detection]:
    """Apply the camera's class remap (e.g. west: suitcase→car) BEFORE dedup
    and the rules filter, so a vehicle the nano mislabels as a disabled
    indoor-object class is recovered as `car`: its tile-partials then dedup
    together and the box passes the allowlist, tracks and re-IDs normally."""
    out: list[Detection] = []
    changed = False
    for d in detections:
        target = rules.remap_class(camera_slug, d.class_name)
        if target == d.class_name:
            out.append(d)
            continue
        tid = _CLASS_ID.get(target)
        if tid is None:
            out.append(d)  # unknown target class — leave the detection as-is
            continue
        out.append(
            Detection(
                bbox=d.bbox, class_id=tid, class_name=target, confidence=d.confidence
            )
        )
        changed = True
    return out if changed else detections

SUBJECT_FRAMES = "baba.frames.*"


async def consume_frames(nc, batcher: FrameBatcher, stats: StatsCollector | None = None) -> None:
    """Subscribe to frame-ready notifications. The pixels themselves live
    in each camera's POSIX shm ring; the detector attaches that ring through
    the shared `baba-shm` volume (compose: `- baba-shm:/dev/shm`) and pulls by
    sequence."""
    decoder = msgspec.msgpack.Decoder(FrameMessage)
    # One reader per camera_slug. Lazy-created on first message — keeps the
    # mapping minimal and survives mid-flight camera additions without an
    # explicit reconciliation pass on this side.
    readers: dict[str, FrameRingReader] = {}
    # Throttle the "ring miss" warning so a transient slow detector batch
    # doesn't fill the log with per-frame warnings while it catches up.
    last_miss_log_ns: dict[str, int] = {}

    async def handler(msg) -> None:
        try:
            wire = decoder.decode(msg.data)
            if stats is not None:
                stats.incr("frames_in")
            reader = readers.get(wire.camera_id)
            if reader is None:
                reader = FrameRingReader(wire.camera_id)
                readers[wire.camera_id] = reader
            frame = reader.get_by_sequence(wire.sequence)
            if frame is None:
                if stats is not None:
                    stats.incr("frames_shm_miss")
                # Either the ring isn't created yet (ingestor still
                # opening that camera) or the slot was overwritten
                # before we got here (detector is behind). Either way,
                # nothing actionable — wait for the next frame.
                now = time.time_ns()
                last = last_miss_log_ns.get(wire.camera_id, 0)
                if now - last > 5_000_000_000:
                    log.warning(
                        "shm miss cam=%s seq=%d (ring not ready or "
                        "frame overwritten before consume)",
                        wire.camera_id,
                        wire.sequence,
                    )
                    last_miss_log_ns[wire.camera_id] = now
                return
            await batcher.submit(
                PendingFrame(
                    camera_id=wire.camera_id,
                    sequence=wire.sequence,
                    timestamp_ns=wire.timestamp_ns,
                    pixels=frame.pixels,
                    enqueued_ns=time.time_ns(),
                    pts_ns=wire.pts_ns,
                    pixel_format=frame.pixel_format,
                    width=frame.width or wire.width,
                    height=frame.height or wire.height,
                )
            )
        except Exception:
            log.exception("failed to handle incoming frame message")

    await nc.subscribe(SUBJECT_FRAMES, cb=handler)
    log.info("subscribed to %s (frames via shm)", SUBJECT_FRAMES)


def _slice_output(output: np.ndarray | list[np.ndarray], idx: int) -> list[np.ndarray]:
    """Extract the per-frame slice from a batched model output."""
    if isinstance(output, list):
        return [o[idx] if o.ndim >= 2 else o[idx : idx + 1] for o in output]
    return [output[idx]]


def _classify(
    rules: DetectionRules,
    camera_slug: str,
    detection,
    size_pct: float,
    maintain_conf: float | None,
) -> tuple[str | None, bool]:
    """`(drop_reason | None, birth_eligible)` for one detection.

    Resolves this camera+class to its thresholds, then defers the decision to
    `baba_core.detection_gate.classify` — shared with the incident replay so
    what the operator is shown is produced by the same code that runs live."""
    enabled, min_conf, min_box_pct = rules.effective(camera_slug, detection.class_name)
    return gate_classify(
        GateRules(enabled=enabled, min_confidence=min_conf, min_box_pct=min_box_pct),
        confidence=detection.confidence,
        size_pct=size_pct,
        maintain_conf=maintain_conf,
    )


def _load_backend(config: DetectorConfig, cache_dir: Path):
    backend = BACKENDS.select([config.backend])()
    # ov_nv12_input: the intel ring carries NV12, so the OpenVINO backend folds
    # NV12->RGB into the model on the GPU (a plain graph gets a PrePostProcessor,
    # an nv12-baked one needs nothing). TensorRT takes an nv12-baked graph and
    # ignores it, as does onnxruntime on the cpu variant's RGB ring.
    # ov_input_size tells the OV backend the spatial size to compile for
    # (must match the letterbox target); lets BABA_DETECTOR_INPUT_SIZE
    # actually take effect on OV — 640->512 is the Arc's real GPU lever.
    extra: dict[str, object] = {"ov_input_size": config.input_size, "ov_nv12_input": True}
    if config.family == "rfdetr":
        extra["ov_imagenet_norm"] = True
    if config.ov_precision:
        extra["ov_detector_precision"] = config.ov_precision
    backend.load(
        config.model_path,
        BackendConfig(
            device_id=config.device_id,
            precision=Precision.FP16,
            max_batch_size=config.max_batch_size,
            cache_dir=cache_dir,
            extra=extra,
        ),
    )
    backend.warmup(batch_size=config.max_batch_size)
    return backend


@dataclass(frozen=True)
class _Preprocess:
    mode: str
    fn: Callable
    shm_format: str


_LEGACY_PREPROCESS = _Preprocess("cpu (legacy float32)", letterbox_batch, "rgb")


def _preprocess_for(backend, config: DetectorConfig) -> _Preprocess:
    """What preprocessing the loaded model already has baked into its graph.
    Three flavours:
      1) legacy:  float32 (B, 3, H, W) NCHW — caller does cast+norm+permute on CPU
      2) rgb baked:  uint8  (B, H, W, 3) NHWC — graph does cast+norm+permute
      3) nv12 baked: uint8  (B, H+H/2, W)    — graph does slice+upsample+
                                               BT.709 YUV→RGB + cast+norm+permute
    The flavour is read directly off the engine's input tensor metadata
    (backend.input_tensors()), which works for TRT/OpenVINO/ORT alike."""
    try:
        inputs_meta = backend.input_tensors()
    except NotImplementedError:
        log.info(
            "backend %s does not expose input_tensors(); assuming "
            "legacy float32 NCHW preprocessing",
            backend.name,
        )
        return _LEGACY_PREPROCESS
    if not inputs_meta or inputs_meta[0].dtype != np.dtype(np.uint8):
        return _LEGACY_PREPROCESS
    first = inputs_meta[0]
    if len(first.shape) == 4 and first.shape[-1] == 3:
        return _Preprocess("gpu (rgb baked)", letterbox_batch_uint8, "rgb")
    if len(first.shape) == 3:
        # (B, H+H/2, W) — NV12 input: either a baked YUV->RGB ONNX graph
        # (TensorRT) or an OpenVINO backend wrapping the model with an
        # NV12 PrePostProcessor. Either way colour conversion is on the
        # GPU; the detector just ships NV12 and doesn't need to know which.
        # RF-DETR is fed a square STRETCH (its native resize); D-FINE
        # keeps the aspect-preserving letterbox. Both hand NV12 to the
        # on-device PPP; only the CPU geometry differs.
        if config.family == "rfdetr":
            return _Preprocess("gpu (nv12, stretch)", stretch_batch_nv12, "nv12")
        return _Preprocess("gpu (nv12)", letterbox_batch_nv12, "nv12")
    raise RuntimeError(
        f"model {config.model_path.name} has unrecognised uint8 input "
        f"shape {first.shape}; expected NHWC RGB (B,H,W,3) or NV12 "
        "flat (B,H+H/2,W). Re-bake with tools/bake_preproc.py."
    )


def _trace_server(raw_trace: RawDetectionTrace):
    async def _serve_trace(msg) -> None:
        """`{camera_slug, start_ns, end_ns, max_frames}` → the raw detections
        the gate saw in that window. Answers with an explicit `coverage` so the
        caller can tell "nothing happened" apart from "the ring no longer
        reaches that far back" — two very different things to show an operator
        who is about to change a threshold."""
        try:
            req = json.loads(msg.data)
            slug = str(req["camera_slug"])
            start_ns = int(req["start_ns"])
            end_ns = int(req["end_ns"])
            max_frames = int(req.get("max_frames", 600))
            frames = raw_trace.slice(slug, start_ns, end_ns, max_frames)
            coverage = raw_trace.coverage(slug)
            reply = {
                "camera_slug": slug,
                "coverage": (
                    {"oldest_ns": coverage[0], "newest_ns": coverage[1]} if coverage else None
                ),
                "frames": [
                    {
                        "t_ns": f.timestamp_ns,
                        "seq": f.sequence,
                        "w": f.width,
                        "h": f.height,
                        "d": f.detections,
                    }
                    for f in frames
                ],
            }
        except Exception as exc:
            log.exception("detector trace request failed")
            reply = {"error": str(exc)}
        await msg.respond(json.dumps(reply).encode())

    return _serve_trace


async def _flush_telemetry(nc, telemetry: TelemetryAccumulator) -> None:
    # One-minute cadence; the window length rides along in the message so
    # the sink never has to assume it.
    encoder = msgspec.msgpack.Encoder()
    last = time.monotonic()
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for msg_out in telemetry.flush(window_s=now - last):
            await nc.publish(
                SUBJECT_TELEMETRY_TEMPLATE.format(source="detector", camera_id=msg_out.camera_id),
                encoder.encode(msg_out),
            )
        last = now


class _TilePlanner:
    """Tiling plans are derived from frame geometry alone; cached per observed
    (camera, w, h) so the plan is computed once and logged when it changes
    (camera reconnects at a new resolution → new plan, new log line)."""

    def __init__(self, *, tiles_apply: bool, input_size: int) -> None:
        self._tiles_apply = tiles_apply
        self._input_size = input_size
        self._plans: dict[tuple[str, int, int], list[TileSpec]] = {}

    def plan(self, camera_id: str, w: int, h: int) -> list[TileSpec]:
        key = (camera_id, w, h)
        plan = self._plans.get(key)
        if plan is not None:
            return plan
        plan = plan_tiles(w, h) if self._tiles_apply else [TileSpec(0, 0, w, h)]
        self._plans[key] = plan
        if len(plan) > 1:
            log.info(
                "tiling cam=%s: %dx%d (aspect %.2f) → full-frame pass + %d "
                "overlapping tiles of %dx%d — extreme-aspect frame would "
                "otherwise letterbox to a sliver of the %d² input (tiles for "
                "detail, full frame for XL objects)",
                camera_id,
                w,
                h,
                max(w, h) / min(w, h),
                len(plan) - 1,
                plan[1].width,
                plan[1].height,
                self._input_size,
            )
        return plan


class _Pipeline:
    """One batch at a time: format check, tiled inference, then the per-frame
    gate and publish."""

    def __init__(
        self,
        config: DetectorConfig,
        backend,
        preprocess: _Preprocess,
        *,
        rules: DetectionRules,
        publisher: DetectionPublisher,
        stats: StatsCollector,
        telemetry: TelemetryAccumulator,
        raw_trace: RawDetectionTrace,
    ) -> None:
        self._config = config
        self._backend = backend
        self._preprocess = preprocess
        self._postprocessor = (
            RFDetrPostprocessor() if config.family == "rfdetr" else HFDetrPostprocessor()
        )
        log.info(
            "detector head family=%s postprocessor=%s",
            config.family,
            type(self._postprocessor).__name__,
        )
        self._class_names = COCO_CLASSES  # TODO: load from sidecar JSON next to model
        self._rules = rules
        self._publisher = publisher
        self._stats = stats
        self._telemetry = telemetry
        self._raw_trace = raw_trace
        # Tiling exists to undo the LETTERBOX: a 3.56:1 frame padded into a square
        # keeps only 640×180 of content, so a tile whose aspect is near-normal
        # recovers the crushed detail. A STRETCH fills all 640×640 to begin with,
        # so there is nothing to recover — measured on the canonical patio clip
        # (2026-07-25): tiling buys +0.006 median confidence for 3× the inference.
        # So the plan follows the preprocessing, not a magic aspect number: stretch
        # families run one pass per frame, letterbox families keep tiling.
        self._planner = _TilePlanner(
            tiles_apply=config.family != "rfdetr", input_size=config.input_size
        )
        # Remembers which cameras last tripped the format-mismatch guard so the
        # warning fires on change, not every batch.
        self._last_bad_format_cams: list[str] = []

    def admit(self, batch: list[PendingFrame]) -> list[PendingFrame]:
        """Fail loud on a format mismatch instead of feeding wrong bytes to
        the letterbox. The whole batch shares ONE preprocess_fn keyed to
        the model's expected format; a camera whose ring carries a
        different pixel_format (an ingestor running a different variant than
        this detector) would be silently misinterpreted as garbage. Drop
        those frames and warn — rate-limited so one misconfigured camera
        doesn't flood the log."""
        expected = self._preprocess.shm_format
        wrong = [pf for pf in batch if pf.pixel_format != expected]
        if not wrong:
            return batch
        bad = sorted({pf.camera_id for pf in wrong})
        if bad != self._last_bad_format_cams:
            log.warning(
                "dropping %d frame(s) from %s: ring pixel_format %r != model "
                "expects %r. Point these cameras' decoder at a %s-emitting "
                "backend (check BABA_DECODER_BACKEND and both services' variants).",
                len(wrong),
                ",".join(bad),
                wrong[0].pixel_format,
                expected,
                expected,
            )
            self._last_bad_format_cams = bad
        return [pf for pf in batch if pf.pixel_format == expected]

    async def process(self, batch: list[PendingFrame]) -> None:
        self._stats.incr("batches")
        self._stats.observe("batch_size", len(batch))
        frame_dims, frame_plans, frame_dets = await self._infer(batch)
        for f_idx, pf in enumerate(batch):
            await self._gate_and_publish(pf, frame_dims[f_idx], frame_plans[f_idx], frame_dets[f_idx])

    async def _infer(
        self, batch: list[PendingFrame]
    ) -> tuple[list[tuple[int, int]], list[list[TileSpec]], dict[int, list]]:
        config = self._config
        # Expand frames into inference work items — 1 per tile. Normal
        # cameras produce exactly one item wrapping the original pixels
        # (slice_tile returns them untouched), so the common path is the
        # same single-inference flow as before tiling existed.
        frame_dims: list[tuple[int, int]] = []
        frame_plans: list[list[TileSpec]] = []
        work: list[tuple[int, TileSpec, np.ndarray]] = []  # (frame idx, spec, pixels)
        for f_idx, pf in enumerate(batch):
            # Use the logical picture H,W from the ring header (pf.width/
            # height) rather than pixels.shape — for NV12 the buffer is
            # H + H/2 rows tall and shape[0] would point at chroma.
            orig_w = pf.width or pf.pixels.shape[1]
            orig_h = pf.height or (
                pf.pixels.shape[0] if pf.pixel_format == "rgb" else pf.pixels.shape[0] * 2 // 3
            )
            frame_dims.append((orig_w, orig_h))
            plan = self._planner.plan(pf.camera_id, orig_w, orig_h)
            frame_plans.append(plan)
            if len(plan) > 1:
                self._stats.observe("tiles_per_frame", len(plan))
            for spec in plan:
                work.append(
                    (f_idx, spec, slice_tile(pf.pixels, pf.pixel_format, orig_w, orig_h, spec))
                )

        # Run the work in chunks the backend actually supports — tiling can
        # push a batch past max_batch_size, and the backend contract (TRT
        # static batch, OV batch=1 on Intel) caps a single infer() call.
        frame_dets: dict[int, list] = {i: [] for i in range(len(batch))}
        for c0 in range(0, len(work), config.max_batch_size):
            chunk = work[c0 : c0 + config.max_batch_size]
            tensor, scales, pads = self._preprocess.fn(
                [pixels for _, _, pixels in chunk], target=config.input_size
            )
            with self._stats.timer("infer_ms"):
                # Off the event loop: a batch infer is tens of ms of GPU
                # work that would otherwise block frame intake, the health
                # tick and telemetry for its whole duration. Awaited one at
                # a time, so the backend still sees strictly serial infer()
                # calls (single CUDA/OV context, no concurrent execution).
                output = await asyncio.to_thread(self._backend.infer, tensor)
            for j, (f_idx, spec, _) in enumerate(chunk):
                ctx = PostprocessContext(
                    original_width=spec.width,
                    original_height=spec.height,
                    letterbox_scale=scales[j],
                    letterbox_pad=pads[j],
                    input_size=config.input_size,
                )
                dets = self._postprocessor.decode(
                    _slice_output(output, j),
                    ctx,
                    conf_threshold=config.conf_threshold,
                    class_names=self._class_names,
                )
                frame_dets[f_idx].extend(offset_detections(dets, spec))
        return frame_dims, frame_plans, frame_dets

    async def _gate_and_publish(
        self,
        pf: PendingFrame,
        dims: tuple[int, int],
        plan: list[TileSpec],
        detections: list,
    ) -> None:
        rules = self._rules
        orig_w, orig_h = dims
        # Per-camera class remap first: fix mislabels (west
        # suitcase→car) so the corrected box dedups with its
        # tile-partials and survives the allowlist below.
        detections = _remap_detections(rules, pf.camera_id, detections)
        # Interior tile seams: used to dedup seam-clipped partials AND
        # to mark surviving partials maintain-only below (empty on a
        # non-tiled camera → no effect).
        seam_xs, seam_ys = interior_seams(plan, orig_w, orig_h)
        if len(detections) > 1:
            # Duplicates come from two sources: the same object seen by
            # two overlapping tiles, AND the nano model boxing one
            # object under two vehicle classes (car+truck) — the latter
            # happens on ANY camera, so dedup runs unconditionally.
            # Interior tile seams let dedup also drop a seam-clipped
            # partial contained in the whole object (empty → IoU-only).
            detections = dedup_detections(detections, seam_xs, seam_ys)
        # Bank the raw set BEFORE the gate. Everything below this line
        # discards detections by design; those discards are exactly the
        # evidence an incident replay needs to show what a proposed
        # threshold would have admitted. Remap + dedup have already run,
        # so the trace sees precisely what the gate sees.
        self._raw_trace.record(
            pf.camera_id,
            sequence=pf.sequence,
            timestamp_ns=pf.timestamp_ns,
            width=orig_w,
            height=orig_h,
            detections=detections,
        )
        # Apply DB-driven global floor + per-camera override.
        # Postprocess already filtered by `config.conf_threshold`
        # (the env floor, intentionally low); this pass enforces
        # the per-class / per-camera values the operator set via
        # the UI.  Cheap — O(N detections), N is typically <30.
        # Every verdict feeds the telemetry accumulator: published
        # detections carry their conf/size sample, dropped ones the
        # reason — the continuous record threshold tuning reads.
        kept_detections = []
        birth_flags: list[bool] = []
        cam_maintain = rules.maintain(pf.camera_id)  # per-camera, no global fallback
        for d in detections:
            size_pct = gate_size_pct(d.bbox.width, d.bbox.height, orig_w, orig_h)
            reason, birth = _classify(rules, pf.camera_id, d, size_pct, cam_maintain)
            # A seam-clipped tile-partial is a fragment of an object
            # continuing past the seam — let it KEEP a track alive but
            # never BIRTH its own id (the west parked-car double box:
            # a front-corner fragment that outlived dedup spawned a 2nd
            # 'car' track). Downgrade to maintain-only.
            if birth and is_seam_clipped(d, seam_xs, seam_ys):
                birth = False
            if reason is None:
                self._telemetry.note_published(
                    pf.camera_id, d.class_name, d.confidence, size_pct, birth
                )
                kept_detections.append(d)
                birth_flags.append(birth)
            else:
                self._telemetry.note_dropped(pf.camera_id, d.class_name, reason)
        await self._publisher.publish(
            camera_id=pf.camera_id,
            sequence=pf.sequence,
            timestamp_ns=pf.timestamp_ns,
            birth_eligible=birth_flags,
            frame_width=orig_w,
            frame_height=orig_h,
            detections=kept_detections,
            pts_ns=pf.pts_ns,
        )
        self._stats.incr("detections_out", len(kept_detections))


async def run(config: DetectorConfig) -> None:
    paths = StoragePaths.from_env()
    # Detector only reads models and writes compiled engines under models/cache;
    # it mounts /models but NOT /media or /state. The blanket ensure_exists()
    # would try to mkdir those unmounted tiers in / — silently fine as root, but
    # PermissionError under the non-root runtime user. Ensure just what we use.
    paths.model_cache.mkdir(parents=True, exist_ok=True)

    # Fail loud if the requested model isn't on disk. The previous
    # behaviour was to forward the missing path to the backend, which
    # would either crash with a cryptic ORT/TRT parse error or — worse
    # for ORT — load a stale cached "optimized.onnx" if one happened to
    # exist under cache_dir with the matching stem. Either way the
    # operator gets surprise behaviour minutes after startup instead of
    # a clear "your BABA_DETECTOR_MODEL points at nothing" at boot.
    if not config.model_path.exists():
        raise FileNotFoundError(
            f"BABA_DETECTOR_MODEL points at a non-existent file: {config.model_path}. "
            f"Run tools/download_models.sh to fetch the default zoo, or set "
            f"BABA_DETECTOR_MODEL to an .onnx file present under /models."
        )

    backend = _load_backend(config, paths.model_cache)
    preprocess = _preprocess_for(backend, config)
    # The ring format is fixed by the variant, so a model that takes the other
    # one would drop every frame at admit() while the detector reports healthy.
    ring_format = RING_PIXEL_FORMAT[config.variant]
    if preprocess.shm_format != ring_format:
        raise RuntimeError(
            f"{config.model_path.name} takes {preprocess.shm_format} frames, but the "
            f"{config.variant} ingestor writes {ring_format}. Point BABA_DETECTOR_MODEL "
            f"at a model that takes {ring_format} (tools/bake_preproc.py bakes an NV12 input)."
        )
    log.info(
        "detector ready: backend=%s device=%s batch<=%d model=%s preprocess=%s expects_shm=%s",
        backend.name,
        backend.device_info.device_name,
        config.max_batch_size,
        config.model_path.name,
        preprocess.mode,
        preprocess.shm_format,
    )

    nc = await nats_connect(config.nats_url, name="detector")
    stats = StatsCollector(service="detector")
    # Report what we ACTUALLY loaded. The System page used to read
    # BABA_DETECTOR_MODEL out of the api's own environment, which is a
    # different process with a different (possibly stale) env — it showed the
    # previous model for a whole session after a detector swap.
    stats.set_label("model", config.model_path.name)
    stats.set_label("device", backend.device_info.device_name)
    stats.set_label("family", config.family)

    batcher = FrameBatcher(config, stats=stats)
    # Live-reloadable per-class / per-camera rules.  Resolved per
    # detection after `decode()` to drop anything below the configured
    # floor before it reaches tracker/embedder.  Refreshed via Postgres
    # NOTIFY whenever the operator edits Settings → AI / per-camera.
    rules = DetectionRules(config.dsn)
    await rules.start()
    await consume_frames(nc, batcher, stats)
    await stats.start(nc)

    telemetry = TelemetryAccumulator()
    # Rolling pre-gate detections, served on request to the incident replay.
    raw_trace = RawDetectionTrace()
    await nc.subscribe(SUBJECT_DETECTOR_TRACE, cb=_trace_server(raw_trace))
    telemetry_task = spawn(_flush_telemetry(nc, telemetry), name="detector-telemetry", log=log)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        # Setting the event is not enough to end the run. The work loop tests
        # it once per iteration and then parks on `batcher.next_batch()`,
        # which blocks until a frame arrives and by its own contract returns
        # an empty list only after `close()`. On a quiet camera set that call
        # never returns, so the loop never comes back to look at `stop` and
        # the container waits out its kill timeout. Closing the batcher is
        # what makes the signal arrive.
        stop.set()
        batcher.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    # Dedicated health tick, decoupled from frame flow. batcher.next_batch()
    # BLOCKS on an empty queue (await queue.get()) — it never returns an empty
    # list — so touching only inside the work loop would let the marker go
    # stale whenever all cameras stop feeding frames (a PoE switch reboot, an
    # ingestor restart, a NATS hiccup), and healwatch would then restart a
    # perfectly healthy detector, reloading the TRT engine and blacking out
    # detection even once frames return. This is the same input-starvation
    # decoupling the ingestor and tracker already do. If inference itself
    # wedges the loop, this task keeps running on the same event loop only
    # while the loop is live — a true hang still starves it and trips restart.
    # Its first touch lands right after model load, so the container is marked
    # healthy as soon as the inference pipeline can accept work.
    tick_task = spawn(HealthMarker("baba", "detector").run_loop(), name="detector-health", log=log)

    pipeline = _Pipeline(
        config,
        backend,
        preprocess,
        rules=rules,
        publisher=DetectionPublisher(nc),
        stats=stats,
        telemetry=telemetry,
        raw_trace=raw_trace,
    )
    try:
        while not stop.is_set():
            batch = await batcher.next_batch()
            stats.set_gauge("queue_depth", batcher.pending)
            batch = pipeline.admit(batch)
            if batch:
                await pipeline.process(batch)
    finally:
        tick_task.cancel()
        telemetry_task.cancel()
        for t in (tick_task, telemetry_task):
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await stats.stop()
        batcher.close()
        backend.unload()
        await rules.stop()
        await drain_quietly(nc)


def main() -> None:
    setup_logging("detector")
    config = DetectorConfig.from_env()
    run_service(run(config))


if __name__ == "__main__":
    main()
