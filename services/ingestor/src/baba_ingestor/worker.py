from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

import numpy as np
from baba_core import (
    DecoderConfig,
    FrameRingWriter,
    StatsCollector,
    VideoDecoder,
    mask_credentials,
)
from baba_core.wire import IngestorTelemetry
from home_core.tasks import spawn

from baba_ingestor.config import CameraSpec
from baba_ingestor.publisher import NATSFramePublisher

log = logging.getLogger(__name__)

_NORMAL_CAP = 30.0
_DEGRADED_CAP = 300.0
_DEGRADE_AFTER = 10

# Adaptive-fps control-loop tuning (only relevant to cameras with idle_fps
# set). ACTIVE_HOLD is the hysteresis: how long after the last "active"
# verdict the camera keeps publishing at target_fps before decaying to
# idle_fps — asymmetric on purpose (ramp-up is instant, decay is lazy) so a
# subject that pauses briefly doesn't flap the rate. ACTIVITY_STALE is the
# fail-loud guard: verdicts older than this mean the tracker/pipeline feedback
# is broken, and the worker falls back to FULL rate — a broken control loop
# must degrade cost, never detection coverage.
_ACTIVE_HOLD_S = float(os.environ.get("BABA_INGESTOR_ACTIVE_HOLD_S", "15"))
_ACTIVITY_STALE_S = float(os.environ.get("BABA_INGESTOR_ACTIVITY_STALE_S", "10"))

# Scene-light sampling cadence for telemetry. One subsampled reduction per
# 10 s is invisible in the CPU profile but gives 6 light/IR points per
# telemetry bucket — plenty to place a day/night flip within a minute.
_LIGHT_SAMPLE_S = 10.0
# Mean |chroma - neutral| below which the picture is considered monochrome,
# i.e. the IR-cut filter is open and the camera is in night-IR mode. Real
# color scenes measure well above this even in dull weather; IR frames sit
# near 0 with only compression noise.
_IR_CHROMA_DEV = 2.0


