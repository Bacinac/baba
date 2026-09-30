"""Reading a vehicle's plate off the recorded segment, after the fact.

This lives in the state evaluator because it is the only service that already
decodes NATIVE recorded frames, and native resolution is the whole game:
`cameras.downscale_max_edge` caps west at 1920 from a native 4096, and the plate
that reads perfectly at 96-183 px in the segment arrives at the SHM ring as
45-86 px, which measured twenty-four different strings for one plate.

It runs as a sweep rather than on a track-finalized message, and that is not
just the simpler wiring. A track ends the moment the car leaves frame, but the
segment covering that moment is still open and unwritten for up to a minute
afterwards. Waiting is not a workaround here, it is the requirement.

The reading window is narrow. On the measured arrival the plate was legible for
about two seconds out of a thirty-second track, so frames are sampled densely
across the whole track and most of them return nothing. That is expected: the
question is not whether every frame reads, it is whether the good two seconds
are hit at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import cv2
import numpy as np
from baba_core.occupancy import demote_rival_places, reconcile_places
from baba_core.paths import PLATE_CROPS, MediaLayout
from baba_core.plates import (
    PlateSighting,
    PlateVote,
    bind_to_place,
    drop_fixtures,
    drop_standing,
    even_times,
    focus_times,
    match_gallery,
    moving_samples,
)
from baba_core.recordings import covers_until_sql

log = logging.getLogger(__name__)

# Vehicle COCO ids: car, motorcycle, bus, truck.
_VEHICLE_CLASS_IDS = (2, 3, 5, 7)

# Where to spend those frames: across the span the SAMPLES cover, not the span
# the track claims. West's vehicle track ran 15:44 to 17:39 because it had held
# a parked car all afternoon, while every one of its forty-nine samples falls in
# the thirty-one seconds of the arrival. Scanning the track's own window spent
# the whole budget on an empty afternoon and never reached the car.
#
# Ranking by box area was tried instead and is also wrong: the biggest boxes on
# that track are 17:39:37-42, when the car has swung round to reverse into its
# space and the plate faces away. It read at 17:39:17-22, mid-approach and
# head-on, at half that box size. Which moment shows the plate is not something
# a box tells us, so the span is swept evenly and the vote sorts it out.
#
# But only the MOVING part of it. A parked car is not going to show a plate it
# was not already showing, and reading one is worse than wasteful: shed's track
# spent seventy-one minutes framing the other household car standing in the
# background and read its plate a hundred and fifteen times, unanimously, which
# would have named the arrival after the wrong vehicle. A plate belongs to the
# car that drove in, and it stays attached while that car stands.

# Never spend more than this many decoded frames on one track, however long it
# parked for. A car that sits for an hour is not more readable for it.
_MAX_FRAMES = 120

# A plate box narrower than this is the unusable band. Measured: 46 px produced
# twenty-four strings for one plate, 96 px read it exactly. The floor sits
# between them and closer to the failure, so a marginal read is still collected
# and merely outvoted rather than being silently discarded.
_MIN_PLATE_PX = 70

# How much of the vehicle box to keep around the plate. The tracker's box is
# drawn on the downscaled frame and lags a moving car by a frame or two, so it
# is padded before being trusted as a crop.
_BBOX_PAD = 0.30

# An approach takes seconds. Capping each zone span keeps a car that stopped
# INSIDE the zone (enter, no exit for an hour) from turning its whole stay into
# a read window.
_MAX_SPAN = timedelta(seconds=90)

# Wait this long after a track ends before trying: the covering segment has to
# be closed and indexed first.
_SETTLE = timedelta(minutes=2)
_LOOKBACK = timedelta(hours=6)

# The window a car arriving at a place drove through, either side of the
# moment the place read `present`. The classifier wants several agreeing
# evaluations before it commits, and the car has to cross the approach and
# manoeuvre before that, so the reach backwards is generous; forwards it is
# only clock slack. Measured on the 26.08 arrival: the plate was read at
# 16:12:33 and the place committed at 16:13:20, 47 s later.
_FILL_READ_BEFORE = timedelta(seconds=100)
_FILL_READ_AFTER = timedelta(seconds=20)

# The frames to spend on that window — and this is the number the whole path
# turns on. The plate is legible for about ONE SECOND of an approach: on the
# 26.08 arrival every one of the six readings falls between 16:12:33 and
# 16:12:34. Spreading 120 frames over two minutes samples every 1.75 s and
# walks straight past it, which is exactly what the first run of this code
# did — it decoded the right footage and reported nothing. At four frames a
# second the window cannot be missed. Nothing waits on this (it runs on CPU,
# minutes after the fact, once per episode) so the cost is affordable
# precisely because it is rare.
_FILL_FRAMES = int((_FILL_READ_BEFORE + _FILL_READ_AFTER).total_seconds() * 4)
# How far back the fill sweep will reach for an episode nobody read. Long
# enough to cover a car that arrived overnight and a deploy that spanned an
# arrival; short enough that it never re-litigates last week.
_FILL_LOOKBACK = timedelta(hours=36)

# An enrolled plate is named through the gallery, and a vote no single frame
# agrees with can still land there — that is what the gallery margin is for.
# An unenrolled plate has no gallery: its raw string goes on the place badge,
# so the string itself must have been read, not assembled. 12.09, west P1: a
# guest car under the carport, seven readings of the roof trim, seven different
# strings, and the per-position majority `N06066` — which no frame returned —
# became the name of the place.
_UNMATCHED_MIN_SEEN = 2
# A place that emptied this recently and filled again did not receive a car;
# it lost one and found it. Measured across the nightly IR flaps: the gap
# between release and re-fill is tens of seconds.
_FLAP_WINDOW = timedelta(minutes=5)

# A car that has just parked still has its lights on, and the approach search
# gets first refusal. Ten minutes is past both.
_RESIDENT_SETTLE = timedelta(minutes=10)
# How often an unnamed place is asked again. The picture changes with the
# light, not with the minute, so this is a slow loop: on the 04.09 night the
# answer would have arrived on the first pass after the lights went off.
_RESIDENT_RETRY = timedelta(minutes=30)
# How much recent footage one attempt looks at, and how many frames out of it.
# A standing car shows the same plate in every one of them, so this buys a
# vote rather than a chance — six frames is what makes a consensus honest
# without paying for a seventh that cannot disagree.
_RESIDENT_WINDOW = timedelta(minutes=2)
_RESIDENT_FRAMES = 6
# How much room, in the segment's own pixels, is left around a place region
# before it is handed to the detector. This is a crop, not a verdict: it exists
# so the plate hanging off the region's near edge is inside the picture at all,
# and what the reading then belongs to is decided by `bind_to_place` against
# every place on the camera.
#
# Fixed pixels rather than a fraction of the region, because the regions are
# not the same size: west P1 is 983 px wide and a proportional margin would
# hand the detector a 2163 px crop, which it resizes to 608 — the coarseness
# that loses plates in the first place. At 200 the crops land near the 640 that
# read this property's plates at 0.9 confidence.
_RESIDENT_CROP_REACH_PX = 200.0

# The window around a crossing of the plate zone. The car is legible on the
# approach INTO the zone as much as inside it — measured 27.08: the crossings
# fired 15:51:12-15 and the plate read at 15:51:15 — so the window opens
# before the first crossing and closes a little after the last. Short by
# design: this is the tightest of the three triggers, which is what makes it
# the cheapest and the fastest.
_PASS_READ_BEFORE = timedelta(seconds=20)
_PASS_READ_AFTER = timedelta(seconds=40)
# How far back the pass sweep reaches for a crossing nobody has read. Shorter
# than the fill sweep's: a pass is a moment, and if its footage has aged out
# of this window the fill sweep is the one still holding the question open.
_PASS_LOOKBACK = timedelta(hours=6)

# One arrival is several tracks. A car coming up the drive is seen by the gate,
# then the shed, then west, and the plate is legible on whichever of them it
# happened to face. So a track is read together with every vehicle track
# overlapping it on OTHER cameras — but each camera votes for ITSELF.
#
# Pooling their readings was tried and is wrong. Overlapping in time does not
# make two tracks the same car: on the 25.07 arrival the shed track's crop
# caught the OTHER household car parked in the background, and pooled voting
# stamped its plate onto west's track, which was already correctly identified
# as this one. Same-place-same-minute is not evidence of same-car, and a plate
# is far too strong a claim to hang on it.
_GROUP_TOLERANCE = timedelta(seconds=45)
_MAX_GROUP = 4
# Each member gets the FULL frame budget, not a share of one. Splitting it
# starved the camera that mattered: west's thirty-frame share covered the first
# seven seconds of its track and the plate does not become legible until the
# sixth, so it read nothing while shed spent its share on a car parked in the
# background. There is no deadline here — this runs on CPU, minutes after the
# fact, with nothing waiting on it — so the cost is paid by sweeping fewer
# tracks per pass instead.


# A zone of this kind is a fixed stretch of driveway where plates are legible.
# Given one, the reader stops depending on the track at all for WHERE to look —
# it only needs the track to know WHEN. That removes the three things that each
# broke in turn on the 27.07 manoeuvre: sample density (183 observations, 10
# samples), box staleness (a crop 27 seconds behind the car), and inferring
# motion from those same sparse samples (a distant car reads as standing still).
# The plate was legible on fifteen consecutive frames throughout; nothing was
# ever pointed at it.
_ALPR_ZONE_KIND = "alpr"

# A zone is a fixed rectangle, so anything standing in it is read on every
# frame forever. It still says WHERE to look; whose plate it is falls to
# `drop_fixtures`, which is why each reading carries the tracked box position.
# Requiring the plate INSIDE that box was tried and measured wrong — it removes
# west's only correct readings and nothing the travel rule does not already.


class _NotOurs(Exception):
    """The episode took a name while its car was being read."""


class PlateReader:
    """Sweeps recently-ended vehicle tracks and reads their plates."""

    def __init__(self, pool: Any, media_root: Path, stack: Any) -> None:
        self._pool = pool
        self._media_root = media_root
        self._stack = stack

    async def run(self, stop: asyncio.Event, interval_s: float = 60.0) -> None:
        while not stop.is_set():
            try:
                await self._sweep()
            except Exception:
                log.exception("plate sweep failed — continuing")
            # Passes first: it is the earliest signal and the cheapest window,
            # so an arrival it answers spares the fill sweep its much wider
            # scan a few minutes later.
            try:
                await self._sweep_zone_passes()
            except Exception:
                log.exception("zone-pass sweep failed — continuing")
            try:
                await self._sweep_fills()
            except Exception:
                log.exception("fill sweep failed — continuing")
            try:
                await self._sweep_residents()
            except Exception:
                log.exception("resident sweep failed — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def _sweep_zone_passes(self) -> None:
        """Read the footage of a car crossing the plate zone.

        The earliest honest signal there is that a car is arriving, and the
        only one that does not depend on a track outliving the moment. On the
        27.08 arrival the plate was legible at 15:51:15 while the west track
        that reached the database was born at 15:51:21 — six seconds late,
        with its first sample at its own birth, so every window derived from
        it began after the answer had gone. All four zone_enter events of that
        pass belonged to tracks that were suppressed and never stored.

        So the pass drives itself. `events` keeps the crossing whether or not
        anything downstream survived it, and the zone stamped its own kind
        into the payload, so this neither joins `tracks` nor needs the zone to
        still exist.
        """
        rows = await self._pool.fetch(
            """
            SELECT e.camera_id, c.slug, c.downscale_max_edge,
                   date_trunc('minute', e.at) AS minute,
                   min(e.at) AS first_at, max(e.at) AS last_at
            FROM events e
            JOIN cameras c ON c.id = e.camera_id
            WHERE e.kind = 'zone_enter'
              AND e.payload->>'zone_kind' = 'alpr'
              AND e.at < now() - $1::interval
              AND e.at > now() - $2::interval
              AND NOT EXISTS (
                  SELECT 1 FROM plate_reads r
                  WHERE r.camera_id = e.camera_id
                    AND r.read_at BETWEEN e.at - $3::interval
                                      AND e.at + $4::interval)
              -- And nobody has already looked. Without this the only guard is
              -- the absence of a read, so a pass that yields nothing is read
              -- again every tick for the whole lookback — the same frames, the
              -- same nothing, on an accelerator already at its ceiling.
              AND NOT EXISTS (
                  SELECT 1 FROM plate_zone_pass_reads a
                  WHERE a.camera_id = e.camera_id
                    AND a.minute = date_trunc('minute', e.at))
            -- One pass is several crossings: a car entering the zone
            -- fragments into a handful of short-lived tracks, each firing its
            -- own event a second apart. Grouping by the minute collapses them
            -- into the one arrival they are.
            GROUP BY e.camera_id, c.slug, c.downscale_max_edge,
                     date_trunc('minute', e.at)
            ORDER BY min(e.at) DESC
            LIMIT 2
            """,
            _SETTLE, _PASS_LOOKBACK, _PASS_READ_BEFORE, _PASS_READ_AFTER,
        )
        for row in rows:
            # Marked before the attempt, as the fill sweep is: a scan that
            # finds nothing must not be repeated on the next tick, and a crash
            # mid-scan must not exempt itself from that either.
            await self._pool.execute(
                "INSERT INTO plate_zone_pass_reads (camera_id, minute) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                row["camera_id"], row["minute"],
            )
            try:
                await self._read_pass(row)
            except Exception:
                log.exception("zone-pass read failed on %s", row["slug"])

    async def _read_pass(self, pass_: Any) -> None:
        """Read the window a car took to cross the zone, plus its approach."""
        zones = await self._pool.fetch(
            "SELECT name, polygon FROM zones WHERE camera_id = $1 "
            "AND kind = $2 AND enabled = true",
            pass_["camera_id"], _ALPR_ZONE_KIND,
        )
        boxes = _zone_boxes(zones)
        if not boxes:
            return
        lo = pass_["first_at"] - _PASS_READ_BEFORE
        hi = pass_["last_at"] + _PASS_READ_AFTER
        frames = int((hi - lo).total_seconds() * 4)
        targets = even_times(lo, hi, frames)
        readings = await self._scan_window(
            pass_, lo, hi, targets, [], boxes, frames, sequential=True,
        )
        # No track here either, so the plate's own travel is the whole test.
        readings, standing = drop_standing(readings)
        for text, moved, n in standing:
            log.warning(
                "%s: discarding %d readings of %r — that plate moved %.0f px "
                "across the window, so it was standing in the zone, not "
                "driving through it", pass_["slug"], n, text, moved,
            )
        text_score = self._consensus(readings, pass_["slug"])
        if text_score is None:
            return
        text, score, seen = text_score
        gid = await self._match_identity(None, text)
        if self._assembled(text, seen, gid, pass_["slug"]):
            return
        crop_rel = self._save_crop(
            readings, f"pass-{pass_['slug']}-{int(pass_['first_at'].timestamp())}"
        )
        await self._record_read(
            pass_["camera_id"], None, text, score, crop_rel, readings, gid,
        )
        log.warning(
            "%s: read %r off the recording of a pass through the plate zone",
            pass_["slug"], text,
        )

    async def _sweep_fills(self) -> None:
        """Read the approach for a place that filled without anyone reading it.

        The track is the weak link in the chain, not the footage. On the 26.08
        arrival the plate was legible for six consecutive frames at 75-124 px
        and the registration matched the gallery exactly — but no vehicle
        track survived that arrival, so nothing ever pointed the reader at the
        segment, and the spot stood nineteen hours unnamed over footage that
        answers the question outright.

        A place filling is itself the evidence a car arrived. Given the moment
        it filled and a camera with a plate zone watching it, the reader needs
        nothing else.
        """
        rows = await self._pool.fetch(
            """
            SELECT o.id, o.place, o.occupied_since
            FROM place_occupancy o
            WHERE o.released_at IS NULL
              AND o.global_id IS NULL
              AND o.plate_read_id IS NULL
              AND o.plate_search_at IS NULL
              -- The covering segment has to be closed and indexed first.
              AND o.occupied_since < now() - $1::interval
              AND o.occupied_since > now() - $2::interval
              -- Nothing already read this arrival: the ordinary track-driven
              -- sweep gets first refusal, and this is the fallback.
              AND NOT EXISTS (
                  SELECT 1 FROM plate_reads r
                  WHERE r.read_at BETWEEN o.occupied_since - $3::interval
                                      AND o.occupied_since + $4::interval)
              -- Not a car that never left. Under IR the classifier drops a
              -- standing car and picks it up again seconds later, and that
              -- blink opens what looks exactly like an arrival: measured
              -- 17.08 22:01:47 released, 22:02:23 present again, the car
              -- parked eleven hours either side of it. The approach is empty
              -- at such a moment, so reading it can only waste the decode or
              -- find something standing across the road.
              AND NOT EXISTS (
                  SELECT 1 FROM place_occupancy prev
                  WHERE prev.place = o.place
                    AND prev.released_at BETWEEN o.occupied_since - $5::interval
                                             AND o.occupied_since)
            ORDER BY o.occupied_since DESC
            LIMIT 2
            """,
            _SETTLE, _FILL_LOOKBACK, _FILL_READ_BEFORE, _FILL_READ_AFTER,
            _FLAP_WINDOW,
        )
        for row in rows:
            # Marked before the attempt, not after: a read that finds nothing
            # must not be retried every minute for the whole stay, and a crash
            # mid-scan must not either. One shot per episode — the footage
            # does not improve on the second look.
            await self._pool.execute(
                "UPDATE place_occupancy SET plate_search_at = now() WHERE id = $1",
                row["id"],
            )
            try:
                await self._read_fill(row)
            except Exception:
                log.exception("fill read failed for place %s", row["place"])

    async def _sweep_residents(self) -> None:
        """Ask a place that is occupied but unnamed to show its plate again.

        The approach search runs once because that footage never improves.
        This is a different question asked of different footage: not what
        drove past, but what is standing there now — and that picture changes
        every minute. It is the one thing measured to work on this property
        after dark. On the night of 04.09 both cars arrived unreadable, their
        own headlights drowning the plate; ten minutes later, parked and dark,
        one of them read at full confidence in every frame and went on doing
        so all night while its bay stood unnamed.

        A place that already carries an identity is never a candidate. This
        fills a blank; it does not argue with a name that is already there.
        """
        rows = await self._pool.fetch(
            """
            SELECT o.id, o.place
            FROM place_occupancy o
            WHERE o.released_at IS NULL
              AND o.global_id IS NULL
              AND o.occupied_since < now() - $1::interval
              AND (o.resident_search_at IS NULL
                   OR o.resident_search_at < now() - $2::interval)
            ORDER BY o.occupied_since
            LIMIT 2
            """,
            _RESIDENT_SETTLE, _RESIDENT_RETRY,
        )
        for row in rows:
            # Marked before the attempt, as the approach search is: a scan that
            # finds nothing must wait its interval out rather than run again on
            # the next tick, and a crash mid-scan must not exempt itself.
            await self._pool.execute(
                "UPDATE place_occupancy SET resident_search_at = now() WHERE id = $1",
                row["id"],
            )
            try:
                await self._read_resident(row)
            except Exception:
                log.exception("resident read failed for place %s", row["place"])

    async def _read_resident(self, episode: Any) -> None:
        """Read the car standing in this place, off the last minutes of footage.

        No plate zone is required here, and none would help: a zone watches an
        approach, and this car arrived long ago. What the camera needs is a
        region drawn on the place itself, which is what decides whose plate
        the reading is.
        """
        cams = await self._pool.fetch(
            """
            SELECT c.id AS camera_id, c.slug, c.downscale_max_edge,
                   c.stream_width, c.stream_height,
                   json_agg(json_build_object('place', sr.place,
                                              'polygon', sr.polygon)) AS regions
            FROM scene_regions sr
            JOIN cameras c ON c.id = sr.camera_id
            WHERE sr.enabled AND sr.place IS NOT NULL AND c.enabled
              AND c.stream_width > 0 AND c.stream_height > 0
              AND EXISTS (SELECT 1 FROM scene_regions x
                          WHERE x.camera_id = c.id AND x.place = $1 AND x.enabled)
            GROUP BY c.id, c.slug, c.downscale_max_edge,
                     c.stream_width, c.stream_height
            ORDER BY c.slug
            """,
            episode["place"],
        )
        if not cams:
            log.info("place %s has no camera watching it — nothing to read",
                     episode["place"])
            return
        # Ends where the fill sweep starts, not at now: the segment covering
        # this minute is still being written and has no row to find it by.
        hi = datetime.now(UTC) - _SETTLE
        lo = hi - _RESIDENT_WINDOW
        for cam in cams:
            regions = _place_regions(cam)
            mine = regions.get(episode["place"])
            if mine is None:
                continue
            box = _grown(mine, cam["stream_width"], cam["stream_height"])
            targets = even_times(lo, hi, _RESIDENT_FRAMES)
            readings = await self._scan_window(
                cam, lo, hi, targets, [], [box], _RESIDENT_FRAMES,
                sequential=True,
            )
            readings, elsewhere = bind_to_place(
                readings, episode["place"],
                {p: _pixels(r, cam["stream_width"], cam["stream_height"])
                 for p, r in regions.items()},
            )
            for text, nearest, widths in elsewhere:
                log.info(
                    "%s: %r sits %.1f widths from %s, not from %s — not this car",
                    cam["slug"], text, widths, nearest, episode["place"],
                )
            text_score = self._consensus(readings, cam["slug"])
            if text_score is None:
                continue
            text, score, _ = text_score
            gid = await self._match_identity(None, text)
            if gid is None:
                continue
            crop_rel = self._save_crop(readings, f"resident-{episode['id']}")
            named = await self._name_resident(
                episode, cam["camera_id"], text, score, crop_rel, readings, gid,
            )
            if named:
                log.warning(
                    "place %s: named %r off the car standing in it",
                    episode["place"], text,
                )
                return

    async def _name_resident(
        self,
        episode: Any,
        camera_id: UUID,
        text: str,
        score: float,
        crop_rel: str,
        readings: list[Any],
        gid: UUID,
    ) -> bool:
        """Write the read and consume it on this episode, in one transaction.

        Consumed at birth on purpose. An unconsumed read is free for the
        registry's time join to attach to whatever arrived within five minutes
        of it, and this one was taken hours after its own car arrived — it
        carries geometry, not a moment, and left loose it would wander onto
        somebody else's arrival.

        The episode is re-checked under the write: a name that landed while
        this scan was running wins, and this returns having done nothing.
        """
        times = sorted(r.when for r in readings if r.when is not None)
        if not times:
            log.warning("resident read of %r carries no frame times", text)
            return False
        read_at = times[len(times) // 2]
        demoted: list[Any] = []
        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    read_id = await conn.fetchval(
                        """
                        INSERT INTO plate_reads (camera_id, track_id, read_at,
                                                 plate_text, ocr_score,
                                                 readings, global_id, crop_path)
                        VALUES ($1, NULL, $2, $3, $4, $5, $6, $7)
                        RETURNING id
                        """,
                        camera_id, read_at, text, score, len(readings), gid,
                        crop_rel,
                    )
                    # One car is in one bay. Reading it here settles every
                    # place that only believed it held this identity — which
                    # is how a name put down by the time join gets corrected
                    # by a car that can be seen standing somewhere else.
                    demoted = await demote_rival_places(
                        conn, gid, keeping=episode["id"],
                    )
                    done = await conn.execute(
                        """
                        UPDATE place_occupancy
                        SET global_id = $2, plate_read_id = $3, evidence = 'plate'
                        WHERE id = $1 AND released_at IS NULL AND global_id IS NULL
                        """,
                        episode["id"], gid, read_id,
                    )
                    if not done.endswith(" 1"):
                        # Take the read row down with the naming. Left behind
                        # it would be an unconsumed read carrying an identity
                        # and a timestamp hours from its own arrival, free for
                        # the registry's join to hand to whoever parks next.
                        raise _NotOurs
            except _NotOurs:
                log.info("place %s was named while its car was being read",
                         episode["place"])
                return False
        for d in demoted:
            log.warning(
                "place %s gives up %s — that car is standing in %s and was "
                "read there", d["place"], text, episode["place"],
            )
        return True

    async def _read_fill(self, episode: Any) -> None:
        """Read every plate-zone camera that watches this place, over the
        window in which a car arriving here would have driven past."""
        cams = await self._pool.fetch(
            """
            SELECT DISTINCT c.id AS camera_id, c.slug, c.downscale_max_edge
            FROM scene_regions sr
            JOIN cameras c ON c.id = sr.camera_id
            WHERE sr.place = $1 AND sr.enabled
              AND EXISTS (SELECT 1 FROM zones z
                          WHERE z.camera_id = c.id AND z.kind = $2 AND z.enabled)
            """,
            episode["place"], _ALPR_ZONE_KIND,
        )
        if not cams:
            log.info("place %s has no camera with a plate zone — nothing to read",
                     episode["place"])
            return
        lo = episode["occupied_since"] - _FILL_READ_BEFORE
        hi = episode["occupied_since"] + _FILL_READ_AFTER
        for cam in cams:
            zones = await self._pool.fetch(
                "SELECT name, polygon FROM zones WHERE camera_id = $1 "
                "AND kind = $2 AND enabled = true",
                cam["camera_id"], _ALPR_ZONE_KIND,
            )
            boxes = _zone_boxes(zones)
            if not boxes:
                continue
            targets = even_times(lo, hi, _FILL_FRAMES)
            readings = await self._scan_window(
                cam, lo, hi, targets, [], boxes, _FILL_FRAMES, sequential=True,
            )
            # No car box to judge against here, so the plate's own travel is
            # the whole test — and it has to be, because a zone is a fixed
            # rectangle and whatever parks inside it reads perfectly forever.
            readings, standing = drop_standing(readings)
            for text, moved, n in standing:
                log.warning(
                    "%s: discarding %d readings of %r — that plate moved %.0f px "
                    "across the window, so it was standing in the zone, not "
                    "driving through it", cam["slug"], n, text, moved,
                )
            text_score = self._consensus(readings, cam["slug"])
            if text_score is None:
                continue
            text, score, seen = text_score
            gid = await self._match_identity(None, text)
            if self._assembled(text, seen, gid, cam["slug"]):
                continue
            crop_rel = self._save_crop(readings, f"fill-{episode['id']}")
            await self._record_read(
                cam["camera_id"], None, text, score, crop_rel, readings, gid,
            )
            log.warning(
                "place %s: read %r off the recording of its own arrival",
                episode["place"], text,
            )
            return  # one camera's answer is the answer

    async def _sweep(self) -> None:
        rows = await self._pool.fetch(
            """
            SELECT t.id, t.camera_id, t.started_at,
                   COALESCE(t.ended_at, now()) AS ended_at, t.global_id,
                   c.downscale_max_edge, c.slug
            FROM tracks t
            JOIN cameras c ON c.id = t.camera_id
            WHERE t.class_id = ANY($1::int[])
              AND t.plate_text IS NULL
              -- The visit is over: read it as soon as its segments settle.
              --
              -- The pass through a plate zone USED to be a second condition
              -- here, as `EXISTS (events WHERE e.track_id = t.id …)`. That
              -- made the pass visible only through a track that survived into
              -- this table, and the passes worth reading are precisely the
              -- ones that do not: on the 27.08 arrival all four zone_enter
              -- events belonged to tracks with no `tracks` row at all, while
              -- the row that did survive was born six seconds AFTER the plate
              -- was legible and never touched the zone. A pass is its own
              -- event, so it now drives its own sweep (`_sweep_zone_passes`)
              -- and owes nothing to whether a track outlived it.
              AND t.ended_at IS NOT NULL
              AND t.ended_at < now() - $2::interval
              AND t.ended_at > now() - $3::interval
            ORDER BY COALESCE(t.ended_at, now()) DESC
            LIMIT 5
            """,
            list(_VEHICLE_CLASS_IDS),
            _SETTLE,
            _LOOKBACK,
        )
        for r in rows:
            try:
                await self._read_track(r)
            except Exception:
                log.exception("plate read failed for track %s", r["id"])

    async def _group(self, track: Any) -> list[Any]:
        """This track plus the other cameras' views of the same arrival.

        Overlap in time is the whole test. Two different cars arriving within
        the same forty-five seconds on different cameras would be pooled
        wrongly, which is why only the cameras that actually produced a reading
        are named from it — a camera that saw the plate saw that car.
        """
        siblings = await self._pool.fetch(
            """
            SELECT t.id, t.camera_id, t.started_at,
                   COALESCE(t.ended_at, now()) AS ended_at, t.global_id,
                   c.downscale_max_edge, c.slug
            FROM tracks t
            JOIN cameras c ON c.id = t.camera_id
            WHERE t.class_id = ANY($1::int[])
              AND t.id <> $2
              AND t.camera_id <> $3
              AND t.started_at < $5::timestamptz + $6::interval
              -- COALESCE, not `ended_at > ...`: a still-open track on another
              -- camera is exactly the sibling worth having now that a pass
              -- through a plate zone can trigger the read before any visit has
              -- ended. Filtering on the raw column dropped precisely those.
              AND COALESCE(t.ended_at, now()) > $4::timestamptz - $6::interval
            ORDER BY t.started_at
            LIMIT $7
            """,
            list(_VEHICLE_CLASS_IDS),
            track["id"],
            track["camera_id"],
            track["started_at"],
            track["ended_at"],
            _GROUP_TOLERANCE,
            _MAX_GROUP - 1,
        )
        return [track, *siblings]

    async def _read_track(self, track: Any) -> None:
        for member in await self._group(track):
            # Only a member whose own footage has settled may be stamped
            # "decided": the '' sentinel is permanent, and a still-open
            # sibling swept forty seconds early would be concluded before its
            # own pass was ever on disk. It still gets its read attempt — a
            # success is a success whenever it comes.
            settled = member["id"] == track["id"] or (
                member["ended_at"] < datetime.now(UTC) - _SETTLE
            )
            await self._read_member(member, _MAX_FRAMES, may_conclude=settled)

    async def _alpr_spans(self, member: Any) -> list[tuple[datetime, datetime]]:
        """When this car drove through a plate zone, from the zone's own events.

        This does not consult `track_embedding_samples`, and that is the whole
        point. The embedder samples a parked car every few minutes and a moving
        one only while something asks it to; on the 29.07 arrival the track
        starts at 16:41:01 and its FIRST sample is 16:50:55 — ten minutes later,
        with every sample after it sitting at the same x. Deriving the read
        window from movement therefore found no movement at all and gave up,
        on a track whose approach is recorded second by second in its zone
        events.

        Each span is capped: an approach takes seconds, so a zone_enter with a
        distant or missing exit (the car stopped inside the zone) must not turn
        into the hour it then stood there.
        """
        rows = await self._pool.fetch(
            """
            SELECT e.at, e.kind, e.payload
            FROM events e
            WHERE e.track_id = $1 AND e.kind IN ('zone_enter', 'zone_exit')
            ORDER BY e.at
            """,
            member["id"],
        )
        spans: list[tuple[datetime, datetime]] = []
        open_at: datetime | None = None
        for r in rows:
            p = r["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            # The kind recorded ON THE EVENT, so a zone deleted after the pass
            # still contributes. The 29.07 plate is legible 16:41:06-14, inside
            # a zone that no longer exists.
            if p.get("zone_kind") != _ALPR_ZONE_KIND:
                continue
            if r["kind"] == "zone_enter":
                open_at = open_at or r["at"]
            elif open_at is not None:
                spans.append((open_at, min(r["at"], open_at + _MAX_SPAN)))
                open_at = None
        if open_at is not None:
            spans.append((open_at, min(member["ended_at"], open_at + _MAX_SPAN)))
        return spans

    async def _read_member(
        self, member: Any, budget: int, may_conclude: bool = True
    ) -> None:
        """Read one camera's view of the arrival and let it speak for itself."""
        zones = await self._pool.fetch(
            "SELECT name, polygon FROM zones WHERE camera_id = $1 AND kind = $2 "
            "AND enabled = true",
            member["camera_id"],
            _ALPR_ZONE_KIND,
        )
        samples = await self._pool.fetch(
            """
            SELECT captured_at, bbox
            FROM track_embedding_samples
            WHERE track_id = $1
            ORDER BY captured_at
            """,
            member["id"],
        )
        # Nothing to crop to. Reading the whole native frame is not an option —
        # the detector resizes its input to 608, which is what makes a plate in
        # a 4096-wide frame invisible. A zone supplies that crop when the track
        # cannot.
        if not samples and not zones:
            if may_conclude:
                await self._mark_done(member["id"], None, None, None)
            return

        boxes = _zone_boxes(zones)
        # Two independent accounts of when this car drove past: what the
        # samples show, and what the zone recorded. Either can be missing —
        # the samples for an arrival nobody asked the embedder about, the zone
        # events for a camera with no plate zone drawn — so both are gathered
        # before deciding which to believe.
        moving = moving_samples(samples) if boxes else []
        alpr_spans = await self._alpr_spans(member) if boxes else []
        if boxes and not moving and not alpr_spans:
            # A parked car is not going to show a plate it was not already
            # showing, and reading one is worse than wasteful: one night the
            # tracker kept losing and re-birthing the standing car under IR,
            # and twenty tracks with zero moved samples each read its plate
            # (as ZG7648GZ — IR turns 3 into 9) and became a fresh named row.
            if may_conclude:
                await self._mark_done(member["id"], None, None, None)
            return
        if boxes and moving:
            # The whole moving pass, not the part of it that fell inside a zone.
            # Narrowing to the zone's own enter/exit spans was an optimisation
            # that removed the answer: on the 29.07 arrival it left three
            # seconds (16:41:01-04, car still far enough that the plate spans
            # 31-48 px and OCR returns a different string every frame), while
            # the plate reads perfectly at 16:41:06-14 — a stretch that fell in
            # a zone since deleted. The sweep scanned the surviving window and
            # found nothing on footage that yields ZG9420GZ at confidence 1.00
            # on 26 consecutive frames.
            #
            # The zone is still what says WHERE to crop, which is the part it
            # is good at. WHEN is the movement: a plate is legible for a couple
            # of seconds somewhere in the approach, and which seconds cannot be
            # known in advance — that is what the vote is for.
            #
            # This once claimed the `moving` gate also handled the parked car
            # in the background. It does not and cannot: the gate decides
            # WHETHER to read, and a parked car's plate sits in the zone during
            # every frame of somebody else's approach. Ownership is decided
            # after the scan, on the geometry each reading carries.
            lo = moving[0]["captured_at"] - timedelta(seconds=2)
            hi = moving[-1]["captured_at"] + timedelta(seconds=2)
            targets = even_times(lo, hi, budget)
        elif alpr_spans:
            # No movement to be seen in the samples — which, as often as not,
            # means the approach was never sampled rather than that the car
            # stood still. The zone events know when it drove through.
            targets = []
            per_span = max(8, budget // len(alpr_spans))
            for a, b in alpr_spans:
                targets.extend(even_times(a, b, per_span))
            targets.sort()
            targets = targets[:budget]
        else:
            targets = focus_times(samples, budget)
        readings = await self._scan_window(
            member, member["started_at"], member["ended_at"], targets,
            samples, boxes, budget,
        )

        readings, discarded = drop_fixtures(readings)
        for text, travelled, moved, n in discarded:
            log.warning(
                "%s: discarding %d readings of %r — the car travelled %.0f px "
                "while that plate moved %.0f, so it is not on this car",
                member["slug"], n, text, travelled, moved,
            )
        text_score = self._consensus(readings, member["slug"])
        if text_score is None:
            if may_conclude:
                await self._mark_done(member["id"], None, None, None)
            return
        text, score, seen = text_score
        # The sentinel goes LAST: it is what stops the sweep from ever
        # returning to this track, so everything that must survive — the track
        # stamp, the read row — happens first, and a failure between them
        # leaves the track eligible for a retry instead of leaving the read
        # unrecorded forever. The unique index on plate_reads.track_id makes
        # that retry idempotent.
        gid = await self._match_identity(member["id"], text)
        if self._assembled(text, seen, gid, member["slug"]):
            if may_conclude:
                await self._mark_done(member["id"], None, None, None)
            return
        crop_rel = self._save_crop(readings, str(member["id"]))
        await self._record_read(
            member["camera_id"], member["id"], text, score, crop_rel, readings, gid,
        )
        await self._mark_done(member["id"], text, score, crop_rel)

    async def _scan_window(
        self,
        cam: Any,
        lo: datetime,
        hi: datetime,
        targets: list[datetime],
        samples: list[Any],
        boxes: list[tuple[float, float, float, float]],
        budget: int,
        sequential: bool = False,
    ) -> list[PlateSighting]:
        """Decode `targets` out of whatever segments cover [lo, hi]."""
        segments = await self._pool.fetch(
            f"""
            SELECT r.path, r.started_at, r.ended_at
            FROM recordings r
            WHERE r.camera_id = $1
              AND r.started_at < $3
              AND {covers_until_sql("r.started_at", "r.ended_at")} > $2
            ORDER BY r.started_at
            """,  # noqa: S608
            cam["camera_id"], lo, hi,
        )
        readings: list[PlateSighting] = []
        for seg in segments:
            if budget <= 0:
                break
            seg_end = seg["ended_at"] or hi
            here = [at for at in targets if seg["started_at"] <= at < seg_end]
            if not here:
                continue
            used, found = await asyncio.to_thread(
                self._scan_segment,
                self._media_root / seg["path"],
                seg["started_at"],
                here,
                samples,
                budget,
                cam["downscale_max_edge"],
                boxes,
                sequential,
            )
            budget -= used
            readings.extend(found)
        return readings

    def _consensus(
        self, readings: list[PlateSighting], slug: str
    ) -> tuple[str, float, int] | None:
        vote = PlateVote()
        for r in readings:
            vote.add(r.text)
        if not len(vote):
            return None
        text, agreement = vote.consensus()
        score = float(np.mean([r.confidence for r in readings]))
        seen = vote.seen(text)
        log.info(
            "%s: plate consensus %r over %d readings (agreement %s, ocr %.2f, "
            "read as such %d)",
            slug, text, len(vote),
            " ".join(f"{a:.0%}" for a in agreement), score, seen,
        )
        return text, score, seen

    @staticmethod
    def _assembled(text: str, seen: int, gid: UUID | None, slug: str) -> bool:
        if gid is not None or seen >= _UNMATCHED_MIN_SEEN:
            return False
        log.warning(
            "%s: %r matched no enrolled plate and %d frames read it as such — "
            "the vote assembled it, so it names nothing", slug, text, seen,
        )
        return True

    def _save_crop(self, readings: list[PlateSighting], key: str) -> str:
        crop_rel = MediaLayout.rel(PLATE_CROPS, f"{key}.jpg")
        path = self._media_root / crop_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        best = max(readings, key=lambda r: r.width)
        with contextlib.suppress(Exception):
            cv2.imwrite(str(path), best.crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return crop_rel

    async def _record_read(
        self,
        camera_id: UUID,
        track_id: UUID | None,
        text: str,
        score: float,
        crop_rel: str,
        readings: list[PlateSighting],
        gid: UUID | None,
    ) -> None:
        """The read as an event of its own, matched or not.

        `read_at` is the median frame time of the readings that carried the
        vote — the moment the car actually showed the plate. The decision
        lands minutes later (segment close, sweep tick, CPU scan) and joining
        on it would misplace every arrival, so it is recorded separately.

        An unmatched plate is recorded too: the place badge showing a
        visitor's raw registration is worth more than showing nothing, and
        enrolling the plate later back-fills the identity.
        """
        times = sorted(r.when for r in readings if r.when is not None)
        if not times:
            log.warning("read of %r carries no frame times — not recorded", text)
            return
        read_at = times[len(times) // 2]
        await self._pool.execute(
            """
            INSERT INTO plate_reads (camera_id, track_id, read_at, plate_text,
                                     ocr_score, readings, global_id, crop_path)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (track_id) WHERE track_id IS NOT NULL DO NOTHING
            """,
            camera_id, track_id, read_at, text, score,
            len(readings), gid, crop_rel,
        )
        # A reconcile hiccup must not fail the read path: the read row is
        # already durable, the 30-second sweep will join it, and failing here
        # would skip the terminal sentinel and re-decode 120 frames for a
        # read that succeeded.
        try:
            async with self._pool.acquire() as conn:
                await reconcile_places(conn)
        except Exception:
            log.exception("post-read reconcile failed — the sweep will retry")

    def _scan_segment(
        self,
        path: Path,
        seg_started_at: datetime,
        targets: list[datetime],
        samples: list[Any],
        budget: int,
        downscale_max_edge: int | None,
        boxes: list[tuple[float, float, float, float]] | None = None,
        sequential: bool = False,
    ) -> tuple[int, list[PlateSighting]]:
        """Decode the requested moments of this segment and read what is there.

        Blocking; the caller runs it off the event loop. Returns how much of the
        frame budget was spent and every plate seen, each carrying where it sat
        and where the tracked car sat at that moment.

        Two ways through the file, and which one is cheaper depends entirely on
        how dense the targets are. SEEKING wins when they are a couple of
        clusters in a long segment: reading past everything else to reach them
        is what made a linear scan unaffordable in the first place. But a seek
        is not free — the decoder restarts at the preceding keyframe and
        decodes forward — so once the targets are only a few frames apart, each
        one pays for a whole GOP and the same pictures get decoded over and
        over. Measured on the fill path: 480 seeks across two minutes of
        4096x1152 took ten minutes; reading the same footage straight through
        takes about one.
        """
        if not path.exists():
            return 0, []
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return 0, []
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0
            height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0
            if fps <= 0 or width <= 0 or height <= 0:
                return 0, []
            # The tracker's bboxes are drawn on the downscaled frame; the
            # segment is native. One ratio converts between them.
            scale = _native_scale(width, height, downscale_max_edge)
            hits: list[PlateSighting] = []
            used = 0
            for at, frame in self._frames(cap, seg_started_at, targets, fps,
                                          budget, sequential):
                used += 1
                # Where the car is right now, in the segment's own pixels —
                # the yardstick every reading in this frame is held against.
                owner = _owner_box(samples, at, scale)
                # Each zone is cropped on its own. One rectangle around all of
                # them would fuse two small gates into one large crop and hand
                # the detector something several times coarser — the difference
                # between 46 readings and 6, measured on the same footage.
                crops: list[tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int]]] = []
                if boxes:
                    h, w = frame.shape[:2]
                    for bx in boxes:
                        c = _crop(frame, (bx[0] * w, bx[1] * h, bx[2] * w, bx[3] * h), 1.0)
                        if c is not None:
                            crops.append(c)
                else:
                    c = _crop(frame, _bbox_at(samples, at), scale)
                    if c is not None:
                        crops.append(c)
                for crop, inner, origin in crops:
                    # A plate has to sit inside the window rather than in the
                    # padding around it: on a driveway that padding contains
                    # the neighbouring parked car, whose plate is often the
                    # clearer of the two.
                    for reading in self._stack.read(crop[:, :, ::-1]):
                        if reading.width < _MIN_PLATE_PX:
                            continue
                        x1, y1, x2, y2 = reading.box
                        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                        if not (inner[0] <= cx <= inner[2] and inner[1] <= cy <= inner[3]):
                            continue
                        hits.append(
                            PlateSighting(
                                text=reading.text,
                                confidence=reading.confidence,
                                width=reading.width,
                                at=(origin[0] + cx, origin[1] + cy),
                                owner=None
                                if owner is None
                                else ((owner[0] + owner[2]) / 2, (owner[1] + owner[3]) / 2),
                                crop=crop[y1:y2, x1:x2].copy(),
                                when=at,
                            )
                        )
            return used, hits
        finally:
            cap.release()

    def _frames(
        self,
        cap: Any,
        seg_started_at: datetime,
        targets: list[datetime],
        fps: float,
        budget: int,
        sequential: bool,
    ) -> Any:
        """Yield (moment, frame) for each target, however is cheaper here."""
        if not sequential:
            for at in targets:
                if budget <= 0:
                    break
                frame_no = round((at - seg_started_at).total_seconds() * fps)
                if frame_no < 0:
                    continue
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ok, frame = cap.read()
                if not ok:
                    continue
                budget -= 1
                yield at, frame
            return
        # Straight through, taking the frame nearest each target as it comes
        # past. The first target still gets a seek — a segment is a minute long
        # and the window can start in the middle of it — but only one.
        wanted = sorted(targets)
        if not wanted:
            return
        frame_no = max(0, round((wanted[0] - seg_started_at).total_seconds() * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        i = 0
        while i < len(wanted) and budget > 0:
            ok, frame = cap.read()
            if not ok:
                return
            at = seg_started_at + timedelta(seconds=frame_no / fps)
            frame_no += 1
            if at < wanted[i]:
                continue
            # This frame is at or past the target it answers; skip any further
            # targets it also covers, so a dense request never decodes twice.
            while i < len(wanted) and wanted[i] <= at:
                i += 1
            budget -= 1
            yield at, frame

    async def _mark_done(
        self, track_id: UUID, text: str | None, score: float | None, crop: str | None
    ) -> None:
        # An empty string rather than NULL when nothing was read, so the sweep
        # does not pick the same track up again every minute for six hours.
        await self._pool.execute(
            "UPDATE tracks SET plate_text = $2, plate_score = $3, plate_crop_path = $4 "
            "WHERE id = $1",
            track_id,
            text if text else "",
            score,
            crop,
        )

    async def _match_identity(
        self, track_id: UUID | None, reading: str
    ) -> UUID | None:
        """Which enrolled plate this reading is, if any.

        Stamps the track when there is one, so the visit renders under its
        name — but the PLACE is not named through that row. The registry joins
        on the read event, which is what lets a read with no track at all
        still name the spot it drove into.
        """
        rows = await self._pool.fetch(
            "SELECT global_id, plate, name FROM identity_labels "
            "WHERE plate IS NOT NULL AND plate <> ''"
        )
        if not rows:
            return None
        gallery = {str(r["global_id"]): r["plate"] for r in rows}
        names = {str(r["global_id"]): r["name"] for r in rows}
        hit = match_gallery(reading, gallery)
        if hit is None:
            # No refutation follows. Naming needs distance <= 1.5 AND a margin
            # of 1.5; not matching needs neither, so it is exactly what a bad
            # reading produces.
            log.info("%r matched no enrolled plate", reading)
            return None
        if track_id is not None:
            await self._pool.execute(
                "UPDATE tracks SET global_id = $2, identity_source = 'plate' "
                "WHERE id = $1",
                track_id,
                UUID(hit.key),
            )
        # Loud on purpose: a plate is the strongest claim this system can make
        # about a vehicle.
        log.warning(
            "%s named by plate %r (read %r, distance %.1f, margin %.1f)",
            names.get(hit.key),
            hit.plate,
            reading,
            hit.distance,
            hit.margin,
        )
        return UUID(hit.key)


def _place_regions(cam: Any) -> dict[str, tuple[float, float, float, float]]:
    """Every place this camera watches, as a normalised rectangle each."""
    rows = cam["regions"]
    rows = json.loads(rows) if isinstance(rows, str) else rows
    out: dict[str, tuple[float, float, float, float]] = {}
    for r in rows:
        poly = r["polygon"]
        poly = json.loads(poly) if isinstance(poly, str) else poly
        b = polygon_bounds([poly])
        if b is not None:
            out[r["place"]] = b
    return out


def _pixels(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    """A normalised rectangle in the segment's own pixels — the space a
    reading's position is reported in."""
    return (box[0] * width, box[1] * height, box[2] * width, box[3] * height)


def _grown(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    """The region with room left around it, kept inside the frame.

    The reach is a pixel distance applied to both axes, so an ultrawide camera
    does not gain three times more room sideways than it does vertically.
    """
    x1, y1, x2, y2 = box
    return (
        max(0.0, x1 - _RESIDENT_CROP_REACH_PX / width),
        max(0.0, y1 - _RESIDENT_CROP_REACH_PX / height),
        min(1.0, x2 + _RESIDENT_CROP_REACH_PX / width),
        min(1.0, y2 + _RESIDENT_CROP_REACH_PX / height),
    )


def _zone_boxes(zones: list[Any]) -> list[tuple[float, float, float, float]]:
    """Each ALPR zone as its own normalised rectangle.

    One per zone rather than one around all of them: fusing two small gates
    into one large crop hands the detector something several times coarser —
    measured as the difference between 46 readings and 6 on the same footage.
    """
    boxes: list[tuple[float, float, float, float]] = []
    for z in zones:
        poly = z["polygon"]
        poly = json.loads(poly) if isinstance(poly, str) else poly
        b = polygon_bounds([poly])
        if b is not None:
            boxes.append(b)
    return boxes


def polygon_bounds(polygons: list[Any]) -> tuple[float, float, float, float] | None:
    """The normalised rectangle covering every ALPR zone on this camera.

    A rectangle rather than the polygons themselves: the crop handed to the
    detector has to be a rectangle anyway, and a plate half a metre outside a
    hand-drawn edge is still this driveway's plate.
    """
    xs: list[float] = []
    ys: list[float] = []
    for poly in polygons:
        for pt in poly:
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))
    if not xs or not ys:
        return None
    return (max(0.0, min(xs)), max(0.0, min(ys)), min(1.0, max(xs)), min(1.0, max(ys)))



def _native_scale(width: float, height: float, cap: int | None) -> float:
    """How much bigger the segment is than the frame the tracker drew boxes on.

    Straight from the camera's own long-edge cap, because inferring it from the
    boxes cannot work: a car that never reaches the frame edge says nothing
    about how wide that frame was, so every bbox "fits" at scale 1.0 and the
    crop lands in the top-left corner of a 4096 px frame. Measured that way on
    west — 38 frames decoded, nothing read, because the car was never in the
    crop.
    """
    long_edge = max(width, height)
    if not cap or cap <= 0 or long_edge <= cap:
        return 1.0
    return long_edge / float(cap)


def _bbox_at(samples: list[Any], at: datetime) -> tuple[float, float, float, float]:
    """Where the car was at `at`, interpolated between the samples either side.

    Nearest-sample was measured to lose the plate outright. A track can carry
    183 observations and only 10 embedding samples, and on the 27.07 manoeuvre
    those 10 sat at both ends with a twenty-seven second gap between them — the
    exact stretch where the plate grew from 63 px to 240 px and read ZG9420GZ
    fifteen frames running. Nearest-sample cropped that whole stretch to a box
    the car had left half a minute earlier, so the reader spent its budget on
    empty gravel and reported nothing found.

    Linear between the bracketing samples: a car crossing a driveway does not
    teleport, and an approximate box plus the padding around it contains the
    car, which is all the crop has to do.
    """
    before = None
    after = None
    for s in samples:
        if s["captured_at"] <= at:
            before = s
        elif after is None:
            after = s
            break
    if before is None:
        before = after or samples[0]
    if after is None:
        after = before
    span = (after["captured_at"] - before["captured_at"]).total_seconds()
    frac = 0.0 if span <= 0 else (at - before["captured_at"]).total_seconds() / span
    frac = max(0.0, min(1.0, frac))
    a, b = before["bbox"], after["bbox"]
    return tuple(  # type: ignore[return-value]
        float(a[i]) + (float(b[i]) - float(a[i])) * frac for i in range(4)
    )


def _crop(
    frame: np.ndarray, box: tuple[float, float, float, float], scale: float
) -> tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int]] | None:
    """The padded crop, where the tracker's own box sits inside it, and where
    the crop itself sits in the frame.

    The padding is there because the box lags a moving car by a frame or two;
    the inner rectangle is there because everything the padding adds belongs to
    something else; the origin is there because a reading's position only means
    something in the frame's coordinates, not the crop's.
    """
    h, w = frame.shape[:2]
    bx1, by1, bx2, by2 = (v * scale for v in box)
    pad_x = (bx2 - bx1) * _BBOX_PAD
    pad_y = (by2 - by1) * _BBOX_PAD
    x1 = int(max(0, bx1 - pad_x))
    y1 = int(max(0, by1 - pad_y))
    x2 = int(min(w, bx2 + pad_x))
    y2 = int(min(h, by2 + pad_y))
    if x2 - x1 < 32 or y2 - y1 < 32:
        return None
    inner = (
        int(max(0, bx1 - x1)),
        int(max(0, by1 - y1)),
        int(min(x2 - x1, bx2 - x1)),
        int(min(y2 - y1, by2 - y1)),
    )
    return frame[y1:y2, x1:x2], inner, (x1, y1)


def _owner_box(
    samples: list[Any], at: datetime, scale: float
) -> tuple[float, float, float, float] | None:
    """The tracked car's box at `at`, in the segment's own pixels."""
    if not samples:
        return None
    return tuple(v * scale for v in _bbox_at(samples, at))  # type: ignore[return-value]


