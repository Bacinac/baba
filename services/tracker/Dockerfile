# syntax=docker/dockerfile:1
# Tracker image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# Tracker runs Norfair (BSD-3-Clause) for IoU+Kalman association plus
# inline OSNet x0_25 (MIT, ~1 MB) for short-horizon appearance-based
# ReID. OSNet rides whichever ONNX provider the base image carries —
# CPU on `cpu`, CUDA on `nvidia`, OpenVINO on `intel` — without any
# vendor-specific code in the service.
#
# libxcb1 is required by opencv-python transitive deps even in
# headless containers.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/tracker/ ./services/tracker/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-tracker

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_tracker"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/tracker/ ./services/tracker/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-tracker

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_tracker"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/tracker/ ./services/tracker/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-tracker

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_tracker"]
