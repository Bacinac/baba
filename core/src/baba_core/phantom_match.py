"""Matching a box against the static-phantom birth registry, and the rule
that says a spot currently suppresses.

Two callers have to agree exactly, for the same reason `detection_gate` lives
in core: the TRACKER applies this on the hot path to decide what it emits, and
the API applies it to the raw detection feed so the zone editor can grey out a
box the pipeline is already throwing away. If those two drift, the preview
lies about what production does — which is worse than showing nothing, because
the operator then goes hunting for a phantom that was handled a week ago.

The registry itself (accumulating births, banking movers, persistence) stays
in the tracker; that is where the evidence is produced. Only the read side —
"does this box land on a spot, and is that spot suppressing?" — is shared.

## Why FOOT position, not box IoU

A spot is matched by whether the detection's FOOT — bottom-centre, the
subject's ground contact — falls inside the learned spot box. The registry
used to match by IoU of the whole box, and that let a real failure through: a
lamp on the west camera read as `person` 443 times over 168 h at a fixed spot,
so the spot was a confirmed phantom, but one evening the detector drew a
NARROWER box at the same place (a different pose/crop). Its IoU with the wide
learned box was 0.25, under the 0.5 gate, so it did not match — it started a
fresh 1-birth spot, stayed under the suppress threshold, and reached Activity
as a phantom "person" with nobody there. The foot was squarely inside the
learned spot the whole time. Foot position is stable across box size and pose;
box IoU is not. This is the same anchor ignore-zones and the appearance hold
already use.

## Why a CONFIDENCE gate

Foot-matching alone would re-break the patio: a real person who sits down, is
briefly occluded, and is reacquired by the tracker is BORN at the chair (not
walking in), banks no mover, and looks identical to furniture — foot in a
fixed spot, zero movers. Position and motion cannot separate "chair where
people really sit" from "lamp that is never a person". Confidence can: the
lamp maxes at ~0.55, a real person held by BoxHold sits at 0.85-0.91. So the
registry never suppresses a detection at or above `REAL_SUBJECT_CONF`,
whatever spot its foot lands in — a confident person is a person, wherever it
stands. This is the same principle that rules out motion gating.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_MIN_BIRTHS",
    "DEFAULT_MIN_SPAN_S",
    "REAL_SUBJECT_CONF",
    "foot",
    "spot_contains_foot",
    "spot_suppresses",
]

# Fallbacks for a fresh install whose migrations haven't applied yet; the
# live values come from app_settings.tracking_defaults (migration 062).
DEFAULT_MIN_BIRTHS = 20
DEFAULT_MIN_SPAN_S = 86400.0

# A detection at or above this confidence is treated as a real subject and is
# never suppressed by the registry, no matter which spot its foot lands in.
# West lamp phantoms cap at ~0.55; BoxHold holds a real seated person at
# 0.85+. 0.70 sits in the gap with margin on both sides.
REAL_SUBJECT_CONF = 0.70


def foot(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """Bottom-centre of the box — the subject's ground contact."""
    return ((bbox[0] + bbox[2]) * 0.5, bbox[3])


def spot_contains_foot(
    spot_bbox: tuple[float, float, float, float],
    det_bbox: tuple[float, float, float, float],
) -> bool:
    """Does the detection's foot fall inside the learned spot box?"""
    fx, fy = foot(det_bbox)
    return spot_bbox[0] <= fx <= spot_bbox[2] and spot_bbox[1] <= fy <= spot_bbox[3]


def spot_suppresses(
    *,
    movers: int,
    births: int,
    span_s: float,
    min_births: int,
    min_span_s: float,
) -> bool:
    """All three conditions. `movers == 0` and the span are what keep a
    motionless PERSON out of this branch — a person who enters from cover and
    freezes can flicker out 20 births with zero movers in two minutes; nobody
    does it for a day. The span is what separates inventory from people."""
    if movers > 0:
        return False
    if births < min_births:
        return False
    return span_s >= min_span_s
