"""The inference backends and video decoders installed, found by entry point.

    [project.entry-points."baba.backends"]
    onnxruntime = "baba_backend_onnxruntime:ONNXRuntimeBackend"

    [project.entry-points."baba.decoders"]
    software = "baba_decoder_software:SoftwareDecoder"

Two groups, so a service pulls in only what it needs.
"""

from __future__ import annotations

from home_core.plugins import Plugins

from baba_core.backend import InferenceBackend
from baba_core.video import VideoDecoder

BACKENDS: Plugins[InferenceBackend] = Plugins(
    "baba.backends", "backend", lambda cls: issubclass(cls, InferenceBackend)
)
DECODERS: Plugins[VideoDecoder] = Plugins(
    "baba.decoders", "decoder", lambda cls: issubclass(cls, VideoDecoder)
)
