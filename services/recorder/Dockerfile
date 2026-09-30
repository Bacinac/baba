# syntax=docker/dockerfile:1
# Recorder image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# Adds ffmpeg (the workhorse — pulls RTSP, mux-cuts segments, writes
# MP4) and the recorder with its locked dependencies.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/recorder/ ./services/recorder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-recorder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_recorder"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/recorder/ ./services/recorder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-recorder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_recorder"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/recorder/ ./services/recorder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-recorder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_recorder"]
