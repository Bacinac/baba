# syntax=docker/dockerfile:1
# Ingestor image is built per-target via BABA_INGESTOR_VARIANT (cpu / nvidia
# / intel). The `cpu` stage decodes with the PyAV/ffmpeg software decoder
# plugin. Each hardware variant ships only its own decoder plugin
# (baba-decoder-ffmpeg-nvdec / baba-decoder-intel) and the ffmpeg build and
# system libraries it needs to reach the vendor's decode silicon.

# Sources built from release tarballs, pinned by checksum, never by `git clone`
# at build time. GitHub started refusing anonymous git-over-HTTPS from the
# nvidia builder ("could not read Username", 02.09.2026) and the deploy prunes
# the build cache, so every deploy re-fetched FFmpeg and could fail on a whim of
# a remote we do not control. A tarball with a sha256 either builds the same
# bytes or fails loudly; there is no third outcome.
ARG FFMPEG_VERSION=8.1.3
ARG FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
ARG NVCH_VERSION=n13.1.15.0
ARG NVCH_SHA256=2255bc74d038b95aa4be30f5f66322c2176acbdb90ada1851db6993536fbeaf7
ARG LLVM_VERSION=23.1.2
ARG LLVM_SHA256=6382de1c1a210ce5a5cc49d18bc8444d137742e7cbf9b19f4ae602bb1ab52534

# ubuntu:24.04 ships no CA bundle, so its apt cannot reach the HTTPS archive
# that would carry ca-certificates to it. Lend it one; the cpu stage's image
# is already pulled for this build, so this costs nothing.
FROM python:3.14-slim-trixie AS ca-certs

FROM python:3.14-slim-trixie AS cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH="/opt/venv/bin:$PATH"

COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tini \
    ffmpeg \
    libavcodec61 \
    libavformat61 \
    libavutil59 \
    libswscale8 \
    libgl1 \
    libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/decoder-software/ ./backends/decoder-software/
COPY services/ingestor/ ./services/ingestor/

# Decoder backends are plugins discovered via the `baba.decoders` entry
# point group. This stage ships the software (PyAV) backend; each vendor
# stage below ships its own hardware decoder instead.
RUN --mount=type=cache,target=/root/.cache/uv uv venv /opt/venv \
 && uv sync --frozen --no-editable --package baba-ingestor --extra cpu

ENTRYPOINT ["/usr/bin/tini", "--"]
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_ingestor"]


# --- nvidia (NVDEC) ---------------------------------------------------------
# Compile ffmpeg with CUDA NVDEC support; decoder-ffmpeg-nvdec runs it
# as a subprocess. Distro ffmpeg builds carry no CUDA.

# LLVM 23 supports CUDA through 13.2; the runtime stays on CUDA 13.4.
FROM nvidia/cuda:13.2.1-devel-ubuntu24.04 AS nvidia-ffmpeg-builder
ARG LLVM_VERSION
ARG LLVM_SHA256
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
ARG NVCH_VERSION
ARG NVCH_SHA256

ENV DEBIAN_FRONTEND=noninteractive
# clang: compiles ffmpeg's CUDA kernels (scale_cuda et al) via
# --enable-cuda-llvm — the free replacement for the nonfree nvcc path
# (--enable-cuda-nvcc). Keeps the resulting libav* redistributable,
# which a prebuilt public image requires.
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ccache \
    curl \
    xz-utils \
    zstd \
    pkg-config \
    yasm \
    nasm \
    libtool \
    autoconf \
    automake \
    ca-certificates \
    libedit2 \
    libxml2 \
    libzstd1 \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/llvm.tar.zst \
        "https://github.com/llvm/llvm-project/releases/download/llvmorg-${LLVM_VERSION}/LLVM-${LLVM_VERSION}-Linux-X64.tar.zst" \
 && echo "${LLVM_SHA256}  /tmp/llvm.tar.zst" | sha256sum -c - \
 && mkdir /opt/llvm \
 && tar --use-compress-program="zstd --long=30" -xf /tmp/llvm.tar.zst -C /opt/llvm --strip-components=1 \
 && rm /tmp/llvm.tar.zst
ENV PATH="/opt/llvm/bin:$PATH"

