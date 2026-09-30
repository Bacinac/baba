"""NVIDIA NVDEC video decoder via an ffmpeg subprocess.

libav runs inside a separate ffmpeg process and the ingestor only reads
fixed-size NV12 blobs from a pipe. That keeps the per-RTP-packet demux off the
Python ↔ C boundary — which a PyAV demuxer crossed for every packet, measured
at ~85% of active CPU on a 7-camera load with go2rtc upstream.

Pipeline (all stages in the ffmpeg process):

  RTSP/TCP  →  libavformat demux  →  hevc_cuvid/h264_cuvid (NVDEC silicon)
              →  scale_cuda (GPU resize)
              →  hwdownload + format=nv12 (GPU→host, no colour conversion)
              →  rawvideo on stdout

fps filtering and downscaling happen inside the ffmpeg pipeline; the caller
doesn't need to re-do that work. YUV→RGB happens in the detector's nv12-baked
graph on the GPU. All the vendor-neutral machinery (ffprobe, subprocess
lifecycle, frame reads) lives in baba_core.ffmpeg_decoder — this module only
supplies the CUDA-specific argv and codec map.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from baba_core.ffmpeg_decoder import FFmpegSubprocessDecoder
from baba_core.video import DecoderCapabilities

log = logging.getLogger(__name__)


# Source codec name (as reported by ffprobe) → cuvid decoder.
_CUVID_DECODERS = {
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "av1": "av1_cuvid",
    "vp9": "vp9_cuvid",
    "vp8": "vp8_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "mpeg4": "mpeg4_cuvid",
}


class FFmpegNvdecNV12Decoder(FFmpegSubprocessDecoder):
    capabilities = DecoderCapabilities(
        name="ffmpeg-nvdec-nv12",
        hardware_accelerated=True,
        output_pixel_format="nv12",
    )

    @classmethod
    def is_available(cls) -> bool:
        """Available iff ffmpeg is on PATH and was built with cuvid. The
        runtime CUDA driver only matters at open() time — if it's missing,
        the spawned ffmpeg will print an error and exit, which the worker's
        outer reconnect loop handles."""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return False
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-decoders"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and "h264_cuvid" in result.stdout

    def _decoder_for_codec(self, codec: str) -> str | None:
        return _CUVID_DECODERS.get(codec)

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

        # fps filter runs in CUDA frame context — it only drops/keeps based
        # on PTS, no pixel work, so doing it BEFORE scale_cuda means we skip
        # the GPU resize for frames we'd throw away. Order in -vf matters:
        # fps → scale_cuda → hwdownload → format=nv12.
        vf_chain = []
        if target_fps > 0:
            vf_chain.append(f"fps={target_fps}")
        vf_chain.append(f"scale_cuda={out_w}:{out_h}")
        vf_chain.append("hwdownload")
        vf_chain.append("format=nv12")

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            # nobuffer + low probe size keeps RTSP DESCRIBE→first frame
            # latency at <1s instead of the default ~5s.
            "-fflags",
            "+nobuffer",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1000000",
            # Two filter threads per camera: several ffmpeg subprocesses
            # already contend for the CPU, so more would just add context
            # switching.
            "-filter_threads",
            "2",
            "-rtsp_transport",
            rtsp_transport,
            "-timeout",
            socket_timeout_us,
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            hw_decoder,
            "-i",
            url,
            # -an drops audio at demuxer level — avoids depacketising AAC only
            # to discard it later. Cameras typically send audio over RTSP
            # whether we asked for it or not.
            "-an",
            "-vf",
            ",".join(vf_chain),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "nv12",
            "pipe:1",
        ]

