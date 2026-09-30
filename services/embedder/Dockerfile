# syntax=docker/dockerfile:1
# Embedder image: thin layer on top of baba/base:{cpu,nvidia,intel}.
# Adds the embedder with its locked dependencies; onnxruntime comes
# from the base.
#
# Build order: bases first (`docker compose --profile bases build`),
# then this image.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/embedder/ ./services/embedder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-embedder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_embedder"]


# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/embedder/ ./services/embedder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-embedder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_embedder"]


# ============================== INTEL ==============================
FROM baba/base:intel AS intel

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/embedder/ ./services/embedder/

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-embedder

# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_embedder"]
