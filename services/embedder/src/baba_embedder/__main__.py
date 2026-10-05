"""Embedder service.

Subscribes to `baba.tracks.{slug}` for every camera, debounces samples per
track, looks up the matching frame in the ingestor's shm ring, crops the
bbox, and produces a vector embedding. The embedding plus its bookkeeping
is written to `track_embedding_samples`; later event-manager picks the
best sample at track-finalize time and copies it into `tracks.embedding`.

This first cut ships a stub backend that emits a normalized random vector
of the right shape. Swapping in DINOv2 ONNX is a follow-up — the rest of
the pipeline (ring → crop → debounce → DB) is what we need to validate
first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import cv2
import msgspec
import numpy as np
from baba_core import (
    FaceStack,
    FrameRingReader,
    StatsCollector,
    cap_long_edge,
    drain_quietly,
    dsn_from_env,
    make_face_stack_for_model,
    setup_logging,
    vector_literal,
)
from baba_core.embed import EmbeddingBackend, make_backend
from baba_core.face_settings import acknowledge_face_selection, read_face_selection
from baba_core.inference import run_inference
from baba_core.nats_conn import connect as nats_connect
from baba_core.paths import CROPS, FACE_CROPS, MediaLayout
from baba_core.pg_listen import ResilientListener
from baba_core.runtime import run_service
from baba_core.wire import TracksMessage as _TracksMessage
from baba_core.wire import TrackWire as _TrackWire
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_embedder.native_faces import NativeFaceReader

log = logging.getLogger("baba.embedder")

SUBJECT_TRACKS_IN = "baba.tracks.*"

# Per-track debounce: at most one embedding every DEBOUNCE_MS ms per
# (camera, local_track_id). 500 ms is enough granularity for re-ID work
# without bleeding the GPU when many tracks coexist.
DEBOUNCE_MS = 500

# Throttle for tracks the tracker marked as stationary or parked. We
# still want SOME embedding for them — a drift check so re-ID can spot
# "wait, this is a different car than the one that parked here yesterday"
# — but not at full 2 Hz. 5 minutes is plenty: a parked car doesn't
# semantically change in that window, and a person who stops moving
# (sitting, leaning) usually only stays put for a minute or two before
# returning to active.
_PARKED_REFRESH_MS = 300_000

# Debounce-map garbage collection. Local track ids are never reused, so the
# (camera, track_id) → last_embed_ns map would grow forever without pruning.
# Drop entries untouched for _DEBOUNCE_GC_MS; run the sweep every
# _DEBOUNCE_GC_EVERY messages (cheap dict scan, off the per-track hot path).
_DEBOUNCE_GC_MS = 1_800_000  # 30 min quiet → track has ended
_DEBOUNCE_GC_EVERY = 500

# Bbox quality gates. Tiny or very-low-confidence boxes give garbage
# crops; skip them rather than waste an embedding slot.
MIN_BBOX_SIDE_PX = 32
MIN_CONFIDENCE = 0.30

# Where the focused-subject JPEGs land. Relative to BABA_MEDIA_HOST on
# the host; mounted at /media in the embedder container. Stored
# relative in the DB so a future re-mount doesn't break references.

# COCO person class id — only this triggers the face stack. Vehicles
# and animals skip face entirely (saves inference cost and avoids
# accidental "face detected on a car windshield reflection" noise).
_PERSON_CLASS_ID = 0


# Wire types (frame/detections/tracks) live in baba_core.wire; imported above
# as _TrackWire / _TracksMessage so the rest of this module is unchanged.


@dataclass(slots=True)
class _TrackDebounce:
    last_embed_ns: int = 0


@dataclass(slots=True, frozen=True)
class CameraInfo:
    id: UUID


class _CameraResolver:
    """Maps camera slug → DB uuid. Refreshes on NOTIFY cameras_changed.
    Mirrors the event-manager pattern."""

    def __init__(self) -> None:
        self._map: dict[str, CameraInfo] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, conn: asyncpg.Connection) -> None:
        rows = await conn.fetch("SELECT id, slug FROM cameras WHERE enabled")
        async with self._lock:
            self._map = {r["slug"]: CameraInfo(id=r["id"]) for r in rows}
        log.info("camera map refreshed: %d cameras", len(self._map))

    async def get(self, slug: str) -> CameraInfo | None:
        async with self._lock:
            return self._map.get(slug)


# --- crop preparation ------------------------------------------------------
# Crops are kept at NATIVE aspect ratio and NATIVE resolution — no fixed square
# pre-resize, and no long-edge cap here. Each backend does its own resize
# (OSNet 256x128, DINOv2 224, SCRFD 640), so pre-squashing only threw away
# pixels and padded with black bars OSNet was never trained on.
#
# The cap moved to the CALL SITE, and only onto the paths that want it, because
# the face path must not be capped. Measured 2026-07-15 on the patio camera:
# ArcFace's own canonical template (`arcface_dst` in InsightFace) places the
# eyes 35.24 px apart inside its 112x112 crop, so ~35 px inter-ocular is the
# resolution below which `align_face` starts UPSAMPLING into the template
# rather than downsampling into it — i.e. interpolating detail that was never
# captured. Feeding the face stack a crop already squeezed to 512 long-edge
# knocked a 41 px inter-ocular face down to ~25 px for exactly the near
# subjects we most want to recognise. OSNet cannot tell the difference (it
# resizes to 256x128 regardless), so the cap was buying nothing and costing
# the only path that needed the pixels.


def _crop_for_embedding(
    frame: np.ndarray, bbox: tuple[float, float, float, float]
) -> np.ndarray | None:
    """Return a native-aspect, native-resolution uint8 RGB crop, or None if the
    bbox is too small / off-frame. RGB-frame path. Callers cap it themselves —
    see the note above on why the face path must not be."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(w - 1, int(x1)))
    y1 = max(0, min(h - 1, int(y1)))
    x2 = max(0, min(w, int(x2)))
    y2 = max(0, min(h, int(y2)))
    if x2 - x1 < MIN_BBOX_SIDE_PX or y2 - y1 < MIN_BBOX_SIDE_PX:
        return None
    return frame[y1:y2, x1:x2]


