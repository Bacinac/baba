"""Shared base for ffmpeg-subprocess video decoders.

Both the NVIDIA (NVDEC/cuvid) and Intel (QSV/VAAPI) hardware decoders run an
ffmpeg subprocess that demuxes RTSP, decodes on vendor silicon, downscales in
the vendor's frame context, downloads to host memory and writes fixed-size raw
frames (RGB24 or flat NV12) to a pipe. Everything except the vendor-specific
ffmpeg argv is identical between them: RTSP ffprobe, aspect-preserving
downscale maths, subprocess lifecycle, the fixed-size read loop with the
RGB/NV12 reshape, stderr draining, and restart-safe teardown.

That common machinery lives here so a new hardware decoder is just:
  - a codec → hardware-decoder-name map (`_decoder_for_codec`)
  - an `is_available()` probe
  - `_build_ffmpeg_args()` returning the vendor argv

See backends/decoder-ffmpeg-nvdec (CUDA) and backends/decoder-qsv (Intel) for
the two concrete implementations.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from abc import abstractmethod
from collections.abc import AsyncIterator

import numpy as np
from home_core.tasks import spawn

from baba_core.url import mask_credentials
from baba_core.video import DecoderConfig, VideoDecoder

log = logging.getLogger(__name__)

# A reader joins a live stream mid-GOP, and the decoder reports every picture
# before the next keyframe as missing its references — HEVC by name, H.264 under
# VAAPI as the hwaccel's EINVAL. Before the first frame that is expected; after
# it, the same line means frames are being lost.
_JOINED_MID_GOP = (
    "error constructing the frame rps",
    "could not find ref with poc",
    "decoding error: invalid argument",
)


def scaled_dims(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """Downscaled (W, H) preserving aspect ratio with a max long edge. Rounded
    to even values because hardware YUV scalers don't handle odd dimensions
    cleanly (chroma subsampling)."""
    if max_edge <= 0 or max(width, height) <= max_edge:
        return (width // 2) * 2, (height // 2) * 2
    if width >= height:
        new_w = max_edge
        new_h = max(2, round(height * max_edge / width))
    else:
        new_h = max_edge
        new_w = max(2, round(width * max_edge / height))
    return (new_w // 2) * 2, (new_h // 2) * 2


async def ffprobe_stream(
    url: str, options: dict[str, str] | None = None
) -> tuple[str, int, int]:
    """Ask ffprobe for the first video stream's codec and dimensions. We need
    these before we can pick the `<codec>_<hw>` decoder and compute the scale
    target size — cheaper than opening a real decoder twice.

    Also probes local files (recorded segments), where the transport options
    below are meaningless — `-rtsp_transport` is a private option of the RTSP
    demuxer and ffprobe rejects it outright on an mp4 input, so it is applied
    only to actual RTSP URLs.
    """
    options = options or {}
    args = ["ffprobe", "-hide_banner", "-loglevel", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        # ffmpeg 8.x renamed the old -stimeout to -timeout (microseconds).
        # Accept either spelling from callers that still mirror PyAV's libav
        # option names.
        socket_timeout_us = (
            options.get("timeout") or options.get("stimeout") or "5000000"
        )
        args += [
            "-rtsp_transport",
            options.get("rtsp_transport", "tcp"),
            "-timeout",
            socket_timeout_us,
        ]
    args += [
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "v:0",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ffprobe timeout for {url!r}") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}): {err.decode(errors='replace').strip()}"
        )
    data = json.loads(out)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"ffprobe found no video stream for {url!r}")
    s = streams[0]
    codec = s.get("codec_name", "")
    width = int(s.get("width") or 0)
    height = int(s.get("height") or 0)
    if not codec or width <= 0 or height <= 0:
        raise RuntimeError(
            f"ffprobe returned incomplete stream info: codec={codec!r} {width}x{height}"
        )
    return codec, width, height


class FFmpegSubprocessDecoder(VideoDecoder):
    """Vendor-neutral ffmpeg-subprocess decoder. Subclasses supply the codec
    map, availability probe and argv; this base owns everything else.

    Frames leave ffmpeg as NV12 (the vf chain ends with format=nv12), so no
    colourspace work runs on the CPU: YUV→RGB happens in the detector's graph
    on the GPU, or in a small downstream cv2 call for crops. It also halves the
    pipe transfer against RGB (1.5 vs 3 bytes/pixel)."""

    # ---------- subclass hooks ----------

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Probe: ffmpeg on PATH and built with this backend's hw decoders.
        Must NOT raise on missing hardware — return False."""

    @abstractmethod
    def _decoder_for_codec(self, codec: str) -> str | None:
        """Map an ffprobe codec name to the vendor hw decoder (e.g.
        'h264' → 'h264_qsv'), or None if this backend can't decode it."""

    @abstractmethod
    def _build_ffmpeg_args(
        self,
        *,
        url: str,
        hw_decoder: str,
        options: dict[str, str],
        target_fps: int,
        out_w: int,
        out_h: int,
    ) -> list[str]:
        """Return the full ffmpeg argv (list) for this backend's hw path, with
        NV12 rawvideo on stdout."""

    # ---------- shared machinery ----------

    def __init__(self) -> None:
        self._config: DecoderConfig | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._out_w: int = 0
        self._out_h: int = 0
        self._frame_bytes: int = 0
        self._t0_ns: int = 0
        self._decoding: bool = False

    async def open(self, config: DecoderConfig) -> None:
        self._config = config
        self._decoding = False
        options = dict(config.options or {})

        codec, in_w, in_h = await ffprobe_stream(config.url, options)
        hw_decoder = self._decoder_for_codec(codec)
        if hw_decoder is None:
            raise RuntimeError(
                f"{self.capabilities.name}: no hardware decoder for codec {codec!r}"
            )

        out_w, out_h = scaled_dims(in_w, in_h, config.max_edge)
        self._out_w = out_w
        self._out_h = out_h
        if out_w % 2 or out_h % 2:
            raise RuntimeError(f"NV12 needs even dimensions; got {out_w}x{out_h}")
        self._frame_bytes = out_w * out_h * 3 // 2

        args = self._build_ffmpeg_args(
            url=config.url,
            hw_decoder=hw_decoder,
            options=options,
            target_fps=config.target_fps,
            out_w=out_w,
            out_h=out_h,
        )

        # limit= sets the StreamReader internal buffer. Default 64 KiB causes
        # many small reads for multi-MiB frames; 16 MiB keeps reads near-zero-copy.
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self._stderr_task = spawn(
            self._drain_stderr(),
            name=f"{self.capabilities.name}-stderr-{mask_credentials(config.url)}",
            log=log,
        )
        self._t0_ns = time.monotonic_ns()

        log.info(
            "%s opened: codec=%s decoder=%s in=%dx%d out=%dx%d target_fps=%d",
            self.capabilities.name,
            codec,
            hw_decoder,
            in_w,
            in_h,
            out_w,
            out_h,
            config.target_fps,
        )

    async def frames(
        self,
        stop: asyncio.Event,
    ) -> AsyncIterator[tuple[np.ndarray, int]]:
        if self._proc is None or self._config is None:
            raise RuntimeError("open() must be called before frames()")
        proc = self._proc
        out_w, out_h = self._out_w, self._out_h
        frame_bytes = self._frame_bytes
        t0_ns = self._t0_ns

        frame_deadline = time.monotonic() + 60.0
        while not stop.is_set():
            if proc.returncode is not None:
                # ffmpeg exited (EOF, RTSP timeout, decode error). Let the
                # worker's outer reconnect loop reopen us.
                return
            try:
                # Timeout serves two purposes: respond to stop within a bounded
                # interval, and detect a wedged ffmpeg (network stall) without
                # leaking the task. 5s is well over the worst-case frame interval.
                buf = await asyncio.wait_for(
                    proc.stdout.readexactly(frame_bytes),
                    timeout=min(5.0, max(0.001, frame_deadline - time.monotonic())),
                )
            except TimeoutError:
                if stop.is_set():
                    return
                if time.monotonic() >= frame_deadline:
                    raise RuntimeError("FFmpeg stopped producing decoded frames") from None
                continue
            except asyncio.IncompleteReadError:
                # ffmpeg closed stdout — EOF or process died.
                return

            # np.frombuffer returns a view; the StreamReader recycles the bytes
            # once we yield control. .copy() detaches so the shm ring / publisher
            # can hold the array without racing the next read.
            # NV12 is (H + H/2, W) uint8 flat; Y plane first, UV second.
            self._decoding = True
            frame_deadline = time.monotonic() + 10.0
            array = np.frombuffer(buf, dtype=np.uint8).reshape(out_h + out_h // 2, out_w).copy()
            pts_ns = time.monotonic_ns() - t0_ns
            yield array, pts_ns

    async def _drain_stderr(self) -> None:
        """Read ffmpeg's stderr line-by-line so the pipe buffer never fills
        (which would deadlock the producer). Surfaces decoder errors to our
        log — useful when a camera flips codec or drops keyframes."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                msg = line.decode(errors="replace").rstrip()
                if not msg:
                    continue
                lowered = msg.lower()
                if not self._decoding and any(m in lowered for m in _JOINED_MID_GOP):
                    log.debug("ffmpeg: %s", msg)
                elif "error" in lowered or "fatal" in lowered:
                    log.warning("ffmpeg: %s", msg)
                else:
                    log.debug("ffmpeg: %s", msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ffmpeg stderr drain failed")

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
