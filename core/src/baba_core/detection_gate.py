"""The detection gate: whether a raw model detection is published at all,
and whether it may BIRTH a new track.

This lives in core rather than in the detector because two callers have to
agree exactly. The live detector runs it on every frame. The incident replay
runs it again, offline, over a recorded trace of the same raw detections — once
with the thresholds in force and once with the ones the AI proposes — so the
operator can see what the change WOULD have done before deciding.

A replay that drifts from the live gate is worse than no replay: it argues
confidently for a change that then behaves differently in production. One
function, two callers, no second implementation to keep in step.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GateRules", "classify", "size_pct"]


@dataclass(frozen=True, slots=True)
class GateRules:
    """Resolved thresholds for one (camera, class) pair — global floor with
    the per-camera override already folded in."""

    enabled: bool
    min_confidence: float
    min_box_pct: float


def size_pct(box_width: float, box_height: float, frame_width: int, frame_height: int) -> float:
    """Detection size as % of frame — the LARGER relative dimension. A distant
    person is narrow but tall and still measures tall, so taking the max is
    what makes a single number usable as a floor."""
    if frame_width <= 0 or frame_height <= 0:
        return 0.0
    return 100.0 * max(box_width / frame_width, box_height / frame_height)


def classify(
    rules: GateRules,
    *,
    confidence: float,
    size_pct: float,
    maintain_conf: float | None,
) -> tuple[str | None, bool]:
    """`(drop_reason | None, birth_eligible)` for one detection.

    Two-threshold tracking: publish down to the MAINTAIN floor (the lower of
    the per-camera `maintain_conf` and the per-camera birth threshold) so the
    tracker can keep an existing track alive on a score that decayed below
    birth — a seated person, a parked car. `birth_eligible` marks the ones that
    ALSO cleared the birth threshold and so may spawn a NEW track.

    `maintain_conf` is the camera's own value; there is no global fallback.
    None means the camera map hasn't loaded yet → floor = birth (strict, no
    maintain-only window) until the per-camera value lands.
    """
    if not rules.enabled:
        return "disabled", False
    if rules.min_box_pct > 0 and size_pct < rules.min_box_pct:
        return "size", False
    floor = (
        rules.min_confidence
        if maintain_conf is None
        else min(maintain_conf, rules.min_confidence)
    )
    if confidence < floor:
        return "conf", False
    return None, confidence >= rules.min_confidence
