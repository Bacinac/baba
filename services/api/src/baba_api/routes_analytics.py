"""Analytics endpoints for the /analytics page.

Pre-aggregated time-series + breakdown queries the UI renders as bar
charts. Each query returns a small, denormalised payload (typically
< 1 KB) so the page hydrates in one trip. Heavy lifting is done in
SQL via DATE_TRUNC / generate_series so we don't ship raw events to
the browser.

Default window: last 7 days. Wider windows are accepted up to 90 days;
beyond that the dashboard becomes a different feature (long-term
retention queries on potentially-pruned `events`) and we'd want to
move it to a separate aggregation table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from baba_core import local_today
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

analytics_router = APIRouter()


_MAX_DAYS = 90


class HourBucket(BaseModel):
    # Bucket start in UTC, ISO-8601. The UI converts to user TZ via the
    # existing datetime store so the bars line up with the operator's
    # working hours.
    bucket: datetime
    count: int


class EventsByHourOut(BaseModel):
    since: datetime
    until: datetime
    buckets: list[HourBucket]


@analytics_router.get("/analytics/events_by_hour", response_model=EventsByHourOut)
async def events_by_hour(
    request: Request,
    days: int = Query(default=7, ge=1, le=_MAX_DAYS),
    kind: str | None = Query(default=None),
    camera_id: str | None = Query(default=None),
) -> EventsByHourOut:
    """Hourly count of events over the last N days. Used by the
    analytics page to render the activity timeline.

    Postgres' DATE_TRUNC('hour', at) groups efficiently against the
    existing `(camera_id, at DESC)` index. Filling missing hours is
    done client-side — Postgres' generate_series would work but adds
    DB cost for what's a 168-element zero-fill at the typical 7-day
    window.
    """
    pool = request.app.state.pool
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    until = now + timedelta(hours=1)  # include the current (partial) hour
    since = until - timedelta(days=days)

    flt = SqlFilter()
    flt.add("at >= ?", since)
    flt.add("at < ?", until)
    if kind is not None:
        flt.add("kind = ?", kind)
    if camera_id is not None:
        try:
            flt.add("camera_id = ?", UUID(camera_id))
        except ValueError as e:
            raise HTTPException(400, f"invalid camera_id: {e}") from e

    sql = f"""
        SELECT DATE_TRUNC('hour', at) AS bucket, count(*)::int AS c
        FROM events
        {flt.where()}
        GROUP BY 1
        ORDER BY 1 ASC
    """  # noqa: S608
    rows = await pool.fetch(sql, *flt.args)
    return EventsByHourOut(
        since=since,
        until=until,
        buckets=[HourBucket(bucket=r["bucket"], count=r["c"]) for r in rows],
    )


class CameraCount(BaseModel):
    camera_id: str
    slug: str
    name: str
    count: int


class EventsByCameraOut(BaseModel):
    since: datetime
    until: datetime
    rows: list[CameraCount]


@analytics_router.get("/analytics/events_by_camera", response_model=EventsByCameraOut)
async def events_by_camera(
    request: Request,
    days: int = Query(default=7, ge=1, le=_MAX_DAYS),
) -> EventsByCameraOut:
    """Per-camera event totals over the window. Drives the "busiest
    cameras" bar chart. We LEFT JOIN cameras so a camera with zero
    events still appears (lets the operator spot an idle camera that
    might be misconfigured)."""
    pool = request.app.state.pool
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    until = now + timedelta(hours=1)
    since = until - timedelta(days=days)

    rows = await pool.fetch(
        """
        SELECT
            c.id, c.slug, c.name,
            COALESCE(e.cnt, 0)::int AS cnt
        FROM cameras c
        LEFT JOIN (
            SELECT camera_id, count(*) AS cnt
            FROM events
            WHERE at >= $1 AND at < $2
            GROUP BY camera_id
        ) e ON e.camera_id = c.id
        ORDER BY cnt DESC, c.slug ASC
        """,
        since,
        until,
    )
    return EventsByCameraOut(
        since=since,
        until=until,
        rows=[
            CameraCount(
                camera_id=str(r["id"]),
                slug=r["slug"],
                name=r["name"],
                count=r["cnt"],
            )
            for r in rows
        ],
    )


class KindCount(BaseModel):
    kind: str
    count: int


class EventsByKindOut(BaseModel):
    since: datetime
    until: datetime
    rows: list[KindCount]


class HeatmapOut(BaseModel):
    camera_id: str
    days: int
    class_group: str
    grid_w: int
    grid_h: int
    # Length = grid_w * grid_h. Row-major (y * grid_w + x). Values are
    # raw counts; the renderer normalises to [0,1] for opacity mapping.
    cells: list[int]
    max_count: int


# Same vocabulary used by event-manager.heatmap.class_group_for().
# "all" is the implicit sum-across-groups; the others are 1:1 with
# the rows the accumulator writes.
_HEATMAP_GROUPS = {"all", "person", "vehicle", "animal", "other"}


@analytics_router.get("/analytics/parking")
async def parking_history(request: Request, days: int = Query(default=7, ge=1, le=90)) -> list[dict[str, Any]]:
    """Parking episodes from the occupancy registry, newest first — who stood
    where, since when, for how long, on what evidence. Open episodes ride
    along with released_at null."""
    rows = await request.app.state.pool.fetch(
        """
        SELECT o.place, o.occupied_since, o.released_at, o.evidence,
               COALESCE(il.name, CASE WHEN o.evidence = 'plate'
                                      THEN pr.plate_text END) AS name
        FROM place_occupancy o
        LEFT JOIN identity_labels il ON il.global_id = o.global_id
        LEFT JOIN plate_reads pr ON pr.id = o.plate_read_id
        WHERE o.occupied_since > now() - make_interval(days => $1)
           OR o.released_at IS NULL
        ORDER BY o.occupied_since DESC
        LIMIT 200
        """,
        days,
    )
    return [
        {
            "place": r["place"],
            "name": r["name"],
            "evidence": r["evidence"],
            "occupied_since": r["occupied_since"].isoformat(),
            "released_at": r["released_at"].isoformat() if r["released_at"] else None,
            "duration_s": (
                ((r["released_at"] or datetime.now(UTC)) - r["occupied_since"]).total_seconds()
            ),
        }
        for r in rows
    ]


@analytics_router.get("/analytics/heatmap/{camera_id}", response_model=HeatmapOut)
async def heatmap(
    request: Request,
    camera_id: str,
    days: int = Query(default=7, ge=1, le=_MAX_DAYS),
    class_group: str = Query(default="all"),
) -> HeatmapOut:
    """Movement heatmap for one camera, summed over the last N days,
    optionally filtered to a class group. `all` (default) sums across
    every group server-side. An empty grid is a valid response — the
    camera might be new, idle, or the accumulator hasn't flushed yet
    (30s default window)."""
    if class_group not in _HEATMAP_GROUPS:
        raise HTTPException(
            400,
            f"invalid class_group {class_group!r}; allowed: {sorted(_HEATMAP_GROUPS)}",
        )
    pool = request.app.state.pool
    try:
        import uuid as _u

        cam_uuid = _u.UUID(camera_id)
    except ValueError as e:
        raise HTTPException(400, f"invalid camera_id: {e}") from e

    today = local_today()
    since = today - timedelta(days=days - 1)

    # Always pull the per-group rows; for "all" we sum all of them,
    # for a specific group we sum only the matching rows. We exclude
    # legacy class_group='all' rows from per-group queries — pre-030
    # data lives there and would double-count if added on top of fresh
    # per-group rows the accumulator now writes.
    if class_group == "all":
        rows = await pool.fetch(
            "SELECT cells, grid_w, grid_h FROM heatmap_daily WHERE camera_id = $1 AND day >= $2",
            cam_uuid,
            since,
        )
    else:
        rows = await pool.fetch(
            "SELECT cells, grid_w, grid_h FROM heatmap_daily "
            "WHERE camera_id = $1 AND day >= $2 AND class_group = $3",
            cam_uuid,
            since,
            class_group,
        )

    if not rows:
        return HeatmapOut(
            camera_id=camera_id,
            days=days,
            class_group=class_group,
            grid_w=32,
            grid_h=18,
            cells=[0] * (32 * 18),
            max_count=0,
        )

    canonical_w = int(rows[0]["grid_w"])
    canonical_h = int(rows[0]["grid_h"])
    total_cells = canonical_w * canonical_h
    merged = [0] * total_cells
    for r in rows:
        if int(r["grid_w"]) != canonical_w or int(r["grid_h"]) != canonical_h:
            continue
        cells = r["cells"] or []
        if len(cells) != total_cells:
            continue
        for i, v in enumerate(cells):
            merged[i] += int(v)
    return HeatmapOut(
        camera_id=camera_id,
        days=days,
        class_group=class_group,
        grid_w=canonical_w,
        grid_h=canonical_h,
        cells=merged,
        max_count=max(merged) if merged else 0,
    )


@analytics_router.get("/analytics/events_by_kind", response_model=EventsByKindOut)
async def events_by_kind(
    request: Request,
    days: int = Query(default=7, ge=1, le=_MAX_DAYS),
) -> EventsByKindOut:
    """Event-kind breakdown over the window (track_finalized vs zone_enter
    vs zone_exit vs zone_dwell). Useful for spotting "zone rules fire
    way more than expected"-type misconfigurations."""
    pool = request.app.state.pool
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    until = now + timedelta(hours=1)
    since = until - timedelta(days=days)
    rows = await pool.fetch(
        """
        SELECT kind, count(*)::int AS c
        FROM events
        WHERE at >= $1 AND at < $2
        GROUP BY kind
        ORDER BY c DESC
        """,
        since,
        until,
    )
    return EventsByKindOut(
        since=since,
        until=until,
        rows=[KindCount(kind=r["kind"], count=r["c"]) for r in rows],
    )
