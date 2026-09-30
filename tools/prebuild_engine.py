"""Offline TRT engine pre-build.

Builds (and caches under /models/cache) the native engine for a given ONNX
model WITHOUT running the full detector — so a bigger model's multi-minute
TensorRT compile happens off to the side while the live detector keeps serving
its current engine. Once this finishes, swapping BABA_DETECTOR_MODEL makes the
detector find a ready engine and load it in seconds (no live build, no NVR
blind window).

Usage (inside the detector image, /models mounted, GPU visible):
    python tools/prebuild_engine.py /models/<model>.onnx
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from baba_core import BACKENDS, BackendConfig, Precision, StoragePaths


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: prebuild_engine.py /models/<model>.onnx", file=sys.stderr)
        return 2
    model_path = Path(sys.argv[1])

    paths = StoragePaths.from_env()
    paths.model_cache.mkdir(parents=True, exist_ok=True)

    registry = BACKENDS
    backend_cls = registry.select(["tensorrt"])
    backend = backend_cls()

    t0 = time.monotonic()
    print(f"[prebuild] building engine for {model_path} (fp16, b8) ...", flush=True)
    backend.load(
        model_path,
        BackendConfig(
            device_id=0,
            precision=Precision.FP16,
            max_batch_size=8,
            cache_dir=paths.model_cache,
        ),
    )
    backend.warmup(batch_size=8)
    print(f"[prebuild] done in {time.monotonic() - t0:.0f}s — engine cached under {paths.model_cache}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