class CameraWorker:
    """One asyncio task per camera. Owns its decoder + NATS publisher.

    The decoder is the one `VideoDecoder` plugin the supervisor bound for this
    variant. The worker doesn't know or care which one — it just opens,
    iterates, closes.
    """

    def __init__(
        self,
        spec: CameraSpec,
        nats_url: str,
        decoder_cls: type[VideoDecoder],
        *,
        stats: StatsCollector | None = None,
    ) -> None:
        self.spec = spec
        self._nats_url = nats_url
        self._decoder_cls = decoder_cls
        self._stats = stats
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Adaptive-fps state, fed by the supervisor's baba.activity.* routing.
        # Same event loop as the frame task — plain attributes, no locking.
        self._last_signal_ns = 0
        self._last_active_ns = 0
        self._idle_mode = False
        self._stale_warned = False
        self._last_kept_ref_ns = 0
        # The frame ring is lazy-created on the first decoded frame because
        # we don't know the post-downscale dimensions until then. Embedder
        # and detector attach by camera slug — both containers share
        # ingestor's IPC namespace (compose: `ipc: service:ingestor`).
        self._ring: FrameRingWriter | None = None
        self._sequence = 0
        # Telemetry window counters — drained once a minute by the
        # supervisor's telemetry loop into baba.telemetry.ingestor.<slug>.
        self._tel_mode_since = time.monotonic()
        self._tel_active_s = 0.0
        self._tel_idle_s = 0.0
        self._tel_transitions = 0
        self._tel_frames_out = 0
        self._tel_dropped_idle = 0
        self._tel_light_last = 0.0
        self._tel_luma_sum = 0.0
        self._tel_chroma_sum = 0.0
        self._tel_light_n = 0
        self._tel_ir_n = 0

    def _sample_light(self, frame: np.ndarray, fmt: str, h: int) -> None:
        """Measure scene brightness + IR mode from the decoded frame itself
        (cameras only expose their configured day/night mode, not the live
        state). Heavily subsampled — runs every _LIGHT_SAMPLE_S, not per
        frame."""
        now = time.monotonic()
        if now - self._tel_light_last < _LIGHT_SAMPLE_S:
            return
        self._tel_light_last = now
        try:
            if fmt == "nv12":
                luma = float(frame[:h:8, ::8].mean())
                chroma = float(np.abs(frame[h::4, ::4].astype(np.int16) - 128).mean())
            else:
                sub = frame[::8, ::8].astype(np.int16)
                luma = float(sub.mean())
                # Saturation proxy on RGB: per-pixel channel spread. Halved
                # so the IR threshold is comparable to the NV12 chroma path.
                chroma = float((sub.max(axis=2) - sub.min(axis=2)).mean()) / 2.0
        except Exception:
            log.exception("light sample failed: cam=%s", self.spec.slug)
            return
        self._tel_luma_sum += luma
        self._tel_chroma_sum += chroma
        self._tel_light_n += 1
        if chroma < _IR_CHROMA_DEV:
            self._tel_ir_n += 1

    def drain_telemetry(self, window_s: float) -> IngestorTelemetry:
        """Close out the current mode's running duration and hand back this
        window's counters, resetting them for the next window."""
        now = time.monotonic()
        elapsed = now - self._tel_mode_since
        if self._idle_mode:
            self._tel_idle_s += elapsed
        else:
            self._tel_active_s += elapsed
        self._tel_mode_since = now
        n = self._tel_light_n
        msg = IngestorTelemetry(
            camera_id=self.spec.slug,
            window_s=window_s,
            active_s=self._tel_active_s,
            idle_s=self._tel_idle_s,
            transitions=self._tel_transitions,
            frames_out=self._tel_frames_out,
            frames_dropped_idle=self._tel_dropped_idle,
            luma_avg=self._tel_luma_sum / n if n else -1.0,
            chroma_dev_avg=self._tel_chroma_sum / n if n else -1.0,
            ir_ratio=self._tel_ir_n / n if n else -1.0,
        )
        self._tel_active_s = 0.0
        self._tel_idle_s = 0.0
        self._tel_transitions = 0
        self._tel_frames_out = 0
        self._tel_dropped_idle = 0
        self._tel_luma_sum = 0.0
        self._tel_chroma_sum = 0.0
        self._tel_light_n = 0
        self._tel_ir_n = 0
        return msg

    def note_activity(self, active: bool) -> None:
        """Record the tracker's latest scene-activity verdict for this camera.
        Called by the supervisor for every ActivityMessage on the camera's
        subject; the frame loop reads these timestamps to pick its rate."""
        now = time.time_ns()
        self._last_signal_ns = now
        if active:
            self._last_active_ns = now

    def _keep_frame(self, pts_ns: int) -> bool:
        """Adaptive decimation verdict for one decoded frame.

        Rate selection: full `target_fps` while the tracker's activity feed is
        fresh and reported something active within the ACTIVE_HOLD window (or
        whenever the feed is stale/absent — fail toward coverage); `idle_fps`
        otherwise. Pacing uses decoder PTS when available so the kept-frame
        cadence is stream-time-accurate; a PTS that jumps backwards (camera
        reconnect) resets the pacing baseline."""
        now_ns = time.time_ns()
        if self._last_signal_ns == 0 or now_ns - self._last_signal_ns > int(
            _ACTIVITY_STALE_S * 1e9
        ):
            idle = False
            if not self._stale_warned and self._last_signal_ns != 0:
                log.warning(
                    "cam=%s activity feed stale (>%.0fs) — forcing full rate "
                    "until tracker verdicts resume",
                    self.spec.slug,
                    _ACTIVITY_STALE_S,
                )
                self._stale_warned = True
        else:
            self._stale_warned = False
            idle = now_ns - self._last_active_ns > int(_ACTIVE_HOLD_S * 1e9)
        if idle != self._idle_mode:
            now_mono = time.monotonic()
            elapsed = now_mono - self._tel_mode_since
            if self._idle_mode:
                self._tel_idle_s += elapsed
            else:
                self._tel_active_s += elapsed
            self._tel_mode_since = now_mono
            self._tel_transitions += 1
            self._idle_mode = idle
            log.info(
                "cam=%s adaptive rate → %s (%d fps)",
                self.spec.slug,
                "idle" if idle else "active",
                self.spec.idle_fps if idle else self.spec.target_fps,
            )
            if self._stats is not None:
                self._stats.incr("fps_transitions")
        ref_ns = pts_ns if pts_ns > 0 else now_ns
        if self._last_kept_ref_ns and ref_ns < self._last_kept_ref_ns:
            self._last_kept_ref_ns = 0
        if not idle:
            self._last_kept_ref_ns = ref_ns
            return True
        interval_ns = int(1e9 // self.spec.idle_fps)
        # Half a source-frame of tolerance so decoder jitter can't push every
        # keep-decision one frame late and drift the idle cadence below spec.
        tol_ns = int(5e8 // self.spec.target_fps)
        if self._last_kept_ref_ns and ref_ns - self._last_kept_ref_ns < interval_ns - tol_ns:
            return False
        self._last_kept_ref_ns = ref_ns
        return True

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = spawn(self._run(), name=f"ingestor-{self.spec.slug}", log=log)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                log.warning("worker %s did not stop in 10s; cancelling", self.spec.slug)
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    def _adaptive_rate(self) -> bool:
        adaptive = self.spec.idle_fps is not None and 0 < self.spec.idle_fps < self.spec.target_fps
        if self.spec.idle_fps is not None and not adaptive:
            log.warning(
                "cam=%s idle_fps=%s is not below target_fps=%d — adaptive rate "
                "disabled, publishing at constant target_fps",
                self.spec.slug,
                self.spec.idle_fps,
                self.spec.target_fps,
            )
        return adaptive

    async def _run(self) -> None:
        # camera_id in NATS messages is the slug — stable, human-readable,
        # already used by the existing detector subscription pattern. The DB
        # uuid stays in the DB.
        publisher = NATSFramePublisher(self._nats_url, camera_id=self.spec.slug)
        await publisher.connect()
        adaptive = self._adaptive_rate()
        self._sequence = 0
        log.info(
            "worker started: camera=%s url=%s target_fps=%d idle_fps=%s max_edge=%d",
            self.spec.slug,
            mask_credentials(self.spec.stream_url),
            self.spec.target_fps,
            self.spec.idle_fps if adaptive else "off",
            self.spec.downscale_max_edge,
        )

        backoff_s = 1.0
        # Circuit breaker for a persistently-dead camera (wrong URL, decommis-
        # sioned but still configured). We never HARD give up — a camera can
        # come back after hours — but after DEGRADE_AFTER consecutive attempts
        # that yielded ZERO frames we stretch the backoff cap from 30s to 5min
        # and stop logging a full traceback every cycle. A camera that streams
        # even briefly resets the streak. (A camera that's actually removed is
        # handled separately: the supervisor reconciles on cameras_changed.)
        fail_streak = 0
        try:
            # Outer retry loop: a clean RTSP EOF, a transient camera reboot,
            # or a libav exception used to permanently kill the worker
            # (the supervisor only reconciles on `cameras_changed` NOTIFY,
            # never on worker death). Now we reopen the decoder with escalating
            # backoff. The shm ring sticks around between attempts so consumers
            # don't see it disappear on every reconnect.
            while not self._stop.is_set():
                frames = await self._stream_once(publisher, adaptive, fail_streak)
                if self._stop.is_set():
                    break
                if self._stats is not None:
                    self._stats.incr("reconnects")
                if frames > 0:
                    # Camera streamed this attempt → alive; treat as a normal
                    # transient reconnect (fast retry, 30s cap).
                    fail_streak = 0
                    backoff_s = 1.0
                else:
                    # Zero frames → open failed or died immediately. Escalate.
                    fail_streak += 1
                    if fail_streak == _DEGRADE_AFTER:
                        log.warning(
                            "cam=%s unreachable after %d attempts — backing off to %.0fs "
                            "(will keep retrying so it recovers if the camera returns)",
                            self.spec.slug,
                            fail_streak,
                            _DEGRADED_CAP,
                        )
                cap = _DEGRADED_CAP if fail_streak >= _DEGRADE_AFTER else _NORMAL_CAP
                # go2rtc usually re-attaches within a few seconds, so a live
                # camera reconnects fast; a dead one settles at the degraded cap.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff_s)
                backoff_s = min(backoff_s * 2, cap)
        finally:
            if self._ring is not None:
                self._ring.close()
                self._ring = None
            await publisher.close()
            log.info("worker stopped: camera=%s frames=%d", self.spec.slug, self._sequence)

    async def _stream_once(self, publisher: NATSFramePublisher, adaptive: bool, fail_streak: int) -> int:
        """One decoder session, from open to the stream ending or failing; returns
        how many frames it published."""
        frames = 0
        decoder = self._decoder_cls()
        decoder_config = DecoderConfig(
            url=self.spec.stream_url,
            target_fps=self.spec.target_fps,
            max_edge=self.spec.downscale_max_edge,
        )
        try:
            await decoder.open(decoder_config)
            log.info(
                "decoder open: cam=%s backend=%s",
                self.spec.slug,
                decoder.capabilities.name,
            )
            # Fresh decoder → fresh PTS timeline; restart idle pacing.
            self._last_kept_ref_ns = 0
            async for frame_array, pts_ns in decoder.frames(self._stop):
                if adaptive and not self._keep_frame(pts_ns):
                    self._tel_dropped_idle += 1
                    if self._stats is not None:
                        self._stats.incr("frames_dropped_idle")
                    continue
                await self._emit(publisher, decoder.capabilities.output_pixel_format, frame_array, pts_ns)
                frames += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._stats is not None:
                self._stats.incr("decode_errors")
            # Full traceback on the first failure of a streak, then
            # every 10th, so a persistently-dead camera doesn't spam a
            # traceback every cycle.
            if fail_streak == 0 or fail_streak % 10 == 0:
                log.exception(
                    "decoder loop failed: cam=%s (fail_streak=%d) — will reconnect",
                    self.spec.slug,
                    fail_streak + 1,
                )
        finally:
            try:
                await decoder.close()
            except Exception:
                log.exception("decoder close failed: cam=%s", self.spec.slug)
        return frames

    async def _emit(
        self, publisher: NATSFramePublisher, fmt: str, frame_array: np.ndarray, pts_ns: int
    ) -> None:
        # Decoder's capabilities tell us which layout the
        # first frame is in — RGB ndarrays carry (H, W, 3);
        # NV12 ndarrays carry the flat (H + H/2, W) shape
        # that includes the chroma plane below the luma.
        # We need the *logical* picture H,W either way for
        # the ring header.
        if fmt == "nv12":
            w = frame_array.shape[1]
            # Recover logical H from the flat layout.
            # NV12 buffer height is H + H/2 = 3H/2, so
            # H = buffer_h * 2 // 3.
            h = frame_array.shape[0] * 2 // 3
        else:
            h, w = frame_array.shape[:2]
        # Recreate the ring when the camera reconnects at a
        # different resolution (Hikvision in particular will
        # silently negotiate down on H264 streams). If we
        # kept pushing the new frame layout into a ring
        # sized for the old resolution, readers would
        # interpret bytes with the wrong row stride —
        # visible as purple/green chroma stripes in NV12
        # crops downstream.
        ring = self._ring
        if ring is not None and (ring.width != w or ring.height != h):
            log.info(
                "ring resolution change cam=%s: %dx%d → %dx%d, recreating",
                self.spec.slug,
                ring.width,
                ring.height,
                w,
                h,
            )
            try:
                ring.close()
            except Exception:
                log.exception("ring close failed: cam=%s", self.spec.slug)
            ring = None
        if ring is None:
            ring = self._ring = FrameRingWriter(
                camera_slug=self.spec.slug,
                width=w,
                height=h,
                pixel_format=fmt,
            )
        self._sample_light(frame_array, fmt, h)
        ring.push(frame_array, sequence=self._sequence, pts_ns=pts_ns)
        await publisher.publish(
            sequence=self._sequence,
            timestamp_ns=time.time_ns(),
            pixels=frame_array,
            pts_ns=pts_ns,
        )
        self._tel_frames_out += 1
        if self._stats is not None:
            self._stats.incr("frames_out")
        self._sequence += 1
