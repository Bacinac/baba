"""Automatic tiled inference for extreme-aspect cameras.

The detector's input is a square (BABA_DETECTOR_INPUT_SIZE²) letterbox. A
panoramic camera pays for that geometry twice: a 4096×1152 (3.55:1) stream
letterboxed into 640² keeps only 640×180 of content — the vertical detail is
crushed to 28% of the canvas, and the nano models start hallucinating
(carport car reading as `motorcycle`/`dog` on west). Feeding a higher-res
frame does NOT help: the width-limited letterbox scale still lands on the
same 640×180.

So: when a frame's aspect ratio is extreme, split it along its LONG axis
into overlapping tiles whose per-tile aspect is close to a normal camera's,
run each tile through the unchanged letterbox+inference path, shift the
detections back into frame coordinates and drop cross-tile duplicates from
the overlap band. Everything downstream (rules, tracker, events, UI) keeps
seeing one DetectionsMessage per frame in full-frame coordinates.

The decision is fully automatic from the frame geometry — no per-camera
config. Normal cameras (16:9, 4:3, portrait doorbells) stay on the exact
single-inference path they had.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from baba_core import BoundingBox, Detection

# Aspect ratio (long/short side) above which a frame gets tiled. 16:9 (1.78)
# and portrait 3:4 stay below; the 3.55:1 west panorama that motivated tiling
# is far above.
#
# 2.0 pulls in a 2.22:1 ring like the patio's: measured 2026-07-22, a person
# seated at the far end of that deep scene is ~25 model-pixels after the
# square letterbox, and a half-frame tile doubled that (d-fine-s seated p50
# 0.137 full frame → 0.498 tiled).
#
# NOTE: this threshold only decides anything on the LETTERBOX path. A model
# fed a square STRETCH fills all 640×640 to begin with, so there is nothing
# for a tile to recover — re-measured 2026-07-25 on the same canonical clip,
# tiling bought +0.006 median for 3× the inference. The detector therefore
# skips tiling entirely for stretch families (see `tiles_apply` in __main__);
# the value below stays tuned for the letterbox path that still needs it.
ASPECT_THRESHOLD = 2.0
# Per-tile target aspect after the split — roughly a normal camera.
TARGET_TILE_ASPECT = 1.9
# Overlap between adjacent tiles as a fraction of the SHORT side. Objects
# smaller than the overlap are guaranteed fully visible in at least one tile;
# larger ones are detected partially in both and deduped to the better half.
OVERLAP_FRAC_OF_SHORT = 0.25

# Cross-tile duplicate suppression: same object seen by two overlapping
# tiles. Same-class pairs merge readily; a cross-class pair needs near-total
# overlap before we call it the same object (nano models may classify the
# two partial views differently).
DEDUP_IOU_SAME_CLASS = 0.55
DEDUP_IOU_ANY_CLASS = 0.8


def _even(v: int) -> int:
    """Round down to even — NV12 chroma alignment needs even offsets/sizes."""
    return v & ~1


@dataclass(slots=True, frozen=True)
class TileSpec:
    """One tile's span in picture coordinates (all values even)."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def plan_tiles(width: int, height: int) -> list[TileSpec]:
    """Tile layout for a frame — [full frame] unless the aspect is extreme.

    When tiling engages, the FULL frame stays in the plan as tile 0. Tiles
    magnify: an object spanning most of a tile renders at a scale the nano
    models barely saw in training and they go blind on it (live incident:
    the west carport's black car — detected on the whole-frame pass at ~230
    model-pixels, invisible at ~435 in the tile view). The full-frame pass
    keeps XL objects, the tiles recover the small/medium detail the square
    letterbox crushed, and cross-tile dedup merges the double sightings."""
    if width <= 0 or height <= 0:
        return [TileSpec(0, 0, width, height)]
    long_side, short_side = max(width, height), min(width, height)
    aspect = long_side / short_side
    if aspect <= ASPECT_THRESHOLD:
        return [TileSpec(0, 0, width, height)]

    n = math.ceil(aspect / TARGET_TILE_ASPECT)
    overlap = _even(round(short_side * OVERLAP_FRAC_OF_SHORT))
    tile_len = min(long_side, _even(math.ceil((long_side + (n - 1) * overlap) / n)) + 2)
    # Evenly spread tile origins so the first starts at 0 and the last ends
    # exactly at the frame edge; intermediate ones overlap by construction.
    origins = [
        _even(round(i * (long_side - tile_len) / (n - 1))) if n > 1 else 0
        for i in range(n)
    ]
    tiles: list[TileSpec] = [TileSpec(0, 0, width, height)]
    for o in origins:
        end = min(long_side, o + tile_len)
        if width >= height:
            tiles.append(TileSpec(o, 0, end, height))
        else:
            tiles.append(TileSpec(0, o, width, end))
    return tiles


def slice_tile(
    pixels: np.ndarray, pixel_format: str, width: int, height: int, spec: TileSpec
) -> np.ndarray:
    """Cut one tile out of a frame buffer.

    RGB frames are (H, W, 3). NV12 frames are the flat (H + H/2, W) layout:
    the Y plane in the top H rows and the interleaved UV plane below it at
    half vertical resolution — a tile is the matching row/column spans of
    BOTH planes stacked back into the same flat layout. Even alignment of
    the spec keeps the UV pairs intact.
    """
    if spec.x0 == 0 and spec.y0 == 0 and spec.x1 == width and spec.y1 == height:
        return pixels
    if pixel_format == "nv12":
        y_part = pixels[spec.y0 : spec.y1, spec.x0 : spec.x1]
        uv_part = pixels[
            height + spec.y0 // 2 : height + spec.y1 // 2, spec.x0 : spec.x1
        ]
        return np.ascontiguousarray(np.concatenate([y_part, uv_part], axis=0))
    return np.ascontiguousarray(pixels[spec.y0 : spec.y1, spec.x0 : spec.x1])


