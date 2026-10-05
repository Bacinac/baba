import json
from datetime import UTC, datetime
from uuid import UUID

import asyncpg
from baba_core.events import TrackEvent


async def write_track_event(
    pool: asyncpg.Pool, camera_id: UUID, event: TrackEvent, timestamp_ns: int,
    *, zone_name: str | None = None, zone_kind: str | None = None,
) -> bool:
    status = await pool.execute(
        "INSERT INTO events (id, camera_id, track_id, kind, at, payload) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT (id) DO NOTHING",
        event.event_id(camera_id, timestamp_ns), camera_id, event.track_id, event.kind,
        datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC),
        json.dumps(event.payload(zone_name=zone_name, zone_kind=zone_kind)),
    )
    return status == "INSERT 0 1"
