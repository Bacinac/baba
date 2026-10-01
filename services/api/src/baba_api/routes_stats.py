"""Aggregate stats endpoints powering the Dashboard and System / Storage
settings pages. Kept separate from the per-resource CRUD routers because
these queries cut across multiple tables and the frontend treats them as
one cohesive surface ("show me everything at a glance")."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Request
from pydantic import BaseModel

log = logging.getLogger(__name__)

stats_router = APIRouter()


def _pool(request: Request):
    return request.app.state.pool


# --------------------------------------------------------------------------
# /stats — dashboard overview
# --------------------------------------------------------------------------


class StatsOut(BaseModel):
    cameras_total: int
    cameras_enabled: int
    events_total: int
    events_24h: int
    identities_total: int
    identities_labeled: int
    tracks_total: int
    recordings_total: int
    recordings_bytes: int
    # Cameras that produced at least one event in the last 24h — the
    # "are my cameras actually seeing things" health signal, independent of
    # `cameras_enabled` (operator config). The dashboard renders a per-camera
    # active badge from the id set; the count is its length.
    cameras_active_24h: int
    active_camera_ids_24h: list[str]


@stats_router.get("/stats", response_model=StatsOut)
async def get_stats(request: Request) -> StatsOut:
    """Dashboard aggregates. Single round-trip (one query per metric, all
    fired in parallel via asyncpg's connection pool). Cheap enough at
    real BABA scales — none of these scan more than a covering index."""
    pool = _pool(request)
    cutoff_24h = datetime.now(UTC) - timedelta(hours=24)
    async with pool.acquire() as conn:
        cameras_total = await conn.fetchval("SELECT count(*) FROM cameras")
        cameras_enabled = await conn.fetchval("SELECT count(*) FROM cameras WHERE enabled")
        events_total = await conn.fetchval("SELECT count(*) FROM events")
        events_24h = await conn.fetchval(
            "SELECT count(*) FROM events WHERE at >= $1",
            cutoff_24h,
        )
        # Distinct global_id over the tracks table is the canonical
        # "identity" count — labels are optional metadata sitting on top.
        identities_total = await conn.fetchval(
            "SELECT count(DISTINCT global_id) FROM tracks WHERE global_id IS NOT NULL"
        )
        identities_labeled = await conn.fetchval("SELECT count(*) FROM identity_labels")
        tracks_total = await conn.fetchval("SELECT count(*) FROM tracks")
        recordings_total = await conn.fetchval("SELECT count(*) FROM recordings")
        recordings_bytes = await conn.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM recordings"
        )
        active_rows = await conn.fetch(
            "SELECT DISTINCT camera_id FROM events WHERE at >= $1",
            cutoff_24h,
        )
        active_camera_ids_24h = [str(r["camera_id"]) for r in active_rows]
    return StatsOut(
        cameras_total=cameras_total or 0,
        cameras_enabled=cameras_enabled or 0,
        events_total=events_total or 0,
        events_24h=events_24h or 0,
        identities_total=identities_total or 0,
        identities_labeled=identities_labeled or 0,
        tracks_total=tracks_total or 0,
        recordings_total=recordings_total or 0,
        recordings_bytes=int(recordings_bytes or 0),
        cameras_active_24h=len(active_camera_ids_24h),
        active_camera_ids_24h=active_camera_ids_24h,
    )


# --------------------------------------------------------------------------
# /storage/usage — per-camera storage breakdown
# --------------------------------------------------------------------------


class CameraStorageRow(BaseModel):
    camera_id: str
    slug: str
    name: str
    color: str
    segments: int
    bytes: int
    oldest_at: datetime | None
    newest_at: datetime | None


class StorageUsageOut(BaseModel):
    media_path: str
    media_total_bytes: int
    media_free_bytes: int
    cameras: list[CameraStorageRow]


@stats_router.get("/storage/usage", response_model=StorageUsageOut)
async def get_storage_usage(request: Request) -> StorageUsageOut:
    """Per-camera storage breakdown + media-volume free-space.

    Computes from `recordings` table (cheap, indexed). Disk free-space
    is read from the media mount via os.statvfs — works for any backing
    store (HDD, NVMe, NFS, ZFS dataset) without service-specific code.
    Cameras with zero recordings still appear so the operator can see
    the full retention map. Sort by bytes desc so the heaviest cameras
    surface first.
    """
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id AS camera_id,
                c.slug,
                c.name,
                c.color,
                COALESCE(r.segments, 0) AS segments,
                COALESCE(r.bytes, 0) AS bytes,
                r.oldest_at,
                r.newest_at
            FROM cameras c
            LEFT JOIN (
                SELECT
                    camera_id,
                    count(*) AS segments,
                    COALESCE(sum(size_bytes), 0) AS bytes,
                    min(started_at) AS oldest_at,
                    max(started_at) AS newest_at
                FROM recordings
                GROUP BY camera_id
            ) r ON r.camera_id = c.id
            ORDER BY bytes DESC, c.slug ASC
            """
        )
    media_path = os.environ.get("BABA_MEDIA_PATH", "/media")
    total_bytes = 0
    free_bytes = 0
    try:
        stat = os.statvfs(media_path)
        total_bytes = stat.f_blocks * stat.f_frsize
        # f_bavail = blocks available to non-root processes — closer to
        # what the operator can actually use than f_bfree, which counts
        # reserved blocks too.
        free_bytes = stat.f_bavail * stat.f_frsize
    except OSError as e:
        log.warning("storage usage: statvfs(%s) failed: %s", media_path, e)
    return StorageUsageOut(
        media_path=media_path,
        media_total_bytes=total_bytes,
        media_free_bytes=free_bytes,
        cameras=[
            CameraStorageRow(
                camera_id=str(r["camera_id"]),
                slug=r["slug"],
                name=r["name"],
                color=r["color"],
                segments=int(r["segments"]),
                bytes=int(r["bytes"]),
                oldest_at=r["oldest_at"],
                newest_at=r["newest_at"],
            )
            for r in rows
        ],
    )