# Headers that wire ffmpeg's avcodec into NVIDIA's NVDEC/NVENC ABI. Pin the
# tag so an upstream rename doesn't break our build silently.
RUN curl -fsSL -o /tmp/nvch.tar.gz \
        "https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/${NVCH_VERSION}.tar.gz" \
 && echo "${NVCH_SHA256}  /tmp/nvch.tar.gz" | sha256sum -c - \
 && mkdir /tmp/nvch \
 && tar -xzf /tmp/nvch.tar.gz -C /tmp/nvch --strip-components=1 \
 && make -C /tmp/nvch install \
 && rm -rf /tmp/nvch /tmp/nvch.tar.gz

# Minimal ffmpeg build: enough to demux RTSP + decode H.264/H.265/AV1 via
# NVDEC and hand NV12 frames to Python over a pipe. Docs/manpages are
# disabled; only the ffmpeg binary and its shared libav*.so ship.
# Licence surface stays LGPL + free: no --enable-gpl (nothing we use needs
# it — same reasoning as the intel build below), and the CUDA kernels
# (scale_cuda) compile via clang (--enable-cuda-llvm) instead of the nonfree
# nvcc path, so the resulting binaries are redistributable in a prebuilt
# public image.
# ccache (cached across builds via the mount) wraps gcc so a rebuild of the
# pinned source recompiles the libav* C files from cache instead of cold —
# the CUDA objects aren't cached, but the gcc bulk is, so a repeat build
# drops from ~10 min to ~1-2 min.
RUN --mount=type=cache,target=/root/.cache/ccache \
    curl -fsSL -o /tmp/ffmpeg.tar.xz \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
 && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
 && mkdir /tmp/ffmpeg \
 && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
 && rm /tmp/ffmpeg.tar.xz \
 && cd /tmp/ffmpeg \
 && ./configure \
        --prefix=/usr/local \
        --cc="ccache gcc" \
        --cxx="ccache g++" \
        --enable-shared \
        --disable-static \
        --disable-everything \
        --enable-network \
        --enable-protocol=file,tcp,udp,rtp,pipe \
        --enable-demuxer=rtsp,rtp,sdp \
        --enable-parser=h264,hevc,av1,vp8,vp9,mpegvideo,mpeg4video \
        --enable-decoder=h264,hevc,av1,vp8,vp9,mpeg2video,mpeg4,h264_cuvid,hevc_cuvid,av1_cuvid,vp8_cuvid,vp9_cuvid,mpeg2_cuvid,mpeg4_cuvid \
        --enable-hwaccel=h264_nvdec,hevc_nvdec,av1_nvdec,vp8_nvdec,vp9_nvdec,mpeg2_nvdec,mpeg4_nvdec \
        --enable-filter=fps,scale_cuda,hwdownload,format \
        --enable-encoder=rawvideo \
        --enable-muxer=rawvideo \
        --enable-bsf=h264_mp4toannexb,hevc_mp4toannexb,extract_extradata \
        --enable-cuda-llvm \
        --enable-cuvid \
        --enable-nvdec \
        --extra-cflags=-I/usr/local/cuda/include \
        --extra-ldflags=-L/usr/local/cuda/lib64 \
        --disable-doc \
        --disable-htmlpages \
        --disable-manpages \
        --disable-podpages \
        --disable-txtpages \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /tmp/ffmpeg


FROM nvidia/cuda:13.4.1-runtime-ubuntu24.04 AS nvidia

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH"

COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tini \
 && rm -rf /var/lib/apt/lists/*

# Custom CUDA-enabled ffmpeg from the builder stage.
COPY --from=nvidia-ffmpeg-builder /usr/local /usr/local/
RUN ldconfig

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/decoder-ffmpeg-nvdec/ ./backends/decoder-ffmpeg-nvdec/
COPY services/ingestor/ ./services/ingestor/

# decoder-ffmpeg-nvdec spawns the CUDA ffmpeg (above) as a subprocess — no
# Python binding to it, so nothing here compiles.
RUN --mount=type=cache,target=/root/.cache/uv uv python install --no-bin 3.14 \
 && uv venv /opt/venv --python 3.14 \
 && uv sync --frozen --no-editable --package baba-ingestor --extra nvidia

ENTRYPOINT ["/usr/bin/tini", "--"]
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_ingestor"]


# --- intel (QSV + VAAPI) ----------------------------------------------------
# Compile ffmpeg with Intel Quick Sync (oneVPL/libvpl) AND VAAPI, then install
# baba-decoder-intel (both hw paths) on top of the Intel media runtime. Stock
# distro ffmpeg can't be relied on to carry libvpl, and Arc (DG2/Xe) needs the
# modern oneVPL runtime — the legacy MediaSDK/libmfx only covers Gen ≤11 — so
# we build from source like the nvidia stage does.
#
# Two hardware decoders ship, both writing NV12: `vaapi-nv12` (the intel
# default; clean on the Arc) and `qsv-nv12` (for hosts whose VAAPI path is
# broken, e.g. the N305 iGPU). The ingestor binds exactly the one
# BABA_DECODER_BACKEND names; is_available() says whether this ffmpeg built it.

FROM ubuntu:24.04 AS intel-ffmpeg-builder
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256

ENV DEBIAN_FRONTEND=noninteractive
COPY --from=ca-certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
    build-essential ccache curl xz-utils pkg-config yasm nasm libtool autoconf automake \
    ca-certificates \
    libva-dev \
    libvpl-dev \
 && rm -rf /var/lib/apt/lists/*

# Minimal ffmpeg: demux RTSP + decode H.264/H.265/VP9 on the Intel media engine
# (QSV via libvpl, plus VAAPI) and hand RGB/NV12 frames to Python. --enable-gpl
# is not required for the hw decode/scale filters we use; keep the licence
# surface minimal for a commercial product.
# ccache (cached across builds via the mount) wraps gcc; with no nvcc here the
# whole compile is cached, so a repeat build drops from ~10 min to ~1 min.
RUN --mount=type=cache,target=/root/.cache/ccache \
    curl -fsSL -o /tmp/ffmpeg.tar.xz \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
 && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
 && mkdir /tmp/ffmpeg \
 && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
 && rm /tmp/ffmpeg.tar.xz \
 && cd /tmp/ffmpeg \
 && ./configure \
        --prefix=/usr/local \
        --cc="ccache gcc" \
        --cxx="ccache g++" \
        --enable-shared \
        --disable-static \
        --enable-vaapi \
        --enable-libvpl \
        --disable-doc \
        --disable-htmlpages \
        --disable-manpages \
        --disable-podpages \
        --disable-txtpages \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /tmp/ffmpeg


FROM ubuntu:24.04 AS intel

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH" \
    # Force libva to load the iHD driver (Arc / recent iGPU). Without this
    # libva may pick the legacy i965 and fail on DG2.
    LIBVA_DRIVER_NAME=iHD

# Intel media runtime:
#   intel-media-va-driver-non-free — iHD VAAPI driver (matches the host's iHD
#     24.x; the non-free build carries the full HEVC/VP9 profile set).
#   libmfx-gen1.2 — oneVPL GPU runtime for Gen12/Xe (Arc) that libvpl loads.
#   libvpl2 — oneVPL dispatcher. libva2/libva-drm2 — VAAPI runtime.
# NOTE: intel-media-va-driver-non-free lives in the `multiverse` component and
# libmfx-gen1.2 in `universe`; enable both before install. Package names are
# noble-specific — confirm on first build (docker compose build ingestor).
COPY --from=ca-certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources \
 && { sed -i 's/^Components: main$/Components: main universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; } \
 && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini \
    intel-media-va-driver-non-free \
    libmfx-gen1.2 \
    libvpl2 \
    libva2 libva-drm2 \
 && rm -rf /var/lib/apt/lists/*
# Python 3.14 isn't in noble's default repos; uv manages a standalone build
# (same as the nvidia stage) — see the uv python install below.

# Custom VAAPI+libvpl ffmpeg from the builder stage.
COPY --from=intel-ffmpeg-builder /usr/local /usr/local/
RUN ldconfig

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY backends/decoder-intel/ ./backends/decoder-intel/
COPY services/ingestor/ ./services/ingestor/

# decoder-intel spawns the system ffmpeg (above) as a subprocess — no Python
# binding to it, so nothing here compiles.
RUN --mount=type=cache,target=/root/.cache/uv uv python install --no-bin 3.14 \
 && uv venv /opt/venv --python 3.14 \
 && uv sync --frozen --no-editable --package baba-ingestor --extra intel

ENTRYPOINT ["/usr/bin/tini", "--"]
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_ingestor"]
