# syntax=docker/dockerfile:1
# Shared base image for ALL NVIDIA-variant BABA services except `ingestor`
# (which builds its own CUDA-enabled FFmpeg on the matching CUDA devel image
# and is therefore a separate base chain).
#
# Carries:
#   nvidia/cuda:13.4.1-cudnn-runtime-ubuntu24.04 — CUDA 13 runtime libraries +
#     cuDNN 9, the system libraries onnxruntime-gpu's CUDA provider loads.
#     TensorRT is NOT taken from the image: it comes from uv.lock (tensorrt-cu13
#     wheels, pulled in by the detector's nvidia extra), so its version is pinned
#     in one place.
#   uv-managed Python 3.14 (noble ships 3.12)
#   apt: tini, libgl1, libglib2.0-0 (numpy/cv2 bits used by inference services)
#   uv binary
#   /opt/venv: baba-core, baba-backend-onnxruntime[gpu], common Python libs
#
# Service Dockerfiles `FROM baba/base:nvidia` and only add their unique
# deps + service code; layer dedup means each additional service adds only
# MB (not GB) of disk.
#
# Build via `docker compose --profile bases build base-nvidia` before
# building the services that depend on it.

FROM nvidia/cuda:13.4.1-cudnn-runtime-ubuntu24.04 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    # Install the uv-managed Python under /opt (world-readable) instead of the
    # default ~/.local/share/uv (root's home, 0700) — otherwise /opt/venv/bin/
    # python symlinks into a root-only tree and non-root containers can't exec
    # it ("Permission denied"). Required for the user: 1000 runtime.
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH="/opt/venv/bin:$PATH" \
    # onnxruntime (1.30 on Linux) ships Microsoft's 1DS telemetry client and
    # uploads session/device events unless told not to.
    ORT_DISABLE_TELEMETRY=1 \
    DEBIAN_FRONTEND=noninteractive

COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/onnxruntime/ ./backends/onnxruntime/

# The base-nvidia dependency group (baba-core + onnxruntime-gpu), pinned by
# uv.lock. Each service syncs its own locked dependencies on top.
RUN --mount=type=cache,target=/root/.cache/uv uv python install --no-bin 3.14 \
 && uv venv /opt/venv --python 3.14 \
 && uv sync --frozen --no-editable --only-group base-nvidia

ENTRYPOINT ["/usr/bin/tini", "--"]
