"""Presence episodes: a named person's stay on a camera, as a durable record.

The live identity chain already answers "who is on this camera right now" —
but only in RAM, per track, and the track is the most fragile thing in the
pipeline. This module turns those verdicts into episodes (migration 074):
opened when a verdict first lands, confirmed while verdicts keep landing,
closed only on a measured departure signal.

Track death is deliberately NOT a close signal. That equivalence has already
produced one documented incident (the false "Odlazak": a visit's end tied to
the death of a track the tracker had simply lost), and the measurements here
confirm it: the tracker loses a person who never moved for a median 17 s and
a p90 of 25 min, while the DETECTOR — during gaps short enough that the
person was demonstrably still there — never went more than 4 consecutive
minutes without seeing them. The close rules are built on the sturdy signal,
not the fragile one.

The thresholds are constants, not tunables, on purpose: each is a measured
quantity with its measurement written next to it, and the settings audit of
2026-07-25 showed what happens when knobs accumulate instead.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from baba_core import VEHICLE_GROUP

log = logging.getLogger(__name__)

# Re-confirm an open episode in the DB at most this often. Verdicts arrive per
# tracks message (a few per second per camera); last_confirmed_at only has to
# be fresh relative to close rules measured in minutes.
_CONFIRM_EVERY_S = 30.0

# A verdict must SUSTAIN this long before it opens an episode at all. A real
# stay re-confirms every tick for as long as the track lives; a
# misidentification lives exactly as long as its bad crop. The day-one case:
# a short-lived track of Marko's face at a bad angle matched 'Ana' at 0.574
# ONCE (their faces are measurably adjacent — 9/64 enrolled pairs cross the
# gate), died within seconds, and a durable "Ana on the patio" episode stood
# open for half an hour while she was demonstrably away. One observation is
# noise; twenty seconds of them is a person.
_OPEN_AFTER_S = 20.0

# Forget a pending first-sighting if it is not re-observed within this long —
# whatever produced it is gone.
_PENDING_TTL_S = 60.0

# Close an episode as "moved elsewhere" when the identity is confirmed on
# ANOTHER camera while this one has not confirmed them for this long. Median
# camera-to-camera transition over 7 days of named tracks: 6 seconds. Two
# minutes is far above any real walk between views; overlapping cameras that
# BOTH see the person keep both episodes confirmed, so neither closes.
_ELSEWHERE_UNCONFIRMED = "2 minutes"

# Close every person episode on a camera that has seen NO person at all for
# this long. Measured on churn gaps short enough that the person was certainly
# still there: the detector's longest blind stretch was 4 consecutive minutes
# (p90: 3). Ten minutes is 2.5x that worst case. The rule only fires when the
# camera's detector telemetry is PRESENT and person-free — a camera that
# stopped reporting proves nothing and closes nothing.
_ABSENCE_WINDOW = "10 minutes"

# How far back a linked vehicle's departure reaches when closing its person's
# episodes, and how quiet the person must have gone around it. A person who is
# STILL being confirmed after the car left plainly did not leave with it, and
# their episodes keep flowing confirmations — the rule never touches them.
_VEHICLE_DEPARTURE_WINDOW = "15 minutes"
_VEHICLE_QUIET_MARGIN = "2 minutes"

# The arrival exclusion: a place that filled this close to the vehicle track's
# end means the car STOPPED — it did not leave, and neither did its person.
# The track ends moments before the classifier commits the fill (park
# finalize), so most of the width sits on the lookahead side; a two-minute
# disagreement between this window and what the registry called an arrival
# once made one car an arrival and a departure at once.
_ARRIVAL_LOOKBACK = "3 minutes"
_ARRIVAL_LOOKAHEAD = "5 minutes"

# A day with no confirmation closes the episode as stale. This is the bound on
# how long a dead camera can hold someone "present" — a visible, diagnosable
# failure (closed_by='stale' rows) rather than a silent eternal presence.
_STALE_AFTER = "24 hours"


class PresenceRegistry:
    """Writes presence episodes from live identity verdicts, closes them on
    measured departure signals. One instance, owned by the event-manager."""

    def __init__(self, pool) -> None:
        self._pool = pool
        # (gid, camera_id) -> monotonic time of the last DB confirm, so the
        # per-message verdict stream costs one UPDATE per episode per
        # _CONFIRM_EVERY_S, not per frame.
        self._last_write: dict[tuple[str, str], float] = {}
        # (gid, camera_id) -> evidence last written, so a body->face upgrade
        # bypasses the debounce (it changes what the row claims, not just when).
        self._evidence: dict[tuple[str, str], str] = {}
        # (gid, camera_id) -> (first monotonic, first wall-clock, last monotonic)
        # for verdicts that have not yet earned a row — see _OPEN_AFTER_S.
        self._pending: dict[tuple[str, str], tuple[float, float, float]] = {}
        # (gid, camera_id) -> the open episode's present_since, as the DB row
        # carries it. Fed by RETURNING on every (debounced) upsert, so after a
        # restart the FIRST write re-learns the true start of an episode that
        # was already open — the snapshot's "since" must not reset to the
        # moment the process happened to come back.
        self._since: dict[tuple[str, str], object] = {}

    async def observe(self, camera_id: UUID, matched: dict) -> None:
        """Fold one tracks message's verdicts in. `matched` is the live
        matcher's output: {gid: (name, source)} with source 'face'|'body'."""
        now = time.monotonic()
        for gid, (_name, source) in matched.items():
            key = (str(gid), str(camera_id))
            opened_at = None
            if key not in self._last_write:
                pend = self._pending.get(key)
                if pend is None or now - pend[2] > _PENDING_TTL_S:
                    # First sighting (or the previous one expired unre-observed).
                    self._pending[key] = (now, time.time(), now)
                    continue
                first_mono, first_wall, _last = pend
                self._pending[key] = (first_mono, first_wall, now)
                if now - first_mono < _OPEN_AFTER_S:
                    continue
                # Sustained: the episode starts when the person ARRIVED, not
                # when they had been visible long enough for us to commit.
                del self._pending[key]
                opened_at = datetime.fromtimestamp(first_wall, tz=UTC)
            else:
                fresh = now - self._last_write[key] < _CONFIRM_EVERY_S
                if fresh and self._evidence.get(key) == source:
                    continue
            self._last_write[key] = now
            self._evidence[key] = source
            since = await self._pool.fetchval(
                """
                INSERT INTO presence_episodes
                    (global_id, camera_id, evidence, present_since, last_confirmed_at)
                VALUES ($1, $2, $3,
                        COALESCE($4::timestamptz, now()), now())
                ON CONFLICT (global_id, camera_id) WHERE departed_at IS NULL
                DO UPDATE SET
                    last_confirmed_at = now(),
                    -- face is final; a later body verdict never downgrades it.
                    evidence = CASE WHEN presence_episodes.evidence = 'face'
                                    THEN 'face' ELSE EXCLUDED.evidence END
                RETURNING present_since
                """,
                gid,
                camera_id,
                source,
                opened_at,
            )
            self._since[key] = since

    def since_of(self, camera_id, gid):
        """When the open episode for (gid, camera) began, or None while the
        verdict is still pending / no episode is open. Cache-only on purpose:
        this is read per tracks message and must never cost a query."""
        return self._since.get((str(gid), str(camera_id)))

    def _forget(self, rows) -> None:
        """Drop closed episodes' in-memory state, so a re-appearance has to
        SUSTAIN again before it opens a new row — the gate would otherwise
        apply only to identities never seen on the camera before."""
        for r in rows:
            key = (str(r["global_id"]), str(r["camera_id"]))
            self._last_write.pop(key, None)
            self._evidence.pop(key, None)
            self._since.pop(key, None)

    async def sweep(self) -> None:
        """Close episodes whose person has demonstrably left. Runs every ~30 s.

        Every close sets departed_at = last_confirmed_at: the episode ends when
        the person was last seen, not when the sweep noticed.
        """
        rows = await self._pool.fetch(
            f"""
            UPDATE presence_episodes e
            SET departed_at = e.last_confirmed_at, closed_by = 'elsewhere'
            WHERE e.departed_at IS NULL
              AND e.last_confirmed_at < now() - interval '{_ELSEWHERE_UNCONFIRMED}'
              AND EXISTS (
                  SELECT 1 FROM presence_episodes o
                  WHERE o.global_id = e.global_id
                    AND o.id <> e.id
                    AND o.departed_at IS NULL
                    AND o.last_confirmed_at > e.last_confirmed_at)
            RETURNING e.global_id, e.camera_id
            """  # noqa: S608
        )
        self._forget(rows)
        for r in rows:
            log.info("presence: %s left camera %s (seen elsewhere)",
                     r["global_id"], r["camera_id"])

        rows = await self._pool.fetch(
            f"""
            UPDATE presence_episodes e
            SET departed_at = e.last_confirmed_at, closed_by = 'absence'
            FROM cameras c
            WHERE c.id = e.camera_id
              AND e.departed_at IS NULL
              AND e.last_confirmed_at < now() - interval '{_ABSENCE_WINDOW}'
              -- the camera IS reporting…
              AND EXISTS (
                  SELECT 1 FROM camera_telemetry ct
                  WHERE ct.camera_slug = c.slug AND ct.source = 'detector'
                    AND ct.at > now() - interval '{_ABSENCE_WINDOW}')
              -- …and not one of its minutes saw a person.
              AND NOT EXISTS (
                  SELECT 1 FROM camera_telemetry ct
                  WHERE ct.camera_slug = c.slug AND ct.source = 'detector'
                    AND ct.at > now() - interval '{_ABSENCE_WINDOW}'
                    AND COALESCE((ct.payload->'published'->'person'->>'n')::int, 0) > 0)
            RETURNING e.global_id, e.camera_id
            """  # noqa: S608
        )
        self._forget(rows)
        for r in rows:
            log.info("presence: %s left camera %s (camera person-free)",
                     r["global_id"], r["camera_id"])

        rows = await self._pool.fetch(
            f"""
            UPDATE presence_episodes e
            SET departed_at = e.last_confirmed_at, closed_by = 'vehicle'
            FROM identity_labels v
            JOIN tracks t ON t.global_id = v.global_id
            WHERE v.linked_person = e.global_id
              AND e.departed_at IS NULL
              AND t.class_id = ANY($1::int[])
              AND t.ever_active
              AND t.ended_at > now() - interval '{_VEHICLE_DEPARTURE_WINDOW}'
              -- the person went quiet around the departure; someone still
              -- being confirmed did not leave with the car.
              AND e.last_confirmed_at < t.ended_at + interval '{_VEHICLE_QUIET_MARGIN}'
              -- this track's own plate read named a place: it arrived.
              AND NOT EXISTS (
                  SELECT 1 FROM place_occupancy po
                  JOIN plate_reads pr ON pr.id = po.plate_read_id
                  WHERE pr.track_id = t.id)
              -- a place filled where this car came to rest: it stopped, it
              -- did not leave. Only for a car that came to rest at all, so a
              -- departure passing an unrelated arrival is not swallowed.
              AND NOT (
                  t.came_to_rest
                  AND EXISTS (
                      SELECT 1 FROM place_occupancy po
                      WHERE t.ended_at
                              BETWEEN po.occupied_since - interval '{_ARRIVAL_LOOKBACK}'
                                  AND po.occupied_since + interval '{_ARRIVAL_LOOKAHEAD}'
                        AND EXISTS (
                            SELECT 1 FROM scene_regions sr
                            WHERE sr.camera_id = t.camera_id
                              AND sr.place = po.place AND sr.enabled)))
            RETURNING e.global_id, e.camera_id, v.global_id AS vehicle_gid,
                      t.camera_id AS via_camera, t.ended_at
            """,  # noqa: S608
            [int(c) for c in VEHICLE_GROUP],
        )
        self._forget(rows)
        for r in rows:
            log.info("presence: %s left with vehicle %s",
                     r["global_id"], r["vehicle_gid"])
            # One Activity event per departure. A person open on three cameras
            # left once, and their episodes go quiet minutes apart, so the
            # already-written event is what says so — a set held for the length
            # of one sweep could not, and the operator saw one departure four
            # times on 17.08.
            await self._pool.execute(
                """
                INSERT INTO events (camera_id, kind, at, payload)
                SELECT $1, 'left_with_vehicle', $2, $3::jsonb
                 WHERE NOT EXISTS (
                     SELECT 1 FROM events e
                      WHERE e.kind = 'left_with_vehicle'
                        AND e.at BETWEEN $2::timestamptz - interval '5 minutes'
                                     AND $2::timestamptz + interval '5 minutes'
                        AND e.payload->>'person_gid' = $4
                        AND e.payload->>'vehicle_gid' = $5)
                """,
                r["via_camera"],
                r["ended_at"],
                json.dumps({
                    "person_gid": str(r["global_id"]),
                    "vehicle_gid": str(r["vehicle_gid"]),
                }),
                str(r["global_id"]),
                str(r["vehicle_gid"]),
            )

        rows = await self._pool.fetch(
            f"""
            UPDATE presence_episodes e
            SET departed_at = e.last_confirmed_at, closed_by = 'stale'
            WHERE e.departed_at IS NULL
              AND e.last_confirmed_at < now() - interval '{_STALE_AFTER}'
            RETURNING e.global_id, e.camera_id
            """  # noqa: S608
        )
        self._forget(rows)
        for r in rows:
            log.warning("presence: episode of %s on camera %s closed STALE — "
                        "nothing confirmed for a day, check the camera",
                        r["global_id"], r["camera_id"])

        # The debounce map grows one entry per (person, camera) pair; prune
        # entries whose episode has closed so it cannot grow unbounded.
        if len(self._last_write) > 512:
            open_rows = await self._pool.fetch(
                "SELECT global_id, camera_id FROM presence_episodes "
                "WHERE departed_at IS NULL"
            )
            keep = {(str(r["global_id"]), str(r["camera_id"])) for r in open_rows}
            self._last_write = {k: v for k, v in self._last_write.items() if k in keep}
            self._evidence = {k: v for k, v in self._evidence.items() if k in keep}
