# syntax=docker/dockerfile:1
# State-evaluator image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# Adds ffmpeg + the service with its own dependencies on top of the base
# venv's onnxruntime and baba-core. Mirrors services/embedder/Dockerfile —
# same DINOv2 ONNX path, same shared /dev/shm frame-ring volume and GPU access
# in compose.
#
# ffmpeg is here for prototype capture from a RECORDED moment (capture `at`):
# it seeks the segment and re-scales to the ingestor's ring geometry so the
# crop lands in the same embedding space as a live capture. Live capture reads
# the SHM ring and needs no ffmpeg.
#
# Build order: bases first (`docker compose --profile bases build`), then this.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/state-evaluator/ ./services/state-evaluator/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-state-evaluator

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_state_evaluator"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/state-evaluator/ ./services/state-evaluator/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-state-evaluator

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_state_evaluator"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/state-evaluator/ ./services/state-evaluator/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-state-evaluator

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_state_evaluator"]
