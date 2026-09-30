"""Movement heatmap accumulator.

Per-frame observations land in an in-memory grid keyed by camera +
class group. A periodic flush task UPSERTs the day's row in
`heatmap_daily`, summing into existing cells. Restart loses at most
the unflushed window (default 30s of unflushed counts per camera) —
acceptable for a visualisation surface.

Grid is fixed 32×18 cells over normalised [0,1] coords (16:9 cell
aspect; matches typical camera framing). Backend + renderer assume
this constant, so changes require coordinated frontend deploy.

Class group buckets:
  - 'person'  → COCO 0
  - 'vehicle' → COCO 2/3/5/7
  - 'animal'  → COCO 14..23
  - 'other'   → everything else
The dashboard sums across groups for the "all" view server-side, so
the accumulator only writes the four real groups.
"""

from __future__ import annotations

import logging

import asyncpg
from baba_core import local_today

log = logging.getLogger(__name__)


GRID_W = 32
GRID_H = 18
_CELLS = GRID_W * GRID_H

# Coarse class buckets. Single source of truth shared with the
# /analytics/heatmap endpoint (it just sums when 'all' is requested).
_PERSON: frozenset[int] = frozenset({0})
_VEHICLE: frozenset[int] = frozenset({2, 3, 5, 7})
_ANIMAL: frozenset[int] = frozenset(range(14, 24))


def class_group_for(class_id: int) -> str:
    if class_id in _PERSON:
        return "person"
    if class_id in _VEHICLE:
        return "vehicle"
    if class_id in _ANIMAL:
        return "animal"
    return "other"


def _cell_index(x_norm: float, y_norm: float) -> int | None:
    """Bottom-centre point → row-major cell index, or None if out of
    bounds (which happens on shaky bbox bottoms near frame edges)."""
    if not (0.0 <= x_norm <= 1.0 and 0.0 <= y_norm <= 1.0):
        return None
    # Clamp to last column/row when exactly at 1.0 — int(1.0 * 32) = 32
    # which is out of range otherwise.
    gx = min(GRID_W - 1, int(x_norm * GRID_W))
    gy = min(GRID_H - 1, int(y_norm * GRID_H))
    return gy * GRID_W + gx


class HeatmapAccumulator:
    """Thread-safe-ish under asyncio (single-loop); not under threads.
    `add()` is sync (called from _observe which holds the state lock);
    `drain()` reads + clears the per-(camera, group) dict atomically."""

    def __init__(self) -> None:
        # (camera_slug, class_group) → dense list of cell counts.
        # The 4-group split blows the dict to at most 4x previous size,
        # still negligible at v1 scale (32×18 ints × 8 cams × 4 groups
        # = under 80 KB).
        self._buckets: dict[tuple[str, str], list[int]] = {}

    def add(
        self,
        camera_slug: str,
        class_id: int,
        x_norm: float,
        y_norm: float,
    ) -> None:
        idx = _cell_index(x_norm, y_norm)
        if idx is None:
            return
        key = (camera_slug, class_group_for(class_id))
        cells = self._buckets.get(key)
        if cells is None:
            cells = [0] * _CELLS
            self._buckets[key] = cells
        cells[idx] += 1

    def drain(self) -> dict[tuple[str, str], list[int]]:
        out = self._buckets
        self._buckets = {}
        return out

    def merge(self, buckets: dict[tuple[str, str], list[int]]) -> None:
        """Add drained buckets back into the accumulator — used when a flush
        fails so a drained window isn't silently lost. Cells are summed, so
        counts accrued during the failed flush are preserved too."""
        for key, cells in buckets.items():
            existing = self._buckets.get(key)
            if existing is None:
                self._buckets[key] = list(cells)
            else:
                for i, c in enumerate(cells):
                    existing[i] += c


async def flush_to_db(
    pool: asyncpg.Pool,
    buckets: dict[tuple[str, str], list[int]],
    slug_to_uuid: dict[str, object],  # uuid.UUID
) -> None:
    """Upsert each (camera, class_group) bucket into today's row.
    Sums into the existing array via application-layer addition because
    Postgres has no native int[] elementwise add and a SQL-side
    unnest/sum/array_agg roundtrip is wasteful for a 576-element vector.

    Buckets keyed by an unknown slug (camera deleted mid-flush) are
    silently dropped; the next observation for a returning camera
    starts a fresh row."""
    today = local_today()
    for (slug, group), new_cells in buckets.items():
        cam_id = slug_to_uuid.get(slug)
        if cam_id is None:
            log.debug("heatmap flush: skipping unknown camera slug=%s", slug)
            continue
        try:
            async with pool.acquire() as conn, conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT cells FROM heatmap_daily "
                    "WHERE camera_id = $1 AND day = $2 AND class_group = $3 "
                    "FOR UPDATE",
                    cam_id,
                    today,
                    group,
                )
                if existing is None:
                    await conn.execute(
                        """
                            INSERT INTO heatmap_daily
                                (camera_id, day, class_group, grid_w, grid_h, cells)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (camera_id, day, class_group) DO UPDATE
                            SET cells = EXCLUDED.cells, updated_at = now()
                            """,
                        cam_id,
                        today,
                        group,
                        GRID_W,
                        GRID_H,
                        new_cells,
                    )
                else:
                    old = list(existing["cells"]) if existing["cells"] else [0] * _CELLS
                    if len(old) != _CELLS:
                        old = [0] * _CELLS
                    merged = [a + b for a, b in zip(old, new_cells, strict=False)]
                    await conn.execute(
                        "UPDATE heatmap_daily "
                        "SET cells = $1, updated_at = now() "
                        "WHERE camera_id = $2 AND day = $3 AND class_group = $4",
                        merged,
                        cam_id,
                        today,
                        group,
                    )
        except Exception:
            log.exception(
                "heatmap flush failed for camera=%s class_group=%s",
                slug,
                group,
            )