def _crop_for_embedding_nv12(
    nv12: np.ndarray,
    bbox: tuple[float, float, float, float],
    logical_h: int,
    logical_w: int,
) -> np.ndarray | None:
    """Same contract as `_crop_for_embedding` but slices an NV12 SHM frame
    and converts ONLY the bbox crop to RGB (instead of converting the whole
    frame upfront). With ~1-2 Hz debounced track updates across all cameras
    and a typical person crop of ~100×300 px, this drops the YUV→RGB work
    by 100-1000× compared to the full-frame bridge.

    NV12 layout in `nv12` is the flat (H + H/2, W) uint8 buffer the
    ingestor's NV12 decoder writes — Y plane in the top H rows, interleaved
    UV plane in the bottom H/2 rows. Crop coordinates are clamped to picture
    bounds AND rounded to even values to keep chroma subsampling aligned;
    odd-edge slicing would split a (U, V) pair across the boundary and
    feed cvtColor a torn buffer.
    """
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(logical_w - 1, int(x1)))
    y1 = max(0, min(logical_h - 1, int(y1)))
    x2 = max(0, min(logical_w, int(x2)))
    y2 = max(0, min(logical_h, int(y2)))
    if x2 - x1 < MIN_BBOX_SIDE_PX or y2 - y1 < MIN_BBOX_SIDE_PX:
        return None

    # Snap to even boundaries. Round x1/y1 down, x2/y2 up but clamp; if the
    # clamp lands odd, step back. Crop may shrink by up to 1 px per edge —
    # immaterial vs the bbox uncertainty already in the tracker output.
    x1 &= ~1
    y1 &= ~1
    if x2 % 2:
        x2 = min(x2 + 1, logical_w)
        if x2 % 2:
            x2 -= 1
    if y2 % 2:
        y2 = min(y2 + 1, logical_h)
        if y2 % 2:
            y2 -= 1
    if x2 - x1 < MIN_BBOX_SIDE_PX or y2 - y1 < MIN_BBOX_SIDE_PX:
        return None

    crop_h = y2 - y1
    crop_w = x2 - x1

    # Y plane crop sits in the top H rows of the flat buffer.
    y_crop = nv12[y1:y2, x1:x2]
    # UV plane crop: UV rows live at offset logical_h, halved vertically.
    # The horizontal column space is the same byte stride as Y (each UV row
    # holds W bytes = W/2 interleaved (U, V) pairs), so we slice with the
    # same x1:x2 column range — no halving on x.
    uv_top = logical_h + y1 // 2
    uv_bottom = logical_h + y2 // 2
    uv_crop = nv12[uv_top:uv_bottom, x1:x2]

    # Re-stack the two planes into a contiguous NV12 mini-buffer so that
    # cv2.cvtColor's NV12 reader sees the layout it expects.
    nv12_crop = np.empty((crop_h + crop_h // 2, crop_w), dtype=np.uint8)
    nv12_crop[:crop_h, :] = y_crop
    nv12_crop[crop_h:, :] = uv_crop
    return cv2.cvtColor(nv12_crop, cv2.COLOR_YUV2RGB_NV12)


# --- service core ----------------------------------------------------------


def _crop_tracks(
    ring_frame, eligible: list[_TrackWire]
) -> tuple[list[np.ndarray], list[np.ndarray], list[_TrackWire]]:
    """(capped crops, full-resolution crops, their tracks) for the tracks whose
    box yields a crop, index-aligned."""
    crops: list[np.ndarray] = []
    # Same crop at full ring resolution, kept only for the face pass. The
    # body backend and the gallery JPEG both want the capped version (OSNet
    # resizes to 256x128 anyway, and an uncapped JPEG per sample would grow
    # the media tier for no gain); the face stack wants every pixel the
    # camera gave us — see the note above _crop_for_embedding.
    face_src: list[np.ndarray] = []
    meta: list[_TrackWire] = []
    # NV12 SHM frames get cropped chroma-aligned and converted to RGB
    # one bbox at a time; RGB frames take the straight slice path.
    if ring_frame.pixel_format == "nv12":
        logical_h = ring_frame.height
        logical_w = ring_frame.width
        for t in eligible:
            crop = _crop_for_embedding_nv12(
                ring_frame.pixels,
                (t.x1, t.y1, t.x2, t.y2),
                logical_h,
                logical_w,
            )
            if crop is None:
                continue
            face_src.append(crop)
            crops.append(cap_long_edge(crop))
            meta.append(t)
    else:
        for t in eligible:
            crop = _crop_for_embedding(ring_frame.pixels, (t.x1, t.y1, t.x2, t.y2))
            if crop is None:
                continue
            face_src.append(crop)
            crops.append(cap_long_edge(crop))
            meta.append(t)
    return crops, face_src, meta


class Embedder:
    def __init__(
        self,
        dsn: str,
        nats_url: str,
        backend: EmbeddingBackend,
        media_root: Path,
        face_stack: FaceStack | None = None,
        face_model_key: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._nats_url = nats_url
        self._pool: asyncpg.Pool | None = None
        self._nc = None
        self._cameras = _CameraResolver()
        self._debounce: dict[tuple[str, int], _TrackDebounce] = {}
        self._msgs_since_gc = 0
        self._readers: dict[str, FrameRingReader] = {}
        self._backend = backend
        self._face = face_stack
        self._face_model_key = face_model_key
        self._face_loaded_pair: tuple[str, str] | None = None
        self._processing_lock = asyncio.Lock()
        self._reload_requested = asyncio.Event()
        self._reload_task: asyncio.Task | None = None
        self._native_task: asyncio.Task | None = None
        self._native_stop = asyncio.Event()
        self._stats = StatsCollector(service="embedder")
        self._decoder = msgspec.msgpack.Decoder(_TracksMessage)
        self._listener: ResilientListener | None = None
        self._media_root = media_root
        layout = MediaLayout(media_root)
        self._crops_dir = layout.crops
        self._face_crops_dir = layout.face_crops
        self._crops_dir.mkdir(parents=True, exist_ok=True)
        if face_stack is not None:
            self._face_crops_dir.mkdir(parents=True, exist_ok=True)

    def _reader_for(self, slug: str) -> FrameRingReader:
        r = self._readers.get(slug)
        if r is None:
            r = FrameRingReader(slug)
            self._readers[slug] = r
        return r

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        # Resilient LISTEN: initial camera-map load via on_connect, plus a
        # refresh after any DB drop so camera changes are picked up live.
        self._listener = ResilientListener(
            self._dsn,
            ["cameras_changed", "face_recognition_changed"],
            on_notify=self._on_config_changed,
            on_connect=self._reconcile,
            name="embedder-listen",
        )
        await self._listener.start()
        self._reload_task = spawn(self._reload_loop(), name="embedder-face-reload", log=log)
        self._nc = await nats_connect(self._nats_url, name="embedder")
        await self._nc.subscribe(SUBJECT_TRACKS_IN, cb=self._on_tracks)
        await self._stats.start(self._nc)
        log.info("embedder ready: subscribed to %s", SUBJECT_TRACKS_IN)

    def _on_config_changed(self, channel: str, _payload: str) -> None:
        # Refresh on the loop; can't await inside the asyncpg notify callback.
        if self._pool is None:
            return
        if channel == "face_recognition_changed":
            self._reload_requested.set()
        else:
            spawn(self._refresh_cameras())

    async def _refresh_cameras(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await self._cameras.refresh(conn)

    async def _reconcile(self) -> None:
        await self._refresh_cameras()
        await self._reload_face_settings()

    async def _reload_loop(self) -> None:
        while True:
            await self._reload_requested.wait()
            self._reload_requested.clear()
            try:
                await self._reload_face_settings()
            except Exception:
                log.exception("embedder face settings refresh failed")
                await asyncio.sleep(1.0)
                self._reload_requested.set()

    async def _reload_face_settings(self) -> None:
        assert self._pool is not None
        async with self._processing_lock:
            selection = await read_face_selection(self._pool)
            pair = (selection.detector_key, selection.model_key)
            try:
                if self._face_loaded_pair != pair:
                    path = os.environ.get("BABA_FACE_DETECTOR_MODEL", "").strip()
                    if not path:
                        raise RuntimeError("face detector is not configured")
                    stack, detector, model = await run_inference(
                        make_face_stack_for_model, yunet_path=Path(path), models_dir=Path("/models"),
                        model_key=selection.model_key, detector_key=selection.detector_key,
                    )
                    if stack is None or (detector, model) != pair:
                        raise RuntimeError("selected face models could not be loaded")
                    await self._stop_native_reader()
                    self._face, self._face_model_key = stack, model
                    self._face_loaded_pair = pair
                    self._face_crops_dir.mkdir(parents=True, exist_ok=True)
                    reader = self.native_face_reader()
                    self._native_task = spawn(
                        reader.run(self._native_stop), name="embedder-native-faces", log=log
                    )
                    log.info("face stack loaded revision=%s detector=%s model=%s",
                             selection.revision, detector, model)
            except Exception as exc:
                log.exception("embedder face activation failed")
                await acknowledge_face_selection(self._pool, "embedder", selection, str(exc))
                return
            await acknowledge_face_selection(self._pool, "embedder", selection)

    async def _stop_native_reader(self) -> None:
        if self._native_task is not None:
            self._native_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._native_task
            self._native_task = None

    def _gc_debounce(self, now_ns: int) -> None:
        # GC the debounce map. Its keys are (camera, local_track_id) and a
        # local_track_id is never reused, so without pruning the map grows
        # without bound over a long-running service (one entry per track ever
        # seen). Drop entries not touched in _DEBOUNCE_GC_MS — a track quiet
        # that long has ended; the next appearance mints a fresh key anyway.
        self._msgs_since_gc += 1
        if self._msgs_since_gc < _DEBOUNCE_GC_EVERY:
            return
        self._msgs_since_gc = 0
        cutoff = now_ns - _DEBOUNCE_GC_MS * 1_000_000
        stale = [k for k, d in self._debounce.items() if d.last_embed_ns < cutoff]
        for k in stale:
            del self._debounce[k]
        if stale:
            log.debug("debounce gc: dropped %d stale track keys", len(stale))

    def _due_tracks(self, wire: _TracksMessage, now_ns: int) -> list[_TrackWire]:
        eligible: list[_TrackWire] = []
        for t in wire.tracks:
            if t.confidence < MIN_CONFIDENCE:
                continue
            key = (wire.camera_id, t.track_id)
            d = self._debounce.get(key)
            if d is None:
                d = _TrackDebounce()
                self._debounce[key] = d
            # Motion-state-aware throttle. `active` tracks use the normal
            # 500 ms debounce. `stationary` and `parked` tracks downshift
            # to a 5-minute drift check so a parked car generates ~1
            # sample / 5 min instead of ~5 samples / s. Saves ~99 % of
            # embedder work + DB writes on long-stay scenes like Shed's
            # garage. The first sample after a state change is treated
            # as `active` so the operator gets a fresh embedding +
            # thumbnail when a track stops moving (used by the
            # event-manager object_parked event below).
            elapsed_ns = now_ns - d.last_embed_ns
            interval_ms = (
                DEBOUNCE_MS
                if t.motion_state == "active" or d.last_embed_ns == 0
                else _PARKED_REFRESH_MS
            )
            if elapsed_ns < interval_ms * 1_000_000:
                continue
            eligible.append(t)
        return eligible

    async def _on_tracks(self, msg) -> None:
        async with self._processing_lock:
            await self._process_tracks(msg)

    async def _process_tracks(self, msg) -> None:
        try:
            wire = self._decoder.decode(msg.data)
        except Exception:
            log.exception("failed to decode tracks message")
            return
        if not wire.tracks:
            return
        self._stats.incr("messages")
        self._stats.set_gauge("tracked", len(self._debounce))
        cam = await self._cameras.get(wire.camera_id)
        if cam is None:
            return  # camera deleted between dispatch and now
        now_ns = time.time_ns()
        self._gc_debounce(now_ns)
        # Pick out tracks that are due for a new embedding. Doing all the
        # filtering before the ring lookup avoids the cost of attaching
        # to a ring just to discover everything is debounced.
        eligible = self._due_tracks(wire, now_ns)
        if not eligible:
            return
        reader = self._reader_for(wire.camera_id)
        ring_frame = reader.get_by_sequence(wire.sequence)
        if ring_frame is None:
            # The frame is no longer in the ring (we're behind, or the
            # writer hasn't created the ring yet). Drop these samples; the
            # debounce timestamps stay unset so the very next track tick
            # is eligible to retry.
            return
        crops, face_src, meta = _crop_tracks(ring_frame, eligible)
        if not crops:
            return
        # Inference is synchronous (OSNet on the OpenVINO GPU backend) and a
        # batch still costs tens of ms; running it inline blocks
        # NATS callback dispatch and the per-track debounce timestamps
        # drift. Punt to the default executor so the loop stays live.
        self._stats.incr("crops", len(crops))
        with self._stats.timer("embed_ms"):
            embeddings = await run_inference(self._backend.embed, crops)
        self._stats.incr("embeddings_out", len(crops))
        # Stamp debounce only on tracks we actually embedded.
        for t in meta:
            self._debounce[(wire.camera_id, t.track_id)].last_embed_ns = now_ns
        await self._persist(cam.id, wire, meta, embeddings, crops, face_src)

    async def _persist(
        self,
        camera_uuid: UUID,
        wire: _TracksMessage,
        meta: list[_TrackWire],
        embeddings: np.ndarray,
        crops: list[np.ndarray],
        face_src: list[np.ndarray],
    ) -> None:
        """`crops` are long-edge capped (body embedding + gallery JPEG);
        `face_src` is the same crop at full ring resolution, index-aligned, and
        is what the face pass must see — see the note above
        `_crop_for_embedding` for why the cap is fatal to face recognition."""
        assert self._pool is not None
        if (
            len(meta) != embeddings.shape[0]
            or len(meta) != len(crops)
            or len(meta) != len(face_src)
        ):
            log.warning(
                "meta/embedding/crop/face_src count mismatch: %d vs %d vs %d vs %d",
                len(meta),
                embeddings.shape[0],
                len(crops),
                len(face_src),
            )
            return
        # Use the *observation* wall-clock (timestamp_ns from the track msg,
        # ultimately from ingestor's publish time) instead of DB now(). That
        # keeps `captured_at` aligned with `tracks.started_at/ended_at` which
        # event-manager derives from the same timestamps — so the claim
        # window matches even if the embedder is N seconds backed up by
        # synchronous OSNet/face inference.
        captured_at = datetime.fromtimestamp(wire.timestamp_ns / 1e9, tz=UTC)
        # Pre-mint sample uuids so the JPEG filename and the DB row's
        # primary key match — makes orphan cleanup ("delete file where no
        # DB row exists") and forensic lookups trivial.
        sample_ids = [uuid4() for _ in meta]

        # Face pass — only for person crops, only when the stack is loaded.
        # Runs in a thread executor so the event loop stays responsive while
        # YuNet + AuraFace work. Skipping on non-person classes avoids
        # spurious "face detected on a car windshield" matches.
        face_vecs: list[np.ndarray | None] = [None] * len(meta)
        face_rels: list[str | None] = [None] * len(meta)
        # Per-sample face QUALITY. The detector hands us both and we used to
        # drop them on the floor, which left event-manager ranking a track's
        # faces by `confidence` — the PERSON box's score, which says nothing
        # about the face. A track yields ~10 face samples of which typically one
        # is a real frontal face and the rest are ears/shoulders/backs of heads
        # that cleared the threshold; ranking by person confidence discarded the
        # good one at random. Keep them (migration 060).
        face_scores: list[float | None] = [None] * len(meta)
        face_pxs: list[float | None] = [None] * len(meta)
        face_frontalities: list[float | None] = [None] * len(meta)
        # Whether the detector still finds a face once the crop is warped onto
        # the template. The residual above asks whether five points FIT that
        # template, which a head of hair manages; this asks whether the warped
        # IMAGE is a face, which it does not. Migration 099.
        face_realigneds: list[bool | None] = [None] * len(meta)
        if self._face is not None:
            face_indices = [i for i, t in enumerate(meta) if t.class_id == _PERSON_CLASS_ID]
            if face_indices:

                def _run_faces():
                    out: list[
                        tuple[int, np.ndarray, np.ndarray, float, float, float | None, bool]
                    ] = []
                    for i in face_indices:
                        # FaceStack.embed_from_crop returns (vec, bbox, score)
                        # or None when no usable face. We also need the
                        # aligned 112x112 to save it as a face_crops JPEG, and
                        # the score/geometry to rank this sample against the
                        # track's others — so do detection + alignment + embed
                        # inline and keep all of it.
                        from baba_core.face import align_face, frontality_residual

                        det = self._face.detector.detect(face_src[i])
                        if det is None:
                            continue
                        aligned = align_face(face_src[i], det.landmarks)
                        vec = self._face.embedder.embed(aligned)
                        x1, y1, x2, y2 = det.bbox
                        out.append((i, vec, aligned, float(det.score), min(x2 - x1, y2 - y1),
                                    frontality_residual(det.landmarks),
                                    self._face.detector.detect(aligned) is not None))
                    return out

                results = await run_inference(_run_faces)
                for i, vec, aligned, score, px, frontality, realigned in results:
                    face_scores[i] = score
                    face_pxs[i] = px
                    face_frontalities[i] = frontality
                    face_realigneds[i] = realigned
                    sid = sample_ids[i]
                    face_rel = MediaLayout.rel(FACE_CROPS, f"{sid}.jpg")
                    face_abs = self._media_root / face_rel
                    try:
                        cv2.imwrite(
                            str(face_abs),
                            cv2.cvtColor(aligned, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, 90],
                        )
                    except Exception:
                        log.exception("failed to write face crop %s", face_abs)
                        face_rel = None
                    face_vecs[i] = vec
                    face_rels[i] = face_rel

        self._stats.incr("faces_out", sum(1 for v in face_vecs if v is not None))
        rows: list[tuple] = []
        for i, t in enumerate(meta):
            sid = sample_ids[i]
            crop_rel = MediaLayout.rel(CROPS, f"{sid}.jpg")
            crop_abs = self._media_root / crop_rel
            try:
                cv2.imwrite(
                    str(crop_abs),
                    cv2.cvtColor(crops[i], cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 85],
                )
            except Exception:
                log.exception("failed to write crop jpeg %s", crop_abs)
                self._stats.incr("crop_write_errors")
                crop_rel = None
            face_lit = vector_literal(face_vecs[i]) if face_vecs[i] is not None else None
            face_model_key = self._face_model_key if face_lit is not None else None
            rows.append(
                (
                    sid,
                    camera_uuid,
                    int(t.track_id),
                    int(wire.pts_ns),
                    int(wire.sequence),
                    float(t.confidence),
                    [float(t.x1), float(t.y1), float(t.x2), float(t.y2)],
                    vector_literal(embeddings[i]),
                    captured_at,
                    crop_rel,
                    face_lit,
                    face_rels[i],
                    face_model_key,
                    face_scores[i],
                    face_pxs[i],
                    t.motion_state in ("stationary", "parked"),
                    face_frontalities[i],
                    face_realigneds[i],
                )
            )
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO track_embedding_samples
                    (id, camera_id, local_track_id, pts_ns, sequence,
                     confidence, bbox, embedding, captured_at, crop_path,
                     face_embedding, face_crop_path, face_embedding_model,
                     face_score, face_px, at_rest, face_frontality,
                     face_realigned)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9, $10,
                        $11::vector, $12, $13, $14, $15, $16, $17, $18)
                """,
                rows,
            )
        self._stats.incr("samples_persisted", len(rows))

    def native_face_reader(self) -> NativeFaceReader | None:
        """The re-reader, or None when this build carries no face stack."""
        if self._face is None or self._pool is None:
            return None
        return NativeFaceReader(
            self._pool, self._media_root, self._face,
            self._face_model_key, self._face_crops_dir,
            lock=self._processing_lock,
        )

    async def stop(self) -> None:
        if self._reload_task is not None:
            self._reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reload_task
        self._native_stop.set()
        await self._stop_native_reader()
        await self._stats.stop()
        for r in self._readers.values():
            r.detach()
        self._readers.clear()
        if self._listener is not None:
            await self._listener.stop()
        if self._nc is not None:
            # Drain can raise ConnectionReconnectingError when NATS is
            # already in the process of reconnecting (the path that
            # previously killed the embedder during shutdown without
            # triggering docker's `restart: unless-stopped` policy,
            # because the exception bubbled through the main coroutine
            # and looked like a clean stop to dockerd). Swallow it —
            # we're already shutting down, the connection state doesn't
            # matter.
            try:
                await drain_quietly(self._nc)
            except Exception:
                log.exception("nats drain raised during shutdown — ignored")
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                log.exception("asyncpg pool close raised during shutdown — ignored")


async def _run() -> None:
    dsn = dsn_from_env()
    nats_url = os.environ.get("BABA_NATS_URL", "nats://nats:4222")
    model_env = os.environ.get("BABA_EMBEDDER_MODEL", "").strip()
    model_path = Path(model_env) if model_env else None
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    backend = make_backend(model_path)
    media_root = Path(os.environ.get("BABA_MEDIA_PATH", "/media"))
    embedder = Embedder(dsn, nats_url, backend, media_root=media_root)
    await embedder.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tick_task = spawn(HealthMarker("baba", "embedder").run_loop(), name="embedder-health", log=log)
    try:
        await stop.wait()
    finally:
        tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick_task
        await embedder.stop()


def main() -> None:
    setup_logging("embedder")
    run_service(_run())


if __name__ == "__main__":
    main()
