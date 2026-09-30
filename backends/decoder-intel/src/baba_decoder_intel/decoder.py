"""Intel hardware video decoders via ffmpeg subprocesses.

Two hardware paths onto the same Arc / iGPU media engine, both subclassing
baba_core.ffmpeg_decoder (RTSP demux + packet handling stay out of Python):

  QSV   — oneVPL/libvpl. What Frigate uses on this hardware; the most tuned
          Intel path. Needs an ffmpeg built --enable-libvpl and the oneVPL
          GPU runtime (libmfx-gen) present at runtime.
  VAAPI — the generic Video Acceleration API over the i915 render node. Needs
          only intel-media-va-driver + an ffmpeg built --enable-vaapi. Fully
          hardware-accelerated decode + scale on the same silicon.

Both emit the flat-NV12 frames the intel SHM ring carries. The ingestor binds
exactly one: vaapi-nv12 by default, qsv-nv12 where BABA_DECODER_BACKEND names it
(the N305 iGPU, whose VAAPI path dies every ~60 s). is_available() says whether
the image's ffmpeg carries it.

Device selection: with a single /dev/dri render node exposed to the container,
QSV auto-detects it (matching how Frigate runs here, where the passed-through
node is renderD129, not ffmpeg's default renderD128). VAAPI needs the node
named explicitly — BABA_VAAPI_DEVICE (default /dev/dri/renderD128); set it to
the exposed node. Both honour a per-stream override via DecoderConfig.options.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from baba_core.ffmpeg_decoder import FFmpegSubprocessDecoder
from baba_core.video import DecoderCapabilities

log = logging.getLogger(__name__)


def _ffmpeg_lists(kind: str) -> str:
    """Return `ffmpeg -{kind}` stdout (kind is 'decoders' or 'hwaccels'), or ''
    if ffmpeg is missing / errors. Used by is_available() probes."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return ""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", f"-{kind}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# QSV (oneVPL)
# ---------------------------------------------------------------------------

# ffprobe codec name → QSV decoder. Only codecs QSV accelerates on Arc/iGPU
# (verified against the host's vainfo VLD entrypoints: H264, HEVC incl. Main10,
# VP9, MPEG2). No vp8_qsv / mpeg4_qsv exist in ffmpeg, so a camera sending
# those cannot be opened by this decoder.
_QSV_DECODERS = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "av1": "av1_qsv",
    "vp9": "vp9_qsv",
    "mpeg2video": "mpeg2_qsv",
}


class FFmpegQsvNV12Decoder(FFmpegSubprocessDecoder):
    capabilities = DecoderCapabilities(
        name="qsv-nv12",
        hardware_accelerated=True,
        output_pixel_format="nv12",
    )

    @classmethod
    def is_available(cls) -> bool:
        """ffmpeg on PATH and built with QSV decoders. The runtime oneVPL/VAAPI
        device only matters at open() — a missing/broken node makes the spawned
        ffmpeg exit, which the worker's reconnect loop logs and retries."""
        return "h264_qsv" in _ffmpeg_lists("decoders")

    def _decoder_for_codec(self, codec: str) -> str | None:
        return _QSV_DECODERS.get(codec)

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
        rtsp_transport = options.get("rtsp_transport", "tcp")
        # See ffprobe_stream for the -stimeout → -timeout rename rationale.
        socket_timeout_us = options.get("timeout") or options.get("stimeout") or "5000000"
        # Optional explicit render node; empty → libva auto-detects the single
        # exposed /dev/dri/renderD* node (the common single-GPU case).
        qsv_device = (options.get("qsv_device") or os.environ.get("BABA_QSV_DEVICE", "")).strip()

        # fps before scale_qsv so we skip the media-engine resize for dropped
        # frames. Order: fps → scale_qsv → hwdownload → format=nv12. QSV
        # surfaces are NV12, so hwdownload lands them in host memory as they are.
        vf_chain = []
        if target_fps > 0:
            vf_chain.append(f"fps={target_fps}")
        vf_chain.append(f"scale_qsv={out_w}:{out_h}")
        vf_chain.append("hwdownload")
        vf_chain.append("format=nv12")

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+nobuffer",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1000000",
            "-filter_threads",
            "2",
            "-rtsp_transport",
            rtsp_transport,
            "-timeout",
            socket_timeout_us,
        ]
        # -qsv_device must precede -hwaccel qsv so the hwaccel picks it up.
        if qsv_device:
            args += ["-qsv_device", qsv_device]
        args += [
            "-hwaccel",
            "qsv",
            "-hwaccel_output_format",
            "qsv",
            "-c:v",
            hw_decoder,
            "-i",
            url,
            "-an",
            "-vf",
            ",".join(vf_chain),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "nv12",
            "pipe:1",
        ]
        return args



# ---------------------------------------------------------------------------
# VAAPI
# ---------------------------------------------------------------------------

# ffprobe codec → the plain ffmpeg decoder name used with `-c:v`; `-hwaccel
# vaapi` offloads it to the media engine. (VAAPI, unlike QSV, has no
# `<codec>_vaapi` decoders — the hwaccel wraps the standard decoder.)
_VAAPI_CODECS = {
    "h264": "h264",
    "hevc": "hevc",
    "vp9": "vp9",
    "mpeg2video": "mpeg2video",
}

# ffmpeg's built-in default VAAPI render node. The Arc node exposed to this
# container is renderD129, so BABA_VAAPI_DEVICE must be set in the intel .env.
_DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD128"


class FFmpegVaapiNV12Decoder(FFmpegSubprocessDecoder):
    capabilities = DecoderCapabilities(
        name="vaapi-nv12",
        hardware_accelerated=True,
        output_pixel_format="nv12",
    )

    @classmethod
    def is_available(cls) -> bool:
        """ffmpeg on PATH and built with the VAAPI hwaccel. The render node is
        checked at open() time."""
        return "vaapi" in _ffmpeg_lists("hwaccels")

    def _decoder_for_codec(self, codec: str) -> str | None:
        return _VAAPI_CODECS.get(codec)

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
        rtsp_transport = options.get("rtsp_transport", "tcp")
        socket_timeout_us = options.get("timeout") or options.get("stimeout") or "5000000"
        device = (
            options.get("vaapi_device")
            or os.environ.get("BABA_VAAPI_DEVICE", "")
            or _DEFAULT_VAAPI_DEVICE
        ).strip()

        vf_chain = []
        if target_fps > 0:
            vf_chain.append(f"fps={target_fps}")
        # scale_vaapi resizes on the media engine and downconverts a possible
        # 10-bit HEVC (p010) surface to 8-bit NV12 in one step.
        vf_chain.append(f"scale_vaapi={out_w}:{out_h}:format=nv12")
        vf_chain.append("hwdownload")
        vf_chain.append("format=nv12")

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+nobuffer",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1000000",
            "-filter_threads",
            "2",
            "-rtsp_transport",
            rtsp_transport,
            "-timeout",
            socket_timeout_us,
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            device,
            "-hwaccel_output_format",
            "vaapi",
            "-c:v",
            hw_decoder,
            "-i",
            url,
            "-an",
            "-vf",
            ",".join(vf_chain),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "nv12",
            "pipe:1",
        ]
