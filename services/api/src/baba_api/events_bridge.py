"""Mirror durable events onto the NATS bus for off-box consumers (DIDA).

The pipeline writes every event into the `events` table, which fires Postgres
NOTIFY 'events_new'. That serves in-process consumers fine (notifications,
the browser WebSocket) but the DIDA automation runtime lives on the NATS bus
and can't LISTEN on our Postgres. This bridge closes that gap: it LISTENs on
events_new, reads the full event row, and republishes it to
`baba.events.<camera_id>` — mirroring the per-camera subject convention the
rest of BABA already uses (`baba.tracks.<slug>`, `baba.detections.<slug>`).

The subject is keyed by the immutable camera UUID (not the slug, which the
operator can rename); the slug travels in the payload for convenience. A
consumer subscribes to `baba.events.*` for everything, or to a specific
camera. This covers ALL event kinds uniformly — zone_enter, track_finalized,
scene_state_change, … — so new kinds reach DIDA with no extra wiring.

Best-effort by design: if NATS is down the bridge logs and drops; the event is
already durably in Postgres, so nothing is lost for the in-process consumers.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

import asyncpg
from baba_core.pg_listen import ResilientListener
from baba_core.task_owner import TaskOwner

log = logging.getLogger(__name__)

_SUBJECT_PREFIX = "baba.events"


class EventsNatsBridge:
    def __init__(self, dsn: str, pool: asyncpg.Pool, nc) -> None:
        self._dsn = dsn
        self._pool = pool
        self._nc = nc
        self._listener: ResilientListener | None = None
        self._tasks = TaskOwner("api-events-bridge", log)

    async def start(self) -> None:
        self._listener = ResilientListener(
            self._dsn,
            ["events_new"],
            on_notify=self._on_notify,
            name="api-events-bridge",
        )
        await self._listener.start()
        log.info("events→NATS bridge listening on events_new")

    def _on_notify(self, _channel: str, payload: str) -> None:
        # Can't await inside the asyncpg notify callback — hand off to the loop.
        self._tasks.spawn(self._publish(payload))

    async def _publish(self, payload_text: str) -> None:
        try:
            note = json.loads(payload_text)
            event_id = UUID(note["id"])
        except (ValueError, KeyError, TypeError):
            log.warning("events bridge: undecodable NOTIFY payload %r", payload_text)
            return
        try:
            row = await self._pool.fetchrow(
                """
                SELECT e.id, e.camera_id, e.track_id, e.kind, e.at, e.payload,
                       c.slug AS camera_slug, c.name AS camera_name
                FROM events e
                LEFT JOIN cameras c ON c.id = e.camera_id
                WHERE e.id = $1
                """,
                event_id,
            )
            if row is None:
                return  # already deleted (e.g. static-track suppression cleanup)
            raw_payload = row["payload"]
            event_payload = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else (raw_payload or {})
            )
            msg = {
                "id": str(row["id"]),
                "camera_id": str(row["camera_id"]),
                "camera_slug": row["camera_slug"],
                "camera_name": row["camera_name"],  # authoritative friendly name (off-box DIDA uses it)
                "track_id": str(row["track_id"]) if row["track_id"] else None,
                "kind": row["kind"],
                "at": row["at"].isoformat(),
                "payload": event_payload,
            }
            await self._nc.publish(
                f"{_SUBJECT_PREFIX}.{row['camera_id']}", json.dumps(msg).encode()
            )
            # A doorbell press is an EVENT, not state — it gets its own subject
            # so a consumer can fire on it directly without filtering the event
            # stream (and without deduplicating: every press is meant to ring).
            # The press already flows through here on its way from the doorbell
            # service's `events` row, so this costs one extra publish and no new
            # NATS client in that service.
            # An arrival and a departure ride their own subject for the same
            # reason, and one more: a consumer that wants only these would
            # otherwise take the whole event stream to find them — 29510
            # `object_parked` rows a week for two useful ones, over a link that
            # is 0.79 Mbit/s at one of the sites.
            if row["kind"] in ("vehicle_arrived", "vehicle_left"):
                await self._nc.publish(
                    f"baba.place.{row['camera_id']}",
                    json.dumps(
                        {
                            "camera_id": str(row["camera_id"]),
                            "camera_slug": row["camera_slug"],
                            "kind": row["kind"],
                            "at": row["at"].isoformat(),
                            **event_payload,
                        },
                        default=str,
                    ).encode(),
                )
            if row["kind"] == "doorbell_press":
                await self._nc.publish(
                    f"baba.bell.{row['camera_id']}",
                    json.dumps(
                        {
                            "camera_id": str(row["camera_id"]),
                            "action": event_payload.get("action"),
                        }
                    ).encode(),
                )
        except Exception:
            # Never let a bus hiccup take down the listener; the event is
            # already durable in Postgres for the in-process consumers.
            log.exception("events bridge: publish failed for event %s", event_id)

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
        await self._tasks.stop()
