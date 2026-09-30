# syntax=docker/dockerfile:1
# Shared base image for ALL Intel-variant BABA services except `ingestor`.
# Mirrors docker/base-nvidia.Dockerfile but ships OpenVINO instead of
# CUDA/TRT, so inference services run on Arc / iGPU / NPU via the
# OpenVINOExecutionProvider in onnxruntime.
#
# Build via `docker compose --profile bases build base-intel` before
# building the services that depend on it.

FROM openvino/ubuntu24_runtime:2026.4.0 AS base

USER root
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH="/opt/venv/bin:$PATH" \
    # onnxruntime (1.30 on Linux) ships Microsoft's 1DS telemetry client and
    # uploads session/device events unless told not to.
    ORT_DISABLE_TELEMETRY=1 \
    DEBIAN_FRONTEND=noninteractive

# openvino/ubuntu24_runtime ships Python 3.12; we standardise on 3.14 for
# every BABA image. Ubuntu 24.04 (noble) has no python3.14 in its default
# repos, so we let uv manage a standalone 3.14 build below (same approach as
# the ingestor nvidia stage) instead of apt — no deadsnakes PPA needed.
# ca-certificates for TLS (uv/pip), tini as PID 1, libgl1 + libglib2.0-0 for
# opencv. The Intel GPU compute runtime the OpenVINO GPU plugin needs — Level
# Zero loader (libze1) + intel-level-zero-gpu driver + intel-opencl-icd — is
# ALREADY shipped by the openvino/ubuntu24_runtime base as of the 2026.2 line
# (2025.0 did NOT, which is why the old build apt-installed libze from
# universe). We must NOT re-install it here: the universe libze-intel-gpu1
# file-conflicts with the base's intel-level-zero-gpu (both own
# libze_intel_gpu.so.1) and dpkg aborts. The pip GPU plugin dlopens the base's
# runtime as-is.
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/onnxruntime/ ./backends/onnxruntime/
COPY backends/openvino/ ./backends/openvino/

# The base-intel dependency group, pinned by uv.lock: baba-core +
# onnxruntime (CPU wheel; OpenVINO EP loaded from the openvino package via
# the OpenVINO backend).
# The openvino/ubuntu24_runtime base ships its OWN /opt/venv (Python 3.12 +
# OpenVINO). We need 3.14 (baba packages are requires-python >=3.14), so
# --clear replaces it with a fresh 3.14 env; openvino is reinstalled from PyPI
# at the uv.lock version, which this base tag must match (see backends/openvino
# pin: 2026.2+ fixes the fp16 GPU-plugin layout bug that forced RT-DETR to f32
# on 2025.0). The system-level GPU
# runtime (Level Zero / OpenCL ICD) the base image provides is untouched — the
# pip GPU plugin dlopens it, so the base tag and the wheel stay version-aligned.
RUN --mount=type=cache,target=/root/.cache/uv uv python install --no-bin 3.14 \
 && uv venv /opt/venv --python 3.14 --clear \
 && uv sync --frozen --no-editable --only-group base-intel

ENTRYPOINT ["/usr/bin/tini", "--"]
