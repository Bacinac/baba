"""Swap a camera into its plate profile while a headlight is drowning the zone.

At night a car coming down the drive points its headlights straight into west,
and the camera — metering a yard whose mean brightness is 15 of 255 — saturates.
Measured on the 28.08 arrival: twenty-four seconds in which the plate zone is
white and not one frame carries a legible registration. Nothing was sampled
badly; there was nothing to sample.

Dedicated ANPR cameras answer this with a short exposure, an illuminator on the
lens axis and an IR band-pass filter, triggered by a loop in the tarmac. We have
neither the filter nor the loop, but we do have the trigger — the bloom itself,
and roughly twenty seconds of approach left after it starts.

The numbers this is built on, all west, 28.08, mean luminance of the plate zone
sampled once a second:

    quiet night   53.1 and 53.5 (min 52.3, max 54.4)  — flat to within 4%
    arrival       peaks 176 and 220                   — 3.3x to 4.2x that
    departure     peak 90                             — 1.7x, tail lights
    daylight      115 (18:00) to 188 (14:00)

So the trigger is relative, never absolute: an arrival at night reaches numbers
a bright afternoon also reaches, and only the ratio to the camera's own recent
baseline separates them. The gate is night itself — because swapping a camera to
IR by day would ruin the picture to fix nothing.

Night is asked of the camera, not inferred from the zone. It used to be the zone
baseline under a fixed 80, and that number sat exactly on where west lives once
the floodlight comes on: the evening of 02.09 the rule stood down at 21:15 (87),
armed at 21:19 (80), stood down at 21:21, armed at 21:51, stood down at 22:09 —
chattering across the threshold through the whole arrival window. Shed's zone
sits at 98 all night and so was never armed at all. In thirty days the two of
them produced three night reads between them, against thirty night arrivals of
which two got a name.

`cameras.light_condition` already answers this, for the yard rather than for one
rectangle of it: the ingestor reports an IR ratio of 1.0 from dusk to dawn and
0.0 by day, with no value in between on any night measured, and the band carries
its own hysteresis and debounce. What this still does NOT cover is a floodlit
yard bright enough to leave no headroom for a doubling — the ratio simply will
not be reached, which is the honest outcome: a yard lit that way is already lit
the way an ANPR installation lights it.

The override itself, its cap and its way back live in the api's `camera_ctl`;
this asks over the bus rather than reaching for the device, so a camera keeps
exactly one writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import statistics
import time
from typing import Any

import nats.errors
import numpy as np

from baba_state_evaluator.plate_reader import polygon_bounds

log = logging.getLogger(__name__)

PROFILE_SUBJECT = "baba.camera.profile"

_ALPR_ZONE_KIND = "alpr"

# The bloom lasts about twenty-four seconds, so a second is dense enough, and it
# costs one crop of a frame the ring already holds.
_SAMPLE_S = 1.0

# Ratio over the camera's own recent baseline that counts as a headlight. Two,
# because an arrival measured 3.3x-4.2x while a quiet night moves by 4%. A
# departure is 1.7x and deliberately below the line: the plate faces away by
# then and the camera is not blinded, so there is nothing to fix.
_TRIGGER_RATIO = 2.0

# The baseline is the median of recent QUIET samples — a bloom must not be able
# to drag it up and disarm the very thing that should have triggered.
_BASELINE_N = 45
_BASELINE_MIN_N = 10

# What the override is asked for: the bloom plus the manoeuvre behind it. The
# api caps anything longer, and restores it on its own if we die holding it.
_HOLD_S = 45.0

# The camera's own verdict that it is night: the ingestor's IR ratio crosses
# outright into this band, 1.0 from dusk to dawn and 0.0 by day, and the band
# already carries hysteresis and a debounce. A camera with no band yet does not
# fire — the same side of the failure the rule has always taken.
_NIGHT_BAND = "ir"

# The IR-cut filter is a mechanical part. A few swaps a night is nothing; a
# trigger that chatters would be. Nothing re-arms inside this.
_COOLDOWN_S = 120.0


def zone_luma(frame: Any, box: tuple[float, float, float, float]) -> float | None:
    """Mean brightness of a normalised rectangle of a ring frame.

    NV12 carries luma first and at full resolution, so for the format this
    pipeline actually runs it is a crop and a mean — nothing converted, nothing
    decoded twice.
    """
    pixels = getattr(frame, "pixels", None)
    if pixels is None or frame.height <= 0 or frame.width <= 0:
        return None
    plane = pixels[: frame.height] if pixels.ndim == 2 else pixels
    x1, y1, x2, y2 = box
    c1, r1 = int(x1 * frame.width), int(y1 * frame.height)
    c2, r2 = int(x2 * frame.width), int(y2 * frame.height)
    crop = plane[r1:r2, c1:c2]
    return float(np.mean(crop)) if crop.size else None


def decide(
    luma: float, baseline: float | None, *, is_night: bool,
    in_cooldown: bool = False
) -> bool:
    """Whether this sample is a headlight. Pure, so the rule can be measured."""
    if baseline is None or not is_night or in_cooldown:
        return False
    return luma > baseline * _TRIGGER_RATIO


class HeadlightWatcher:
    """Watches every camera with a plate zone and asks for the plate profile."""

    def __init__(self, pool: Any, nc: Any, reader_for: Any) -> None:
        self._pool = pool
        self._nc = nc
        self._reader_for = reader_for
        self._history: dict[str, list[float]] = {}
        self._is_night: dict[str, bool] = {}
        self._held_until: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}

    async def _cameras(self) -> list[Any]:
        return await self._pool.fetch(
            """
            SELECT c.id, c.slug, c.light_condition, json_agg(z.polygon) AS polygons
            FROM zones z JOIN cameras c ON c.id = z.camera_id
            WHERE z.kind = $1 AND z.enabled AND c.enabled
            GROUP BY c.id, c.slug, c.light_condition
            """,
            _ALPR_ZONE_KIND,
        )

    async def _ask(self, camera_id: Any, *, release: bool) -> bool:
        body = {
            "camera_id": str(camera_id),
            "reason": "headlight",
            "seconds": _HOLD_S,
            "release": release,
        }
        try:
            msg = await self._nc.request(
                PROFILE_SUBJECT, json.dumps(body).encode(), timeout=8
            )
            answer = json.loads(msg.data)
        except (nats.errors.Error, TimeoutError, ValueError) as e:
            # Loud, and not retried in a tight loop. A camera that kept its own
            # profile is the safe side of this failure.
            log.warning("headlight: camera profile request failed: %s", e)
            return False
        if answer.get("error"):
            log.warning("headlight: profile request refused: %s", answer["error"])
        return bool(answer.get("applied"))

    def _baseline(self, slug: str) -> float | None:
        """What this camera's plate zone looks like when nothing is coming."""
        seen = self._history.get(slug) or []
        if len(seen) < _BASELINE_MIN_N:
            return None
        return statistics.median(seen)

    def _remember(self, slug: str, luma: float) -> None:
        """Only a sample the rule did NOT call a headlight. The bloom is the
        thing being measured against; folding it in raises the bar it just
        cleared and the next arrival passes unnoticed."""
        seen = self._history.setdefault(slug, [])
        seen.append(luma)
        del seen[:-_BASELINE_N]

    async def _sample(self, row: Any, now: float) -> None:
        slug = row["slug"]
        until = self._held_until.get(slug)
        if until is not None:
            if now < until:
                # Nothing is measured while held: every sample is of a picture
                # we changed, and folding those in teaches the watcher that the
                # override is the normal state of the yard.
                return
            self._held_until.pop(slug, None)
            await self._ask(row["id"], release=True)
            return
        frame = self._reader_for(slug).get_latest()
        if frame is None:
            return
        polys = row["polygons"]
        polys = json.loads(polys) if isinstance(polys, str) else polys
        box = polygon_bounds([p if isinstance(p, list) else json.loads(p) for p in polys])
        if box is None:
            return
        luma = zone_luma(frame, box)
        if luma is None:
            return
        baseline = self._baseline(slug)
        is_night = row["light_condition"] == _NIGHT_BAND
        self._note_gate(slug, is_night, baseline)
        fires = decide(
            luma, baseline, is_night=is_night,
            in_cooldown=self._cooldown_until.get(slug, 0.0) > now,
        )
        if not fires:
            self._remember(slug, luma)
            return
        if await self._ask(row["id"], release=False):
            self._held_until[slug] = now + _HOLD_S
            self._cooldown_until[slug] = now + _HOLD_S + _COOLDOWN_S
            log.info(
                "%s: headlight — plate zone at %.0f against a %.0f baseline, "
                "camera in plate profile for %.0fs", slug, luma, baseline, _HOLD_S,
            )

    def _note_gate(self, slug: str, night: bool, baseline: float | None) -> None:
        """Two lines a day: the moment this camera goes over to night and the
        rule starts to mean anything, and the moment it comes back. Without them
        a night in which nothing fired is indistinguishable from a night in
        which nothing was watched."""
        if self._is_night.get(slug) is night:
            return
        self._is_night[slug] = night
        log.info(
            "%s: camera on %s, plate zone at %s — %s", slug,
            _NIGHT_BAND if night else "a daylight band",
            f"{baseline:.0f}" if baseline is not None else "no baseline yet",
            "watching for headlights" if night
            else "headlight rule stood down",
        )

    async def _tick(self) -> None:
        now = time.monotonic()
        for row in await self._cameras():
            await self._sample(row, now)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("headlight watch failed — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_SAMPLE_S)