def offset_detections(detections: list[Detection], spec: TileSpec) -> list[Detection]:
    """Shift tile-local detections into full-frame coordinates."""
    if spec.x0 == 0 and spec.y0 == 0:
        return detections
    return [
        Detection(
            bbox=BoundingBox(
                x1=d.bbox.x1 + spec.x0,
                y1=d.bbox.y1 + spec.y0,
                x2=d.bbox.x2 + spec.x0,
                y2=d.bbox.y2 + spec.y0,
            ),
            class_id=d.class_id,
            class_name=d.class_name,
            confidence=d.confidence,
        )
        for d in detections
    ]


# Nano DETRs also emit duplicate boxes of DIFFERENT vehicle classes for one
# object (shed black car: `car 0.74` + `truck 0.40` every frame). Two live
# tracks then fight over the same detections, the box swaps geometry each
# frame, stillness never survives to the parked promotion and the adaptive
# rate loops at target_fps. Within the vehicle group a cross-class pair is
# treated like a same-class pair; person deliberately is NOT — a person
# overlapping a car must never be deduped away.
_VEHICLE_GROUP = frozenset({2, 3, 5, 7})  # car, motorcycle, bus, truck


# A tile-partial is a detection clipped at an INTERIOR tile seam (a tile
# edge that isn't the frame border): the object continued past the tile, so
# the box ends exactly on the cut. When such a fragment sits mostly inside a
# larger same-class box (the whole object, recovered by the full-frame pass
# or the tile that saw it complete), it's the SAME object split across tiles
# — safe to drop. This is the narrow, seam-gated containment that the old
# blanket containment rule lacked: a genuine ADJACENT car has a natural box
# NOT pinned to a seam, so it's never flagged as a partial and never merged.
_SEAM_EPS = 3.0  # px tolerance for "edge lands on a tile seam"
CONTAIN_FRAC = 0.70  # fraction of the fragment inside the bigger box to merge


def interior_seams(
    plan: list[TileSpec], width: int, height: int
) -> tuple[frozenset[int], frozenset[int]]:
    """The x/y coordinates where tiles cut the frame, excluding the frame
    border. Empty for a single-tile (non-tiled) plan → dedup stays IoU-only."""
    xs: set[int] = set()
    ys: set[int] = set()
    for t in plan:
        if t.x0 > 0:
            xs.add(t.x0)
        if t.x1 < width:
            xs.add(t.x1)
        if t.y0 > 0:
            ys.add(t.y0)
        if t.y1 < height:
            ys.add(t.y1)
    return frozenset(xs), frozenset(ys)


def is_seam_clipped(
    d: Detection, seam_xs: frozenset[int], seam_ys: frozenset[int]
) -> bool:
    """True when a bbox edge lands on an interior tile seam — the object was
    cut by the tile boundary, so this is a PARTIAL view of something that
    continues into the neighbouring tile. Used both to dedup partials and to
    mark them maintain-only (a partial must never birth its own track id)."""
    b = d.bbox
    return any(
        abs(b.x1 - x) <= _SEAM_EPS or abs(b.x2 - x) <= _SEAM_EPS for x in seam_xs
    ) or any(
        abs(b.y1 - y) <= _SEAM_EPS or abs(b.y2 - y) <= _SEAM_EPS for y in seam_ys
    )


def dedup_detections(
    detections: list[Detection],
    seam_xs: frozenset[int] = frozenset(),
    seam_ys: frozenset[int] = frozenset(),
) -> list[Detection]:
    """Greedy confidence-first suppression of duplicate boxes — cross-tile
    doubles, same-object different-vehicle-class doubles, AND seam-clipped
    tile-partials contained in the whole object. Cheap (N<30); runs on every
    frame, tiled or not. `seam_xs/ys` are the interior tile seams (empty on
    non-tiled cameras → pure IoU behaviour, unchanged).

    The seam gate is what makes the containment rule safe: an earlier BLANKET
    containment criterion merged ADJACENT parked cars whenever the model drew
    one wide box spanning both (live incident, west carport). Here a fragment
    is only merged into a bigger same-class box when it is ALSO clipped on a
    tile seam — i.e. provably a partial view of a tiled object, not a real
    neighbour, whose box is never pinned to a seam."""
    have_seams = bool(seam_xs or seam_ys)
    kept: list[Detection] = []
    for d in sorted(detections, key=lambda d: -d.confidence):
        d_clipped = have_seams and is_seam_clipped(d, seam_xs, seam_ys)
        dup = False
        for k in kept:
            x1 = max(d.bbox.x1, k.bbox.x1)
            y1 = max(d.bbox.y1, k.bbox.y1)
            x2 = min(d.bbox.x2, k.bbox.x2)
            y2 = min(d.bbox.y2, k.bbox.y2)
            inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            if inter <= 0.0:
                continue
            union = d.bbox.area + k.bbox.area - inter
            iou = inter / union if union > 0 else 0.0
            same = k.class_id == d.class_id or (
                k.class_id in _VEHICLE_GROUP and d.class_id in _VEHICLE_GROUP
            )
            if iou >= DEDUP_IOU_ANY_CLASS or (same and iou >= DEDUP_IOU_SAME_CLASS):
                dup = True
                break
            # Seam-gated containment: a tile-partial fragment mostly inside a
            # larger same-class box is the same object split across tiles.
            if (
                d_clipped
                and same
                and d.bbox.area < k.bbox.area
                and d.bbox.area > 0
                and inter / d.bbox.area >= CONTAIN_FRAC
            ):
                dup = True
                break
        if not dup:
            kept.append(d)
    return kept
