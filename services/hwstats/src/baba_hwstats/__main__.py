"""Hardware stats collector → published to NATS `baba.stats.hardware`.

Hardware-agnostic: probes for the GPU tool at startup (nvidia-smi → NVIDIA,
intel_gpu_top → Intel Arc/iGPU) and publishes whatever gauges apply. On a CPU
host it publishes just CPU + RAM. Everything is best-effort — a failed GPU read
never stops the CPU/RAM publish, and the collector never crashes the way a
monitoring agent shouldn't.

Fits the existing baba.stats.* architecture: one StatsCollector(service=
"hardware") whose gauges the API aggregator picks up and the System page renders
(a dedicated Hardware section, since it reads host resources not a pipeline
stage). Gauges use vendor-neutral names where they map (gpu_util_pct,
gpu_mem_used_mb, gpu_freq_mhz) plus a couple vendor extras (Intel's video-engine
busy for the VAAPI/QSV decode load).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import msgspec
from baba_core import StatsCollector, drain_quietly, setup_logging
from baba_core.nats_conn import connect as nats_connect
from baba_core.runtime import run_service
from baba_core.wire import SUBJECT_TELEMETRY_TEMPLATE
from home_core.health import HealthMarker

log = setup_logging("hwstats")

INTERVAL = float(os.environ.get("BABA_HWSTATS_INTERVAL", "5"))


# --------------------------------------------------------------------------
# CPU + RAM (always available — read straight from /proc, no dependency)
# --------------------------------------------------------------------------


# cgroup path whose CPU/RAM counters we report. Set BABA_HWSTATS_CGROUP to a
# parent slice to isolate BABA from co-tenant containers + the host: with
# cgroupns=host the container sees the full host tree, and every BABA service
# is placed under `baba.slice` (compose cgroup_parent), so
# /sys/fs/cgroup/baba.slice aggregates ONLY BABA's containers. Default is the
# root: on an LXC that's the LXC's own cgroup (already BABA-scoped); on a
# bare-metal / VM root it doesn't expose per-cgroup memory.current/max (only
# cpu.stat et al.), so the RAM collector falls back to host-wide /proc/meminfo.
_CG = os.environ.get("BABA_HWSTATS_CGROUP", "/sys/fs/cgroup").rstrip("/") or "/sys/fs/cgroup"


def _host_mem_total_bytes() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) * 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _proc_meminfo_used_total_bytes() -> tuple[float, float]:
    """(used, total) host RAM in bytes from /proc/meminfo (used = total −
    available). Fallback for the cgroup-v2 ROOT, where memory.current/max don't
    exist — that's the layout on a bare-metal / VM host with cgroupns=host,
    unlike an LXC whose container sees a child cgroup carrying those files."""
    total = avail = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = float(line.split()[1]) * 1024.0
                elif line.startswith("MemAvailable:"):
                    avail = float(line.split()[1]) * 1024.0
    except (OSError, ValueError, IndexError):
        pass
    used = max(0.0, total - avail) if total else 0.0
    return used, total


def _cgroup_cores() -> float:
    """CPU allocation from cpu.max (quota/period); 0/'max' → the cores actually
    schedulable for this process. Use sched_getaffinity (respects the LXC's
    cpuset pin) not os.cpu_count() (which returns the physical host's core
    count) — otherwise cpu_pct is divided by too many cores and reads low."""
    try:
        with open(f"{_CG}/cpu.max") as f:
            quota, period = f.read().split()
        if quota != "max":
            return max(1.0, int(quota) / int(period))
    except (OSError, ValueError):
        pass
    try:
        return float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return float(os.cpu_count() or 1)


def _cgroup_cpu_usec() -> int | None:
    try:
        with open(f"{_CG}/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def collect_cpu_ram(stats: StatsCollector, prev: dict[str, float]) -> None:
    import time

    # --- RAM ---
    # Primary: cgroup v2 (memory.current/max) — scopes usage to the WHOLE BABA
    # deployment on an LXC child cgroup. Fallback: /proc/meminfo host-wide, for
    # a cgroup-v2 ROOT (bare-metal / VM host, cgroupns=host) where those files
    # don't exist. Without the fallback the RAM gauges silently vanish there.
    try:
        env_limit = os.environ.get("BABA_HWSTATS_MEM_LIMIT_MB", "").strip()
        used: float | None = None
        total: float | None = None
        try:
            with open(f"{_CG}/memory.current") as f:
                used = float(f.read().strip())
            with open(f"{_CG}/memory.max") as f:
                limit_raw = f.read().strip()
            # Effective limit, best source first:
            #   1) explicit operator/installer value (the LXC's allocated RAM),
            #   2) a real cgroup limit if one is set,
            #   3) the physical host RAM as the ultimate ceiling (unlimited LXC).
            if env_limit:
                total = float(env_limit) * 1048576.0
            elif limit_raw.isdigit():
                total = float(limit_raw)
            else:
                total = _host_mem_total_bytes()
        except FileNotFoundError:
            # cgroup-v2 root: no per-cgroup memory files → report host-wide.
            used, host_total = _proc_meminfo_used_total_bytes()
            total = float(env_limit) * 1048576.0 if env_limit else host_total
        if used is not None:
            stats.set_gauge("ram_used_mb", used / 1048576.0)
            if total and total > 0:
                stats.set_gauge("ram_total_mb", total / 1048576.0)
                stats.set_gauge("ram_pct", 100.0 * used / total)
    except (OSError, ValueError) as e:
        log.debug("memory read failed: %s", e)

    # --- CPU % (cgroup cpu.stat delta over the allocated cores) ---
    usec = _cgroup_cpu_usec()
    now = time.monotonic()
    cores = _cgroup_cores()
    stats.set_gauge("cpu_cores", cores)
    if usec is not None and prev.get("cpu_usec") is not None:
        d_usec = usec - prev["cpu_usec"]
        d_t = now - prev["cpu_t"]
        if d_t > 0 and cores > 0:
            pct = 100.0 * (d_usec / 1e6) / (d_t * cores)
            stats.set_gauge("cpu_pct", max(0.0, min(100.0, pct)))
    if usec is not None:
        prev["cpu_usec"] = usec
        prev["cpu_t"] = now

    # Load average — host-wide (can't be scoped to a cgroup); a secondary
    # signal only, the cgroup cpu_pct above is the BABA-specific number.
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        stats.set_gauge("cpu_load_1m", float(parts[0]))
        stats.set_gauge("cpu_load_5m", float(parts[1]))
        stats.set_gauge("cpu_load_15m", float(parts[2]))
    except (OSError, ValueError, IndexError) as e:
        log.debug("loadavg read failed: %s", e)


# --------------------------------------------------------------------------
# GPU — NVIDIA (nvidia-smi)
# --------------------------------------------------------------------------


# What we ask nvidia-smi for, in order. Everything here is one process spawn,
# so the cost of a field is a few bytes of CSV — the reason to leave one out is
# that it means nothing, not that it is expensive.
#
# Measured on the RTX 3060 this runs against (2026-07-29): every field below
# returns a real value. `temperature.memory` is deliberately absent — GA106 has
# no memory-junction sensor and reports [N/A], and a permanent em-dash in the
# panel teaches the operator to ignore that corner of it.
_NVIDIA_FIELDS = (
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "clocks.sm",
    "utilization.encoder",
    "utilization.decoder",
    "power.draw",
    "enforced.power.limit",
    "fan.speed",
    "clocks.mem",
    "clocks.max.sm",
    "utilization.memory",
    "pcie.link.gen.current",
    "pcie.link.width.current",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
    "clocks_throttle_reasons.sw_power_cap",
    "clocks_throttle_reasons.hw_power_brake_slowdown",
)


def _nv_num(fields: list[str], i: int) -> float | None:
    """One CSV cell as a number, or None when the card doesn't report it.

    nvidia-smi writes `[N/A]` (and `[Not Supported]`) for fields the hardware
    has no sensor for, per-field rather than per-query — so a missing value must
    drop that single gauge, never the whole read."""
    try:
        return float(fields[i])
    except (IndexError, ValueError):
        return None


def collect_gpu_nvidia(stats: StatsCollector) -> bool:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=" + ",".join(_NVIDIA_FIELDS),
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return False
        # First GPU only (single-GPU deployments). Fields are comma-separated.
        f = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        stats.set_gauge("gpu_util_pct", float(f[0]))
        stats.set_gauge("gpu_mem_used_mb", float(f[1]))
        stats.set_gauge("gpu_mem_total_mb", float(f[2]))
        stats.set_gauge("gpu_temp_c", float(f[3]))
        stats.set_gauge("gpu_freq_mhz", float(f[4]))
        # Per-"engine" gauges so the UI's detailed view renders the same way it
        # does for Intel's engine list: overall compute + encode + decode. NVML
        # doesn't split 3D/compute/blitter the way i915 PMU does, so compute is
        # the single overall-util figure.
        stats.set_gauge("gpu_eng_compute_pct", float(f[0]))
        with contextlib.suppress(IndexError, ValueError):
            enc, dec = float(f[5]), float(f[6])
            stats.set_gauge("gpu_eng_encode_pct", enc)
            stats.set_gauge("gpu_eng_decode_pct", dec)
            # Legacy single "decode" summary bar — max of enc/dec.
            stats.set_gauge("gpu_video_pct", max(enc, dec))

        # Vendor-neutral names: an Intel or AMD collector that learns to read
        # power or fan speed fills the SAME gauges, and the panel renders them
        # without knowing which card it is looking at.
        for name, idx in (
            ("gpu_power_w", 7),
            ("gpu_power_limit_w", 8),
            ("gpu_fan_pct", 9),
            ("gpu_clock_mem_mhz", 10),
            ("gpu_freq_max_mhz", 11),
            ("gpu_mem_io_pct", 12),
            ("gpu_pcie_gen", 13),
            ("gpu_pcie_width", 14),
        ):
            v = _nv_num(f, idx)
            if v is not None:
                stats.set_gauge(name, v)

        # Throttling, folded to two gauges the operator can act on: heat or
        # power. This is the field that explains a frame rate that quietly
        # halved on a hot afternoon while every other number looked fine, so it
        # is worth more than any of the clocks above.
        def _active(idx: int) -> bool:
            return len(f) > idx and f[idx].strip().lower() == "active"

        stats.set_gauge("gpu_throttle_thermal", float(_active(15) or _active(16)))
        stats.set_gauge("gpu_throttle_power", float(_active(17) or _active(18)))
        return True
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        log.debug("nvidia-smi read failed: %s", e)
        return False


# --------------------------------------------------------------------------
# GPU — Intel (intel_gpu_top, JSON)
# --------------------------------------------------------------------------


def _parse_intel_gpu_top(raw: str) -> dict | None:
    """intel_gpu_top -J streams a never-closed JSON array of per-sample
    objects. Close it into a valid array and take the last complete sample."""
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith("["):
        raw = "[" + raw
    raw = raw.rstrip().rstrip(",")
    if not raw.endswith("]"):
        raw = raw + "]"
    try:
        samples = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back: take the last full {...} object by bracket matching.
        depth = 0
        start = None
        last = None
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    last = raw[start : i + 1]
        if last is None:
            return None
        try:
            return json.loads(last)
        except json.JSONDecodeError:
            return None
    return samples[-1] if samples else None


def collect_gpu_intel(stats: StatsCollector) -> bool:
    try:
        # intel_gpu_top runs forever, so bound it with coreutils `timeout`
        # (SIGTERM after 2s) and capture what it printed. subprocess.run's own
        # timeout would instead raise TimeoutExpired and discard the output.
        # -s 500 = one ~500ms sample; over 2s we get a few, we take the last.
        out = subprocess.run(
            ["timeout", "2", "intel_gpu_top", "-J", "-s", "500", "-o", "-"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        sample = _parse_intel_gpu_top(out.stdout)
        if not sample:
            return False
        engines = sample.get("engines", {})
        # Emit every engine i915's PMU exposes as its own gauge so the UI can
        # show a full per-engine breakdown. WHERE inference lands depends on the
        # GPU: discrete Arc (Xe HPG) runs OpenVINO/oneAPI ML on the dedicated
        # "Compute" engine (CCS) while Render/3D stays idle (measured 94%
        # Compute vs 0% Render/3D under a live RT-DETR batch on the A380);
        # integrated Intel GPUs (Gen9-11) have no Compute engine and share
        # Render/3D. "Video" is VAAPI/QSV decode, "VideoEnhance" the scaler/CSC.
        engine_keys = {
            "Compute": "compute",
            "Video": "video",
            "VideoEnhance": "videoenhance",
            "Render/3D": "render3d",
            "Blitter": "blitter",
        }
        for eng_name, key in engine_keys.items():
            busy = engines.get(eng_name, {}).get("busy")
            if busy is not None:
                stats.set_gauge(f"gpu_eng_{key}_pct", float(busy))
        render = engines.get("Render/3D", {}).get("busy")
        compute = engines.get("Compute", {}).get("busy")
        video = engines.get("Video", {}).get("busy")
        # Summary "utilisation" = whichever compute-capable engine holds the load
        # (Compute on Arc, Render/3D on an iGPU).
        util_vals = [v for v in (render, compute) if v is not None]
        if util_vals:
            stats.set_gauge("gpu_util_pct", float(max(util_vals)))
        if video is not None:
            stats.set_gauge("gpu_video_pct", float(video))
        freq = sample.get("frequency", {})
        if freq.get("actual") is not None:
            stats.set_gauge("gpu_freq_mhz", float(freq["actual"]))
        if freq.get("requested") is not None:
            stats.set_gauge("gpu_freq_req_mhz", float(freq["requested"]))
        # rc6 = fraction of the period the GPU was asleep (idle residency).
        rc6 = sample.get("rc6", {}).get("value")
        if rc6 is not None:
            stats.set_gauge("gpu_rc6_pct", float(rc6))
        # Integrated memory controller bandwidth (Arc: VRAM traffic).
        imc = sample.get("imc-bandwidth", {})
        if imc.get("reads") is not None:
            stats.set_gauge("gpu_membw_read_mibs", float(imc["reads"]))
        if imc.get("writes") is not None:
            stats.set_gauge("gpu_membw_write_mibs", float(imc["writes"]))
        # Discrete Arc VRAM if the build reports it (not all do).
        vram = sample.get("memory") or sample.get("vram")
        if isinstance(vram, dict):
            used = vram.get("used") or vram.get("system-used")
            total = vram.get("total")
            if used is not None:
                stats.set_gauge("gpu_mem_used_mb", float(used))
            if total is not None:
                stats.set_gauge("gpu_mem_total_mb", float(total))
        return True
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as e:
        log.debug("intel_gpu_top read failed: %s", e)
        return False


def pick_gpu_collector():
    """Probe once at startup. NVIDIA first (nvidia-smi injected by the nvidia
    container runtime), then Intel. Returns a collect fn or None (CPU host)."""
    if shutil.which("nvidia-smi"):
        log.info("hwstats: GPU backend = nvidia-smi")
        return collect_gpu_nvidia
    if shutil.which("intel_gpu_top"):
        log.info("hwstats: GPU backend = intel_gpu_top")
        return collect_gpu_intel
    log.info("hwstats: no GPU tool found — publishing CPU/RAM only")
    return None


class _GaugeTap:
    """Forwards gauges to the real collector and keeps the latest value of
    each, so the minute bucket can aggregate them.

    The collectors only ever call `set_gauge`, so this stands in for the
    StatsCollector without touching them. Reading `StatsCollector.snapshot()`
    was the alternative and is wrong here: it rolls the rate baseline
    forward, so an extra call would corrupt the 5 s publish it exists for."""

    def __init__(self, stats: StatsCollector) -> None:
        self._stats = stats
        self.latest: dict[str, float] = {}

    def set_gauge(self, name: str, value: float) -> None:
        self._stats.set_gauge(name, value)
        self.latest[name] = float(value)


# Gauges worth a minute bucket. GPU/CPU saturation is what actually bounds
# this deployment, and `baba.stats.*` is live-only by design (no Prometheus —
# see the 2026-06-04 decision), so peak load was unanswerable after the fact:
# the walk-test on 2026-07-25 could be shown to have run at 22.1 inferences/s
# from the ingestor buckets, while what the GPU did at that moment was simply
# not recorded anywhere. MAX matters more than the mean here — a minute that
# averages 50 % but touches 100 % is the one that drops frames.
_BUCKET_GAUGES = (
    "gpu_util_pct",
    "gpu_eng_compute_pct",
    "gpu_video_pct",
    "gpu_eng_videoenhance_pct",
    "gpu_mem_used_mb",
    "gpu_freq_mhz",
    "cpu_pct",
    "ram_pct",
)
BUCKET_S = float(os.environ.get("BABA_HWSTATS_BUCKET_S", "60"))
# Host-wide metrics have no camera to hang on; camera_telemetry keys rows by
# slug, so they land under a sentinel that cannot collide with a real one
# (slugs are validated identifiers). Same table, same retention sweep, same
# flight-recorder pattern — no second storage path.
HOST_SLUG = "_host"


async def main() -> None:
    nats_url = os.environ.get("BABA_NATS_URL", "nats://nats:4222")
    stats = StatsCollector(service="hardware")
    # Advertise which backend we picked so the UI can label the GPU section.
    variant = os.environ.get("BABA_VARIANT", "cpu")
    gpu_collect = pick_gpu_collector()

    nc = await nats_connect(nats_url, name="hwstats")
    await stats.start(nc, interval=INTERVAL)
    log.info("hwstats up: variant=%s interval=%ss", variant, INTERVAL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    prev: dict[str, float] = {}
    # Same file-marker liveness contract as the pipeline services.
    health = HealthMarker("baba", "hwstats")
    # Fail loud when the PICKED backend stops producing: the tool probed fine
    # at startup, so consecutive tick failures mean the GPU metrics on the
    # System page are silently frozen/absent — exactly the degradation class
    # we surface, not absorb. (Real incident: the non-root hardening dropped
    # CAP_PERFMON for uid 1000 and intel_gpu_top aborted every tick for hours
    # with only a debug-level trace.) Warn after 6 straight misses (~30 s),
    # then re-warn every ~10 min so the log stays readable.
    gpu_miss_streak = 0
    tap = _GaugeTap(stats)
    encoder = msgspec.msgpack.Encoder()
    samples: dict[str, list[float]] = {}
    bucket_started = time.monotonic()
    while not stop.is_set():
        collect_cpu_ram(tap, prev)
        if gpu_collect is not None:
            # nvidia-smi / intel_gpu_top shell out and can block for several
            # seconds (intel_gpu_top samples for ~2s). Run off the event loop
            # so the NATS stats publisher + signal handling stay responsive.
            ok = await asyncio.to_thread(gpu_collect, tap)
            if ok:
                if gpu_miss_streak >= 6:
                    log.info("gpu collector recovered after %d misses", gpu_miss_streak)
                gpu_miss_streak = 0
            else:
                gpu_miss_streak += 1
                if gpu_miss_streak == 6 or gpu_miss_streak % 120 == 0:
                    log.warning(
                        "gpu collector has produced no data for %d consecutive "
                        "ticks (~%ds) — GPU metrics on the System page are "
                        "stale/absent; run the tool manually inside this "
                        "container to see the underlying error",
                        gpu_miss_streak,
                        int(gpu_miss_streak * INTERVAL),
                    )
        for name in _BUCKET_GAUGES:
            value = tap.latest.get(name)
            if value is not None:
                samples.setdefault(name, []).append(value)
        elapsed = time.monotonic() - bucket_started
        if elapsed >= BUCKET_S and samples:
            payload: dict[str, float | int] = {"window_s": elapsed, "n": 0}
            for name, values in samples.items():
                payload["n"] = len(values)
                payload[f"{name}_avg"] = round(sum(values) / len(values), 2)
                payload[f"{name}_max"] = round(max(values), 2)
            try:
                await nc.publish(
                    SUBJECT_TELEMETRY_TEMPLATE.format(source="hardware", camera_id=HOST_SLUG),
                    encoder.encode(payload),
                )
            except Exception:
                log.exception("hardware telemetry flush failed")
            samples = {}
            bucket_started = time.monotonic()
        health.touch()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL)

    await stats.stop()
    await drain_quietly(nc)
    log.info("hwstats stopped")


if __name__ == "__main__":
    try:
        run_service(main())
    except KeyboardInterrupt:
        sys.exit(0)
