# syntax=docker/dockerfile:1
# Doorbell image: thin layer on baba/base:{cpu,nvidia,intel}.
#
# Subscribes to the Reolink doorbell's Baichuan push channel (TCP :9000) and
# inserts a `doorbell_press` event when the physical button is pressed. That
# row's NOTIFY is mirrored to NATS by the api events-bridge, so DIDA reacts
# with no extra wiring. No ffmpeg, no GPU, no go2rtc — it speaks to the
# camera's control API directly over the LAN.

# ============================== CPU ==============================
FROM baba/base:cpu AS cpu
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/doorbell/ ./services/doorbell/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-doorbell
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_doorbell"]

# ============================== NVIDIA ==============================
FROM baba/base:nvidia AS nvidia
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/doorbell/ ./services/doorbell/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-doorbell
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_doorbell"]

# ============================== INTEL ==============================
FROM baba/base:intel AS intel
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/doorbell/ ./services/doorbell/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-doorbell
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_doorbell"]