# --------------------------------------------------------------------------
# /system/info — service identity + build/env info
# --------------------------------------------------------------------------


class SystemInfoOut(BaseModel):
    api_version: str
    python_version: str
    variant: str
    # Human-readable accelerator the backends actually bound to, e.g.
    # "Intel(R) Arc(TM) A380 Graphics" or "Intel(R) UHD Graphics (iGPU)".
    # None when running on CPU or when the device can't be queried. The
    # variant alone is NOT a device name — every Intel box is not an Arc.
    gpu_device: str | None
    embedder_model: str
    face_models_present: bool
    detector_model: str
    # Detector head family the detector reported: "dfine" (D-FINE / RT-DETR) or
    # "rfdetr" (RF-DETR / LWDETR). It decides the postprocessor and the input
    # normalisation, so a model running under the wrong family yields garbage
    # boxes — worth showing next to the model rather than inferring from the
    # filename. None until the detector's first snapshot arrives.
    detector_family: str | None
    # Whether the state evaluator loaded the plate models, which are BYOM and
    # off until named. None until its first snapshot arrives.
    plate_reading: bool | None
    auto_describe_enabled: bool
    cookie_secure: bool
    cors_origins: list[str]
    postgres_sslmode: str


@lru_cache(maxsize=1)
def _gpu_name_from_smi() -> str | None:
    """The card's product name, straight from the driver.

    Needed because on NVIDIA no backend can answer the question: the only
    registered backend there is onnxruntime, and it enumerates EXECUTION
    PROVIDERS ("TensorrtExecutionProvider"), which the caller correctly
    refuses to print as hardware. That left the System page saying "NVIDIA"
    while the box was running an RTX 3060.

    Cached for the process lifetime — a card is not hot-swapped, and this
    would otherwise fork a process inside a request handler on every load of
    the System page.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no driver here — an Intel or CPU host, not an error
    if out.returncode != 0:
        return None
    name = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return name or None


def _accelerator_name() -> str | None:
    """Full device name of the GPU/NPU the inference backends bind to.

    Asks the registered backends (each already enumerates its own devices)
    rather than inferring from BABA_VARIANT — "intel" covers everything from
    an Arc A380 to an N305 iGPU, and printing the wrong one on the System page
    misleads exactly the person diagnosing performance."""
    from baba_core.registry import BACKENDS as registry

    # Ask the accelerator backends first and the generic ONNX Runtime last: its
    # enumerate_devices lists EXECUTION PROVIDERS, not hardware, so it happily
    # answers "AzureExecutionProvider" — which is what production reported until
    # this ordering existed.
    names = registry.names(only_available=True)
    names.sort(key=lambda n: n == "onnxruntime")
    for name in names:
        try:
            backend = registry.get(name)
            for dev in backend.enumerate_devices():
                dev_name = (getattr(dev, "device_name", "") or "").strip()
                if not dev_name or dev_name.upper().startswith("CPU"):
                    continue
                # An execution provider is a code path, not a device. Reporting
                # one as the GPU is worse than reporting nothing.
                if dev_name.endswith("ExecutionProvider"):
                    continue
                # Backends report "<ov-device> (<full name>)", e.g.
                # "GPU (Intel(R) UHD Graphics (iGPU))". The operator wants the
                # product, not our device handle — unwrap it, keeping the inner
                # parentheses the vendor string carries itself.
                _head, sep, rest = dev_name.partition(" (")
                if sep and rest.endswith(")"):
                    return rest[:-1]
                return dev_name
        except Exception:  # a backend that can't enumerate must not break the page
            log.warning("backend %s could not enumerate its devices", name, exc_info=True)
            continue
    # No backend could name the hardware. Ask the driver directly rather than
    # falling back to BABA_VARIANT, which only ever knew the vendor.
    return _gpu_name_from_smi()


@stats_router.get("/system/info", response_model=SystemInfoOut)
async def get_system_info(request: Request) -> SystemInfoOut:
    """Static-ish info about the running api process. Used by the System
    settings page so the operator can confirm at a glance which variant
    is deployed, what models the embedder picked up, etc., without
    needing shell access into the container."""
    import sys

    from baba_api.revision import version_string

    config = request.app.state.config
    face_models_present = getattr(request.app.state, "face_stack", None) is not None
    embedder_path = config.embedder_model_path
    # The detector reports the model it actually loaded (stats labels); our own
    # env is only a fallback for the window before its first snapshot arrives.
    # They diverge whenever the model is swapped without recreating the api.
    detector_path = os.environ.get("BABA_DETECTOR_MODEL", "")
    aggregator = getattr(request.app.state, "stats_aggregator", None)
    reported = getattr(aggregator, "labels_for", None)
    detector_family = os.environ.get("BABA_DETECTOR_MODEL_FAMILY") or None
    plate_reading = None
    if callable(reported):
        labels = reported("detector") or {}
        detector_path = labels.get("model") or detector_path
        detector_family = labels.get("family") or detector_family
        plates = (reported("state-evaluator") or {}).get("plates")
        plate_reading = None if plates is None else plates == "on"
    return SystemInfoOut(
        api_version=version_string(),
        python_version=sys.version.split()[0],
        variant=os.environ.get("BABA_VARIANT", "cpu"),
        gpu_device=_accelerator_name(),
        embedder_model=embedder_path,
        face_models_present=face_models_present,
        detector_model=detector_path,
        detector_family=detector_family,
        plate_reading=plate_reading,
        auto_describe_enabled=config.auto_describe_enabled,
        cookie_secure=os.environ.get("BABA_COOKIE_SECURE", "false").lower() in ("1", "true", "yes"),
        cors_origins=list(config.cors_origins),
        postgres_sslmode=os.environ.get("POSTGRES_SSLMODE", "") or "prefer (default)",
    )


# --------------------------------------------------------------------------
# /system/metrics — live per-service pipeline runtime stats
# --------------------------------------------------------------------------
# Fed by StatsAggregator (one NATS subscription to baba.stats.*). In-memory
# and ephemeral by design — this is operational liveness for the System page,
# not durable history. The UI polls this at roughly the publish cadence (~5s).


class TimingOut(BaseModel):
    count: int
    p50: float
    p95: float
    p99: float


class HistoryPoint(BaseModel):
    ts_ns: int
    rates: dict[str, float]
    gauges: dict[str, float]
    timings_p95: dict[str, float]


class ServiceMetrics(BaseModel):
    service: str
    instance: str
    uptime_s: float
    # Seconds since this snapshot was produced — freshness signal. `stale`
    # is True once the service has missed several publish intervals (likely
    # crashed/wedged), so the UI can grey the card without guessing.
    age_s: float
    stale: bool
    rates: dict[str, float]
    gauges: dict[str, float]
    counters: dict[str, int]
    timings: dict[str, TimingOut]
    last_error: str | None = None
    last_error_age_s: float | None = None
    history: list[HistoryPoint]


class SystemMetricsOut(BaseModel):
    generated_ts_ns: int
    services: list[ServiceMetrics]


@stats_router.get("/system/metrics", response_model=SystemMetricsOut)
async def get_system_metrics(request: Request) -> SystemMetricsOut:
    """Live runtime stats per pipeline service for Settings → System.

    Returns whatever services have published on `baba.stats.*` since the API
    started; a service that never published (down, or pre-upgrade build)
    simply won't appear. Empty until the first snapshots arrive (~5s)."""
    agg = getattr(request.app.state, "stats_aggregator", None)
    if agg is None:
        return SystemMetricsOut(generated_ts_ns=0, services=[])
    return SystemMetricsOut(**agg.view())
