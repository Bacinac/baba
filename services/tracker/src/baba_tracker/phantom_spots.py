"""Static-phantom suppression: a persistent registry of where tracks are
born and whether anything born there ever moved.

The gap this closes (see migration 062 for the full rationale): the only
birth gate the tracker had was `birth_eligible` — a stateless scalar,
confidence >= per-camera min_confidence. It cannot tell "55% once, on a real
person" from "55% at the same pixels for six months" (pool camera: a garden
hose + shrub reads as `person` forever). The parked-ghost registry in
MotionTracker has the right idea — spot-keyed, movement-aware — but a
5-minute TTL, in-memory only, keyed by track_id, and wiped by any movement.
It exists to stop a phantom re-announcing itself as `active`; it cannot
accumulate the evidence needed to say "this is furniture".

The discriminator, and why it does not regress stationary-person recall:
a phantom is born at the same pixels over and over and NOTHING born there
ever travels. A real subject ARRIVES — it crosses ground, so it clears the
real-mover latch (lifetime centroid extent >= 1x its own bbox diagonal,
`MotionTracker._REAL_MOVER_RATIO`). Suppression needs births AND zero movers
AND — the condition that actually protects people — those births spread over
HOURS. A person who enters from cover and stands perfectly still can flicker
out 20 births with zero movers in two minutes; nobody does it for two hours.
The span is what separates inventory from people. Without it this would be
motion gating with extra steps, which BABA exists to not have.

Self-healing both ways, by construction:
  * a suppressed track is still fully tracked — the gate is applied to what
    we EMIT, and `observe()` runs before it. The instant a suppressed track
    moves it is emitted from that tick AND banks the mover, so a spot that
    was wrong stops being wrong the first time it matters.
  * one mover disqualifies a spot permanently. Recall wins ties.
  * phantom removed (hose coiled up) -> no more births -> the row ages out.

Failure mode is deliberately toward MORE detections: if Postgres is
unreachable the registry stays empty, nothing is ever suppressed, and the
tracker behaves exactly as it did before this module existed. That is a
visible-phantom degradation, never a missed-person one — so it logs loudly
but does not take the service down.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg
from baba_core.phantom_match import (
    spot_contains_foot,
    spot_suppresses,
)
from baba_core.pipeline_settings import Settings

log = logging.getLogger(__name__)

_GLOBALS_KEY = "tracking_defaults"

# 24 h, not the 2 h this shipped with. Raised after watching the live registry
# on .11: `west/car` accrued 9 births in 2 min with zero movers — the carport
# car, which is real, parked, and flickering exactly like a phantom. A parked
# car banks its mover only when it DRIVES, so a 2 h window would blank any car
# left overnight; at 24 h a daily-driven car always proves itself first, and
# its spot is then immune permanently. The cost is that a phantom takes a day
# to learn instead of two hours — worth it to never blank a real vehicle.

# Rows with no birth for this long are dropped: the phantom is gone (or the
# camera was re-aimed) and a stale spot must not suppress whatever occupies
# those pixels next.
_PRUNE_AFTER_DAYS = 30

# Per-camera cap on tracked spots. A pathological scene (detector melting
# down, every frame a new box somewhere new) must not grow the registry
# without bound between prunes. Spots are evicted lowest-births-first: the
# ones near suppression are the ones worth keeping.
_MAX_SPOTS_PER_CAMERA = 500


@dataclass(slots=True)
class Spot:
    """One birth location for one class on one camera."""

    class_name: str
    # [x1, y1, x2, y2] normalised to [0,1] — resolution-independent, so an
    # ingestor downscale change doesn't orphan the registry.
    bbox: tuple[float, float, float, float]
    births: int
    movers: int
    first_birth_s: float
    last_birth_s: float
    # None until the row has been INSERTed.
    id: uuid.UUID | None = None
    dirty: bool = False

    def suppresses(self, min_births: int, min_span_s: float) -> bool:
        return spot_suppresses(
            movers=self.movers,
            births=self.births,
            span_s=self.last_birth_s - self.first_birth_s,
            min_births=min_births,
            min_span_s=min_span_s,
        )


@dataclass(slots=True)
class _CamState:
    spots: list[Spot] = field(default_factory=list)
    # track_id → the spot it was born at. Lets a mover be banked against its
    # BIRTH spot even after it has travelled far away from it.
    born_at: dict[int, Spot] = field(default_factory=dict)
    # track_ids already counted in `movers`, so a mover is banked once.
    banked: set[int] = field(default_factory=set)
    # track_ids whose confidence has crossed the real-subject bar at least
    # once. STICKY: a real seated person's per-frame confidence oscillates
    # across the gate (0.60-0.73), so gating on the instantaneous value makes
    # them flicker in and out of suppression and fragments the track. Once a
    # track peaks above the bar it is a real subject for its whole life — the
    # west lamp never peaks there, a person does.
    confirmed_real: set[int] = field(default_factory=set)


class PhantomSpots:
    """Slug-keyed birth registry. In-memory hot path, periodic DB flush."""

    def __init__(self, dsn: str, settings: Settings) -> None:
        self._dsn = dsn
        # Same row, same resolution as every other tunable. This module used
        # to parse `tracking_defaults` itself — a second reader of one value
        # is how the two answers start to differ.
        self._s = settings
        self._pool: asyncpg.Pool | None = None
        self._cams: dict[str, _CamState] = {}
        self._slug_to_uuid: dict[str, uuid.UUID] = {}
        self._enabled = False

    @property
    def _min_births(self) -> int:
        return self._s.i("phantom_min_births")

    @property
    def _min_span_s(self) -> float:
        return self._s.f("phantom_min_span_s")

    @property
    def _real_subject_conf(self) -> float:
        return self._s.f("phantom_real_subject_conf")

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        await self._reload()
        n = sum(len(c.spots) for c in self._cams.values())
        n_sup = sum(
            1
            for c in self._cams.values()
            for s in c.spots
            if s.suppresses(self._min_births, self._min_span_s)
        )
        log.info(
            "phantom spots loaded: %d spots across %d cameras (%d currently "
            "suppressing), min_births=%d min_span_s=%.0f",
            n,
            len(self._cams),
            n_sup,
            self._min_births,
            self._min_span_s,
        )

    async def stop(self) -> None:
        if self._pool is not None:
            try:
                await self._flush()
            except Exception:
                log.exception("phantom spots final flush failed")
            await self._pool.close()
            self._pool = None

    # --- hot path ---------------------------------------------------------

    def observe(
        self,
        slug: str,
        class_name: str,
        bbox: tuple[float, float, float, float],
        track_id: int,
        ever_moved: bool,
        now_s: float,
        confidence: float,
    ) -> bool:
        """Record this track against its birth spot; return True when it
        should be SUPPRESSED (not emitted).

        One call, not an observe/query pair: the IoU scan is the cost here and
        doing it twice per track per tick would double it for nothing.

        Ordering contract: the caller MUST call this before its emit gate and
        MUST pass the live `ever_moved`. That is what lets a suppressed track
        rehabilitate its own spot the moment it moves.
        """
        if not self._enabled:
            return False
        cam = self._cams.get(slug)
        if cam is None:
            cam = _CamState()
            self._cams[slug] = cam

        spot = cam.born_at.get(track_id)
        if spot is None:
            # First sighting of this track id → a birth.
            spot = self._match(cam, class_name, bbox)
            if spot is None:
                spot = Spot(
                    class_name=class_name,
                    bbox=bbox,
                    births=0,
                    movers=0,
                    first_birth_s=now_s,
                    last_birth_s=now_s,
                )
                cam.spots.append(spot)
                self._evict_if_needed(cam, keep=spot)
            cam.born_at[track_id] = spot
            spot.births += 1
            spot.last_birth_s = now_s
            spot.dirty = True

        if ever_moved and track_id not in cam.banked:
            # Provenance banked against the BIRTH spot, once per track. This
            # is the self-healing edge: a real subject that was wrongly
            # suppressed clears its own spot permanently by walking.
            cam.banked.add(track_id)
            spot.movers += 1
            spot.dirty = True
            log.info(
                "phantom spots: camera=%s class=%s spot at %s banked a mover "
                "(births=%d) — it can never suppress again",
                slug,
                class_name,
                _fmt_bbox(spot.bbox),
                spot.births,
            )

        if ever_moved:
            return False
        # A real subject, wherever it stands, is never suppressed — a reacquired
        # seated person is indistinguishable from furniture by position and
        # motion, and only confidence tells them apart. The gate is on the
        # track's PEAK confidence, not this frame's: a seated person's per-frame
        # score oscillates across the bar (measured 0.60-0.73), so a per-frame
        # gate flickers them in and out and fragments the track. Sticky — once
        # over the bar, real for the track's whole life. The west lamp never
        # crosses it. See phantom_match.
        if confidence >= self._real_subject_conf:
            cam.confirmed_real.add(track_id)
        if track_id in cam.confirmed_real:
            return False
        return spot.suppresses(self._min_births, self._min_span_s)

    def forget_tracks(self, slug: str, live_ids: set[int]) -> None:
        """Drop per-track bookkeeping for tracks Norfair has retired, so the
        maps can't grow across the process lifetime. Spots themselves persist
        — that's the entire point."""
        cam = self._cams.get(slug)
        if cam is None:
            return
        if cam.born_at:
            for tid in [t for t in cam.born_at if t not in live_ids]:
                del cam.born_at[tid]
        cam.banked &= live_ids
        cam.confirmed_real &= live_ids

    def evict_camera(self, slug: str) -> None:
        """Drop per-track bookkeeping when the main loop evicts a camera
        (disabled, or idle > 120 s).

        The SPOTS deliberately survive: they are hours of accumulated
        evidence, they may be dirty (not yet flushed), and a camera going
        quiet for two minutes is not a reason to relearn a hose from zero.
        They leave memory only via `refresh()` (row gone from the DB, e.g.
        the camera was deleted → ON DELETE CASCADE) or `prune()`."""
        cam = self._cams.get(slug)
        if cam is None:
            return
        cam.born_at.clear()
        cam.banked.clear()
        cam.confirmed_real.clear()

    def stats(self) -> tuple[int, int]:
        """(total spots, suppressing spots) — for telemetry/logs."""
        total = 0
        sup = 0
        for cam in self._cams.values():
            for s in cam.spots:
                total += 1
                if s.suppresses(self._min_births, self._min_span_s):
                    sup += 1
        return total, sup

    def _match(
        self,
        cam: _CamState,
        class_name: str,
        bbox: tuple[float, float, float, float],
    ) -> Spot | None:
        # Foot-in-box, not IoU (see phantom_match): a narrower box at the same
        # place still lands its foot in the learned spot. When several spots
        # contain the foot, the one with the most births carries the strongest
        # evidence — route the birth there so it consolidates instead of
        # fragmenting into weak sub-threshold spots.
        best: Spot | None = None
        best_births = -1
        for s in cam.spots:
            if s.class_name != class_name:
                continue
            if spot_contains_foot(s.bbox, bbox) and s.births > best_births:
                best = s
                best_births = s.births
        return best

    def _evict_if_needed(self, cam: _CamState, *, keep: Spot | None = None) -> None:
        if len(cam.spots) <= _MAX_SPOTS_PER_CAMERA:
            return
        # Lowest births first — a spot with one birth carries no evidence,
        # one near the threshold is the reason this registry exists.
        #
        # `keep` is the spot this eviction was called for. It has not been
        # credited with its first birth yet, so it sorts last and was always
        # the one dropped: past the cap the registry could never learn a new
        # spot again, and the caller went on holding a reference to a spot
        # that was no longer in the list.
        cam.spots.sort(key=lambda s: s.births, reverse=True)
        dropped = [s for s in cam.spots[_MAX_SPOTS_PER_CAMERA:] if s is not keep]
        cam.spots = (
            cam.spots[:_MAX_SPOTS_PER_CAMERA]
            + [s for s in cam.spots[_MAX_SPOTS_PER_CAMERA:] if s is keep]
        )
        stale = {id(s) for s in dropped}
        for tid in [t for t, s in cam.born_at.items() if id(s) in stale]:
            del cam.born_at[tid]
        log.warning(
            "phantom spots: camera hit the %d-spot cap, evicted %d "
            "lowest-evidence spots — is the detector churning?",
            _MAX_SPOTS_PER_CAMERA,
            len(dropped),
        )

    # --- persistence ------------------------------------------------------

    async def _reload(self) -> None:
        assert self._pool is not None
        try:
            cams = await self._pool.fetch("SELECT id, slug FROM cameras")
            rows = await self._pool.fetch(
                "SELECT id, camera_id, class_name, bbox, births, movers, "
                "first_birth_at, last_birth_at FROM track_birth_spots"
            )
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
            # Cold-start ordering: the tracker can come up before the api has
            # applied migration 062. Nothing is suppressed until it has.
            log.warning(
                "track_birth_spots not present yet — phantom suppression is "
                "OFF until migrations apply"
            )
            self._enabled = False
            return
        self._slug_to_uuid = {r["slug"]: r["id"] for r in cams}
        uuid_to_slug = {r["id"]: r["slug"] for r in cams}
        cams_new: dict[str, _CamState] = {}
        for r in rows:
            slug = uuid_to_slug.get(r["camera_id"])
            if slug is None:
                continue
            bbox = tuple(float(v) for v in r["bbox"])
            if len(bbox) != 4:
                continue
            st = cams_new.setdefault(slug, _CamState())
            st.spots.append(
                Spot(
                    id=r["id"],
                    class_name=r["class_name"],
                    bbox=bbox,  # type: ignore[arg-type]
                    births=int(r["births"]),
                    movers=int(r["movers"]),
                    first_birth_s=r["first_birth_at"].timestamp(),
                    last_birth_s=r["last_birth_at"].timestamp(),
                )
            )
        # Preserve live per-track bookkeeping across a reload. All three maps,
        # not one: this kept `banked` and dropped `born_at` and
        # `confirmed_real` every thirty minutes, for the life of the process.
        #
        # `born_at` is what makes a birth count once per track. Without it
        # `observe` took the first-sighting branch again for a track that was
        # still alive, so every live track on every camera re-registered as a
        # fresh birth twice an hour — evidence for a suppression that nothing
        # had done.
        #
        # `confirmed_real` is sticky on purpose: a seated person's per-frame
        # confidence oscillates across the gate, so a subject held only by
        # that latch stopped being published mid-visit when it was wiped, and
        # came back only when a frame happened to score above the gate.
        for slug, st in cams_new.items():
            old = self._cams.get(slug)
            if old is not None:
                st.banked = old.banked
                st.born_at = old.born_at
                st.confirmed_real = old.confirmed_real
        self._cams = cams_new
        self._enabled = True

    async def flush(self) -> None:
        """Persist dirty spots + refresh thresholds. Called on a timer by the
        main loop."""
        if self._pool is None:
            return
        if not self._enabled:
            # Migration 062 hadn't applied when we started — the tracker and
            # the api come up together on a deploy and the tracker often wins.
            # Retry on the flush cadence rather than waiting for the 30-minute
            # refresh, which would leave a freshly deployed stack running with
            # suppression off for half an hour for no reason.
            try:
                await self._reload()
                if self._enabled:
                    total, _ = self.stats()
                    log.info(
                        "phantom spots: table is present now — suppression ON "
                        "(%d spots loaded)",
                        total,
                    )
            except Exception:
                log.exception("phantom spots reload failed")
            return
        try:
                await self._flush()
        except Exception:
            log.exception("phantom spots flush failed")

    async def _flush(self) -> None:
        assert self._pool is not None
        for slug, cam in self._cams.items():
            cam_id = self._slug_to_uuid.get(slug)
            if cam_id is None:
                continue
            for spot in cam.spots:
                if not spot.dirty:
                    continue
                first = _to_dt(spot.first_birth_s)
                last = _to_dt(spot.last_birth_s)
                if spot.id is None:
                    spot.id = await self._pool.fetchval(
                        "INSERT INTO track_birth_spots "
                        "(camera_id, class_name, bbox, births, movers, "
                        " first_birth_at, last_birth_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                        cam_id,
                        spot.class_name,
                        list(spot.bbox),
                        spot.births,
                        spot.movers,
                        first,
                        last,
                    )
                else:
                    await self._pool.execute(
                        "UPDATE track_birth_spots SET births = $1, movers = $2, "
                        "last_birth_at = $3 WHERE id = $4",
                        spot.births,
                        spot.movers,
                        last,
                        spot.id,
                    )
                spot.dirty = False

    async def prune(self) -> None:
        """Drop spots nothing has been born at for `_PRUNE_AFTER_DAYS`, in DB
        and in memory. A stale spot must not suppress whatever occupies those
        pixels next."""
        if self._pool is None or not self._enabled:
            return
        try:
            await self._pool.execute(
                "DELETE FROM track_birth_spots "  # noqa: S608
                f"WHERE last_birth_at < now() - interval '{_PRUNE_AFTER_DAYS} days'"
            )
        except Exception:
            log.exception("phantom spots prune failed")
            return
        cutoff = time.time() - _PRUNE_AFTER_DAYS * 86400
        for cam in self._cams.values():
            stale = {id(s) for s in cam.spots if s.last_birth_s < cutoff}
            if not stale:
                continue
            cam.spots = [s for s in cam.spots if id(s) not in stale]
            for tid in [t for t, s in cam.born_at.items() if id(s) in stale]:
                del cam.born_at[tid]

    async def refresh(self) -> None:
        """Re-read the registry (camera added/removed). Flushes first so
        in-flight counts aren't lost to the reload."""
        if self._pool is None:
            return
        try:
            await self._flush()
            await self._reload()
        except Exception:
            log.exception("phantom spots refresh failed")


def _to_dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=UTC)


def _fmt_bbox(b: tuple[float, float, float, float]) -> str:
    return f"[{b[0]:.3f},{b[1]:.3f},{b[2]:.3f},{b[3]:.3f}]"
