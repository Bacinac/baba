# syntax=docker/dockerfile:1
# Shared base image for CPU-only BABA installs (no GPU/NPU). Used by ALL
# services except `ingestor` (which has its own software-decoder base).
#
# This is the "minimal hardware" install path — for Mini-PCs, NAS units,
# or any host without an NVIDIA / Intel discrete accelerator. Inference
# models still run, just on CPU through onnxruntime CPU EP.
#
# Build via `docker compose --profile bases build base-cpu` before
# building the services that depend on it.

FROM python:3.14-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH="/opt/venv/bin:$PATH" \
    # onnxruntime (1.30 on Linux) ships Microsoft's 1DS telemetry client and
    # uploads session/device events unless told not to.
    ORT_DISABLE_TELEMETRY=1

COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/onnxruntime/ ./backends/onnxruntime/

RUN --mount=type=cache,target=/root/.cache/uv uv venv /opt/venv \
 && uv sync --frozen --no-editable --only-group base-cpu

ENTRYPOINT ["/usr/bin/tini", "--"]
