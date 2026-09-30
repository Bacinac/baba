# syntax=docker/dockerfile:1
# API image: thin layer on top of baba/base:{cpu,nvidia,intel}. Adds
# the FastAPI/HTTP stack, auth deps, ffmpeg (for any future media-path
# work + currently used by health probes), opencv-headless + pillow
# (reference-photo decode), and the api source + db migrations.
#
# Reference-photo enrollment uses the same DINOv2 + face stack that
# embedder uses — driven by baba_core.embed + baba_core.face — so the
# variant choice (cpu/nvidia/intel) here matters: api on the nvidia
# base will run DINOv2 inference on GPU for enrollment, matching live
# pipeline embeddings exactly.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/api/ ./services/api/
COPY db/ ./db/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-api

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_api"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/api/ ./services/api/
COPY db/ ./db/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-api

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_api"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

# ffmpeg + the Intel media runtime. The api cuts Activity clips, and a camera
# that streams HEVC (west) cannot be cut losslessly — it must be transcoded to
# H.264. Without the iHD driver libva fails to initialise ("unknown libva
# error"), the VAAPI attempt is skipped and the cut falls back to libx264:
# measured 11.3 s for a 33 s clip versus 0.36 s for an H.264 camera, which
# reads as a player that never loads. Same package set the ingestor uses (see
# its Dockerfile for why non-free + universe/multiverse are needed); the render
# node itself is already mapped in by docker-compose.intel.yml.
RUN sed -i 's/^Components: main$/Components: main universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true \
 && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    intel-media-va-driver-non-free \
    libmfx-gen1.2 \
    libvpl2 \
    libva2 libva-drm2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/api/ ./services/api/
COPY db/ ./db/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-api

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_api"]
