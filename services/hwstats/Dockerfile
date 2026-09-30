# syntax=docker/dockerfile:1
# Hardware stats collector — one image per HW variant (like the other
# base-derived services). The collector code is identical across variants; only
# the GPU tool differs and is probed at runtime:
#   cpu    — CPU/RAM only.
#   nvidia — nvidia-smi (injected by the nvidia container runtime).
#   intel  — intel_gpu_top (from intel-gpu-tools, installed here).

FROM baba/base:cpu AS cpu
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/hwstats/ ./services/hwstats/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-hwstats
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_hwstats"]


FROM baba/base:nvidia AS nvidia
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/hwstats/ ./services/hwstats/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-hwstats
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_hwstats"]


FROM baba/base:intel AS intel
# intel_gpu_top reads the i915 PMU — that needs CAP_PERFMON at runtime. The
# compose intel override cap_adds PERFMON, but that only lands in the BOUNDING
# set: the service runs as uid 1000 and a non-root exec does NOT inherit it
# into the effective set, so the PMU open fails EACCES and intel_gpu_top
# aborts (igt asserts in get_num_gts). The FILE capability below re-raises
# PERFMON into the effective set on exec for any uid — root-in-container was
# how this worked before the non-root hardening, and this is the non-root
# equivalent.
RUN apt-get update && apt-get install -y --no-install-recommends \
    intel-gpu-tools libcap2-bin \
 && rm -rf /var/lib/apt/lists/* \
 && setcap cap_perfmon+ep /usr/bin/intel_gpu_top
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/hwstats/ ./services/hwstats/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable --inexact --package baba-hwstats
# Defense-in-depth: default the image to non-root even under a bare
# `docker run` (compose also pins user:). All build steps above ran as root.
USER 1000
CMD ["python", "-m", "baba_hwstats"]
