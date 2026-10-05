from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from baba_core.recordings import covers_until_sql
from fastapi import APIRouter, Query, Request

from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

events_router = APIRouter()


@events_router.get("/events")
async def list_events(
    request: Request,
    camera_id: UUID | None = Query(default=None),
    kind: str | None = Query(default=None, max_length=64),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=5000),
) -> list[dict[str, Any]]:
    """List event snapshots with camera, track and recording metadata, newest first."""
    pool = request.app.state.pool
    flt = SqlFilter()

    if camera_id is not None:
        flt.add("e.camera_id = ?", camera_id)
    if kind is not None:
        flt.add("e.kind = ?", kind)
    else:
        # The Events page is the zone/alert log. `track_finalized` events are
        # 1:1 with tracks and fully covered by the Sightings view, so they're
        # excluded here to avoid duplicating that surface. Still reachable via
        # an explicit ?kind=track_finalized for debugging.
        flt.conds.append("e.kind <> 'track_finalized'")
    if since is not None:
        flt.add("e.at >= ?", since)
    if until is not None:
        flt.add("e.at < ?", until)

    where = flt.where()
    limit_param = flt.bind(limit)

    # Join the recording segment that covers the event timestamp, if any.
    # "covers" = started_at <= e.at < when the segment stops covering, which
    # for a row with no `ended_at` is its start plus one segment's worth and
    # not the end of time — see `covers_until_sql`. That distinction is what
    # keeps a segment the indexer abandoned hours ago from claiming an event
    # it never held. The in-progress segment is still linked (it is inside the
    # grace); the frontend gates on `ended_at` before trying to seek it.
    sql = f"""
        SELECT
            e.id, e.kind, e.at, e.payload,
            e.camera_id,
            c.slug AS camera_slug, c.name AS camera_name,
            e.track_id,
            t.thumbnail_path AS track_thumbnail_path,
            EXTRACT(EPOCH FROM (t.ended_at - t.started_at))::int AS track_duration_s,
            t.started_at AS track_started_at,
            t.ended_at AS track_ended_at,
            r.id AS recording_id,
            r.started_at AS recording_started_at,
            r.ended_at AS recording_ended_at,
            r.duration_s AS recording_duration_s
        FROM events e
        JOIN cameras c ON c.id = e.camera_id
        LEFT JOIN tracks t ON t.id = e.track_id
        LEFT JOIN LATERAL (
            -- Anchor on the segment containing the track's START (the clip's
            -- entry point), falling back to the event timestamp when there is
            -- no track. Anchoring on e.at instead would make the seek clamp to
            -- 0 whenever the track began in an earlier segment.
            SELECT id, started_at, ended_at, duration_s
            FROM recordings
            WHERE camera_id = e.camera_id
              AND started_at <= COALESCE(t.started_at, e.at)
              AND COALESCE(t.started_at, e.at) < {covers_until_sql()}
            ORDER BY started_at DESC
            LIMIT 1
        ) r ON true
        {where}
        ORDER BY e.at DESC
        LIMIT {limit_param}
    """  # noqa: S608
    rows = await pool.fetch(sql, *flt.args)
    out: list[dict[str, Any]] = []
    for r in rows:
        # `start_at`/`end_at` is the absolute window the player cuts a clip
        # from (it spans whatever segments the visit covers). `id`/`seek_seconds`
        # deep-link the Recordings page to the segment containing the track's
        # start; the 3s lead-in covers detector warm-up + ByteTrack init.
        recording: dict[str, Any] | None = None
        if r["recording_id"] is not None and r["recording_started_at"] is not None:
            rec_start = r["recording_started_at"]
            entry = r["track_started_at"] or r["at"]
            exit_ = r["track_ended_at"] or r["at"]
            seek = max(0.0, (entry - rec_start).total_seconds() - 3.0)
            recording = {
                "id": str(r["recording_id"]),
                "seek_seconds": seek,
                "start_at": entry.isoformat(),
                "end_at": exit_.isoformat(),
            }
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        out.append(
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "at": r["at"].isoformat(),
                "payload": payload,
                "camera": {
                    "id": str(r["camera_id"]),
                    "slug": r["camera_slug"],
                    "name": r["camera_name"],
                },
                "track": (
                    {
                        "id": str(r["track_id"]),
                        "class_id": payload.get("class_id"),
                        "class_name": payload.get("class_name"),
                        "duration_s": r["track_duration_s"],
                        "thumbnail_path": r["track_thumbnail_path"],
                    }
                    if r["track_id"] is not None
                    else None
                ),
                "recording": recording,
            }
        )
    return out
