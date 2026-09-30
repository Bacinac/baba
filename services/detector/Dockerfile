# syntax=docker/dockerfile:1
# Detector image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# The base already carries Python, onnxruntime, baba-core, and the
# baba-backend-onnxruntime plugin — this Dockerfile only adds detector-
# specific Python deps (opencv-headless), the TensorRT backend plugin
# on the NVIDIA stage (engine compilation), and the detector source.
#
# Build order: bases first (`docker compose --profile bases build`),
# then this image. See docker/base-*.Dockerfile.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/detector/ ./services/detector/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-detector

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_detector"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

WORKDIR /app
COPY backends/tensorrt/ ./backends/tensorrt/
COPY pyproject.toml uv.lock ./
COPY services/detector/ ./services/detector/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-detector --extra nvidia

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_detector"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/detector/ ./services/detector/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-detector

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_detector"]
