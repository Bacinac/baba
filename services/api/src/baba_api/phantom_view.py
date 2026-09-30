"""Read-side view of the tracker's static-phantom registry, for the zone
editor's detection preview.

The zone editor subscribes to `baba.detections.*` — the detector's output,
deliberately BEFORE the tracker. That is the right feed for calibration (it
shows what the model says, not what survived), but it made the preview
under-report: it already greys detections inside an operator's ignore zone,
while saying nothing about the other suppression, which is the one that
actually fires day to day. An operator looking at a bench the detector calls
`truck` at 0.65 sees a live-looking box and goes hunting for a setting, when
the registry killed that spot a week ago and there has been no such track
since.

So the api resolves the same question the tracker resolves — does this box
land on a spot that is currently suppressing? — using the SAME matcher and
predicate from `baba_core.phantom_match`. Nothing about the rule is
re-implemented here; only the plumbing is (DB instead of the tracker's live
in-memory registry).

Reading from the DB rather than asking the tracker is deliberate and costs
nothing in accuracy: suppression needs 20 births spread over 24 hours, so a
spot cannot cross the line between two flushes of the tracker's timer. It also
means the preview keeps working when the tracker is restarting — exactly when
an operator is most likely to be staring at this page.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import asyncpg
from baba_core.phantom_match import (
    DEFAULT_MIN_BIRTHS,
    DEFAULT_MIN_SPAN_S,
    REAL_SUBJECT_CONF,
    spot_contains_foot,
    spot_suppresses,
)

log = logging.getLogger(__name__)

# Re-read cadence while a stream is open. The underlying evidence moves on a
# 24-hour scale, so this is about picking up an operator's threshold change or
# a spot that a mover just rehabilitated, not about tracking births.
REFRESH_S = 60.0

# Operator override key, resolved from the same row the tracker reads.
_REAL_CONF_KEY = "phantom_real_subject_conf"


@dataclass(frozen=True, slots=True)
class SuppressedSpot:
    """One spot that is currently suppressing, with the evidence behind it —
    the UI shows the numbers, because "greyed out" without a reason is just a
    different kind of mystery."""

    class_name: str
    bbox: tuple[float, float, float, float]  # normalised [0,1]
    births: int
    span_h: float


class PhantomView:
    """Suppressed spots for ONE camera, refreshed lazily while in use."""

    __slots__ = ("_loaded_at", "_pool", "_real_conf", "_slug", "_spots")

    def __init__(self, pool: asyncpg.Pool, camera_slug: str) -> None:
        self._pool = pool
        self._slug = camera_slug
        self._spots: list[SuppressedSpot] = []
        self._loaded_at = 0.0
        self._real_conf = REAL_SUBJECT_CONF

    async def refresh_if_stale(self) -> None:
        if time.monotonic() - self._loaded_at < REFRESH_S:
            return
        self._loaded_at = time.monotonic()
        try:
            min_births, min_span_s, self._real_conf = await self._thresholds()
            rows = await self._pool.fetch(
                "SELECT s.class_name, s.bbox, s.births, s.movers, "
                "       s.first_birth_at, s.last_birth_at "
                "FROM track_birth_spots s JOIN cameras c ON c.id = s.camera_id "
                "WHERE c.slug = $1",
                self._slug,
            )
        except Exception:
            # Never take the detection stream down for a preview annotation.
            # Failing to an EMPTY list means "nothing is greyed", which is the
            # same direction the tracker fails in when Postgres is unreachable.
            log.exception("phantom view: load failed for camera %s", self._slug)
            self._spots = []
            return
        spots: list[SuppressedSpot] = []
        for r in rows:
            span_s = (r["last_birth_at"] - r["first_birth_at"]).total_seconds()
            if not spot_suppresses(
                movers=int(r["movers"]),
                births=int(r["births"]),
                span_s=span_s,
                min_births=min_births,
                min_span_s=min_span_s,
            ):
                continue
            bbox = tuple(float(v) for v in r["bbox"])
            if len(bbox) != 4:
                continue
            spots.append(
                SuppressedSpot(
                    class_name=r["class_name"],
                    bbox=bbox,  # type: ignore[arg-type]
                    births=int(r["births"]),
                    span_h=span_s / 3600.0,
                )
            )
        self._spots = spots

    async def _thresholds(self) -> tuple[int, float, float]:
        raw = await self._pool.fetchval(
            "SELECT value FROM app_settings WHERE key = 'tracking_defaults'"
        )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        min_births = DEFAULT_MIN_BIRTHS
        min_span_s = DEFAULT_MIN_SPAN_S
        real_conf = REAL_SUBJECT_CONF
        if isinstance(raw, dict):
            v = raw.get("phantom_min_births")
            if isinstance(v, int | float) and v > 0:
                min_births = int(v)
            v = raw.get("phantom_min_span_s")
            if isinstance(v, int | float) and v >= 0:
                min_span_s = float(v)
            v = raw.get(_REAL_CONF_KEY)
            if isinstance(v, int | float) and 0.0 <= v <= 1.0:
                real_conf = float(v)
        return min_births, min_span_s, real_conf

    def match(
        self, class_name: str, nbox: tuple[float, float, float, float], confidence: float
    ) -> SuppressedSpot | None:
        """The suppressing spot this detection lands on, or None.

        Identical rule to the tracker's `_match` + suppress gate, so the grey
        the operator sees equals the suppression the pipeline does: foot inside
        the spot (not box IoU), same class, and never a confident detection —
        the registry leaves a real subject alone wherever it stands.
        """
        if confidence >= self._real_conf:
            return None
        best: SuppressedSpot | None = None
        best_births = -1
        for s in self._spots:
            if s.class_name != class_name:
                continue
            if spot_contains_foot(s.bbox, nbox) and s.births > best_births:
                best = s
                best_births = s.births
        return best
