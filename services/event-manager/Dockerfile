# syntax=docker/dockerfile:1
# Event-manager image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# Adds ffmpeg (for any future video-side enrichment) and the
# event-manager with its locked dependencies.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/event-manager/ ./services/event-manager/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-event-manager

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_event_manager"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/event-manager/ ./services/event-manager/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-event-manager

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_event_manager"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/event-manager/ ./services/event-manager/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-event-manager

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_event_manager"]
